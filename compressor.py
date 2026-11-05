"""Motor de 10gato: analizar y preparar clips sin depender de una interfaz"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Callable
import json
import os
import shutil
import subprocess
import tempfile
import uuid

MAX_BYTES = 10_000_000
TARGET_BYTES = 9_800_000
LONG_CLIP_SECONDS = Decimal(120)
INITIAL_AUDIO_BITRATE = 96_000

class CompressionError(Exception):
    """Informar un motivo que pueda mostrarse directamente al usuario."""

class CompressionCancelled(CompressionError):
    """Indicar una cancelacion sin tratarla como una conversion terminada."""

@dataclass(frozen=True)
class AudioTrack:
    """Identificar una pista del archivo mediante su indice global de ffprobe."""

    index: int
    codec: str

@dataclass(frozen=True)
class MediaInfo:
    """Conservar los datos necesarios para planificar sin volver a leer el archivo."""

    path: Path
    size: int
    duration: Decimal
    origin: Decimal
    video_index: int
    video_codec: str
    width: int
    height: int
    pixel_aspect: Fraction
    fps: Fraction
    audio: tuple[AudioTrack, ...]
    formats: frozenset[str]

@dataclass(frozen=True)
class CompressionPlan:
    """Separar las decisiones de compresion de la ejecucion de FFmpeg."""

    mode: str
    video_bitrate: int
    audio_bitrate: int
    width: int
    height: int
    fps: Fraction
    audio_tracks: tuple[int, ...]
    needs_warning: bool

@dataclass(frozen=True)
class TimeSpan:
    """Conservar el inicio y el final realmente decodificados de una pista."""

    start: Decimal
    end: Decimal

@dataclass(frozen=True)
class CompressionResult:
    """Indicar que archivo puede usar el usuario despues de una operacion valida."""

    path: Path
    reused_original: bool

def positive_decimal(value):
    # Leer una duracion de ffprobe durante el analisis
    # Aceptar solo cifras positivas y finitas que puedan convertirse a Decimal
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except InvalidOperation:
        return None

def parse_media(path: Path, data: dict) -> MediaInfo:
    # Interpretar los datos del clip antes de calcular sus ajustes
    # Usa los metadatos de ffprobe y el archivo original para consultar su tamano
    path = Path(path).resolve()
    videos = [stream for stream in data.get("streams", [])
                if stream.get("codec_type") == "video"
                and not stream.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise CompressionError("No se encontró una pista de video. Elige otro archivo.")
    video = videos[0]
    container = data.get("format", {})
    duration = positive_decimal(container.get("duration")) or positive_decimal(video.get("duration"))
    try:
        origin = Decimal(str(container.get("start_time", "0")))
        if not origin.is_finite():
            raise ValueError

        # Usar la cobertura de las pistas si sus tiempos son fiables, sin contar el origen como contenido
        timeline_streams = [video, *[stream for stream in data.get("streams", [])
                                        if stream.get("codec_type") == "audio"]]
        if all(positive_decimal(stream.get("duration")) is not None
                and "start_time" in stream for stream in timeline_streams):
            ends = [Decimal(stream["start_time"]) + Decimal(stream["duration"])
                    for stream in timeline_streams]
            if all(end.is_finite() for end in ends):
                duration = positive_decimal(max(ends) - origin)
        if duration is None:
            raise CompressionError("No hay una duración fiable para calcular la compresión. Elige otro archivo.")
        width, height = int(video["width"]), int(video["height"])
        fps = Fraction(video.get("avg_frame_rate", "0/0"))
        if width < 2 or height < 2 or fps <= 0:
            raise ValueError
        aspect_text = video.get("sample_aspect_ratio", "1:1")
        aspect = Fraction(aspect_text.replace(":", "/")) if aspect_text not in ("N/A", "0:1") else Fraction(1)
        if aspect <= 0:
            raise ValueError
        rotation = next((item["rotation"] for item in video.get("side_data_list", [])
                            if "rotation" in item), video.get("tags", {}).get("rotate", 0))
        if int(rotation) % 180:
            width, height, aspect = height, width, 1 / aspect
        audio = tuple(AudioTrack(int(stream["index"]), stream.get("codec_name", ""))
                        for stream in data.get("streams", []) if stream.get("codec_type") == "audio")
        return MediaInfo(path, path.stat().st_size, duration, origin, int(video["index"]),
                            video.get("codec_name", ""), width, height, aspect, fps, audio,
                            frozenset(container.get("format_name", "").split(",")))
    except (ValueError, KeyError, TypeError, ZeroDivisionError, InvalidOperation) as error:
        raise CompressionError("No se pudieron leer la resolución, los FPS o los tiempos. Elige otro archivo.") from error

def make_plan(media: MediaInfo, audio_tracks: tuple[int, ...] | None = None) -> CompressionPlan:
    # Elegir los ajustes tras analizar el clip y resolver la seleccion de audio
    # Usa la duracion real, las pistas elegidas y los limites de tamano y calidad
    available = tuple(track.index for track in media.audio)
    if audio_tracks is None:
        if len(available) > 1:
            raise CompressionError("Hay varias pistas de audio. Elige una pista o confirma cuáles mezclar.")
        audio_tracks = available
    if (len(set(audio_tracks)) != len(audio_tracks) or set(audio_tracks) - set(available)
            or (available and not audio_tracks)):
        raise CompressionError("La selección de pistas de audio no es válida.")

    ready_audio = audio_tracks == available and len(available) <= 1 and all(
        track.codec == "aac" for track in media.audio)
    if media.size <= MAX_BYTES and media.video_codec == "h264" and "mp4" in media.formats:
        mode = "ready" if ready_audio else "copy_video"
        return CompressionPlan(mode, 0, INITIAL_AUDIO_BITRATE if audio_tracks else 0,
                                media.width, media.height, media.fps, audio_tracks, False)

    total_bitrate = int(Decimal(TARGET_BYTES * 8) / media.duration)
    audio_bitrate = min(INITIAL_AUDIO_BITRATE, total_bitrate // 4) if audio_tracks else 0
    video_bitrate = total_bitrate - audio_bitrate
    if video_bitrate < 1 or (audio_tracks and audio_bitrate < 1):
        raise CompressionError("El presupuesto no permite expresar un bitrate positivo para el codificador.")

    if video_bitrate >= 4_500_000:
        max_width, max_height, max_fps = 1920, 1080, 60
    elif video_bitrate >= 2_250_000:
        max_width, max_height, max_fps = 1280, 720, 60
    elif video_bitrate >= 1_200_000:
        max_width, max_height, max_fps = 1280, 720, 30
    else:
        max_width, max_height, max_fps = 854, 480, 30

    # Mantener la proporcion visible sin aumentar ninguna dimension codificada
    display_width = media.width * media.pixel_aspect
    scale = min(Fraction(max_width) / display_width, Fraction(max_height, media.height),
                Fraction(media.width) / display_width, Fraction(1))
    width = int(display_width * scale) // 2 * 2
    height = int(media.height * scale) // 2 * 2
    if width < 2 or height < 2:
        raise CompressionError("La proporción del video no permite una salida H.264 con dimensiones pares.")

    return CompressionPlan("compress", video_bitrate, audio_bitrate, width, height,
                            min(media.fps, Fraction(max_fps)), audio_tracks,
                            media.duration > LONG_CLIP_SECONDS)

def resolve_tool(value: str | None, name: str) -> str:
    # Localizar FFmpeg o ffprobe cuando la consola necesita ejecutarlo
    # Usa la ruta indicada por el usuario o busca el ejecutable en el PATH
    executable = shutil.which(value or name)
    if not executable:
        raise CompressionError(f"No se encontró {name}. Indica su ruta con --{name}.")
    return executable

def run_tool(arguments: list[str]) -> str:
    # Ejecutar una herramienta al analizar, convertir o verificar el clip
    # Recibe el ejecutable y sus argumentos por separado, sin pasar por una consola
    try:
        with subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            try:
                output, errors = process.communicate()
            except KeyboardInterrupt as error:
                process.kill()
                process.communicate()
                raise CompressionCancelled("Conversión cancelada. Se han limpiado los temporales.") from error
        if process.returncode or errors.strip():
            reason = errors.decode("utf-8", errors="replace").strip()[-2000:]
            raise CompressionError(f"{Path(arguments[0]).name} no pudo completar la operación: {reason}")
        return output.decode("utf-8", errors="replace")
    except OSError as error:
        raise CompressionError(f"No se pudo ejecutar {arguments[0]}: {error}") from error

def inspect_media(path: Path, ffprobe: str) -> MediaInfo:
    # Analizar el archivo al seleccionarlo o comprobar una salida
    # Necesita un archivo accesible y la ruta del ejecutable ffprobe
    path = Path(path).resolve()
    if not path.is_file():
        raise CompressionError("No se encontró el clip. Elige otro archivo.")
    output = run_tool([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    try:
        return parse_media(path, json.loads(output))
    except json.JSONDecodeError as error:
        raise CompressionError("ffprobe no devolvió metadatos válidos. Elige otro archivo.") from error

def read_span(path: Path, stream_index: int, ffprobe: str) -> TimeSpan:
    # Medir los extremos de una pista durante la verificacion
    # Usa ffprobe para decodificar la pista indicada y consultar sus tiempos
    output = run_tool([
        ffprobe, "-v", "error", "-err_detect", "explode", "-select_streams", str(stream_index),
        "-show_frames", "-show_streams", "-show_entries",
        "frame=best_effort_timestamp_time,duration_time,pkt_duration_time,nb_samples:stream=sample_rate",
        "-of", "json", str(path),
    ])
    try:
        data = json.loads(output)
        sample_rate = data.get("streams", [{}])[0].get("sample_rate")
        spans = []
        for frame in data["frames"]:
            start = Decimal(frame["best_effort_timestamp_time"])
            duration = (Decimal(frame["nb_samples"]) / Decimal(sample_rate)
                        if sample_rate else positive_decimal(frame.get("duration_time"))
                        or positive_decimal(frame.get("pkt_duration_time")))
            if not start.is_finite() or duration is None or duration <= 0:
                raise ValueError
            spans.append((start, start + duration))
        return TimeSpan(min(start for start, _ in spans), max(end for _, end in spans))
    except (ValueError, KeyError, IndexError, TypeError, InvalidOperation, ZeroDivisionError) as error:
        raise CompressionError("No se pudo verificar el inicio y el final de una pista decodificada.") from error

def verify_output(media: MediaInfo, plan: CompressionPlan, output: Path, ffprobe: str) -> None:
    # Verificar la copia temporal despues de convertir y antes de guardarla
    # Compara su tamano y sus pistas con el original y los ajustes previstos
    if output.stat().st_size > MAX_BYTES:
        raise CompressionError("El resultado supera 10.000.000 bytes. No se guardó ninguna copia. "
                                "Los reintentos automáticos estarán disponibles en el siguiente hito.")
    result = inspect_media(output, ffprobe)
    if result.video_codec != "h264" or len(result.audio) != bool(plan.audio_tracks):
        raise CompressionError("El resultado no contiene las pistas de video y audio esperadas.")
    if result.audio and result.audio[0].codec != "aac":
        raise CompressionError("El audio resultante no es AAC.")
    if plan.mode == "compress" and (result.width, result.height, result.fps) != (
            plan.width, plan.height, plan.fps):
        raise CompressionError("La resolución o los FPS de salida no coinciden con lo previsto.")

    # Comparar sobre el origen comun del contenedor para conservar los desfases entre pistas
    tolerance = max(Decimal("0.1"), Decimal(2 * plan.fps.denominator) / plan.fps.numerator)
    original_video = read_span(media.path, media.video_index, ffprobe)
    output_video = read_span(output, result.video_index, ffprobe)
    for original_time, output_time in ((original_video.start, output_video.start),
                                        (original_video.end, output_video.end)):
        if abs(original_time - media.origin - output_time) > tolerance:
            raise CompressionError("La pista de video está truncada o desplazada. No se guardó la copia.")
    if plan.audio_tracks:
        originals = [read_span(media.path, index, ffprobe) for index in plan.audio_tracks]
        output_audio = read_span(output, result.audio[0].index, ffprobe)
        expected = TimeSpan(min(span.start for span in originals) - media.origin,
                            max(span.end for span in originals) - media.origin)
        if abs(expected.start - output_audio.start) > tolerance or abs(expected.end - output_audio.end) > tolerance:
            raise CompressionError("El audio está incompleto o desplazado. No se guardó la copia.")

def audio_arguments(media: MediaInfo, plan: CompressionPlan, ffprobe: str) -> list[str]:
    # Preparar las opciones de audio antes de ejecutar la conversion
    # Usa las pistas elegidas y consulta sus tiempos con ffprobe si hay que mezclarlas
    if not plan.audio_tracks:
        return ["-an"]
    options = ["-c:a", "aac", "-b:a", str(plan.audio_bitrate), "-ar", "48000", "-ac", "2"]
    if len(plan.audio_tracks) == 1:
        return ["-map", f"0:{plan.audio_tracks[0]}", *options]

    # Alinear las senales antes de mezclarlas para no adelantar una pista que empiece tarde
    first_time = min(read_span(media.path, index, ffprobe).start for index in plan.audio_tracks) - media.origin
    first_sample = int(first_time * 48000)
    filters = [f"[0:{index}]aresample=48000:async=1:first_pts={first_sample},"
                f"aformat=channel_layouts=stereo[a{position}]"
                for position, index in enumerate(plan.audio_tracks)]
    inputs = "".join(f"[a{position}]" for position in range(len(plan.audio_tracks)))
    filters.append(f"{inputs}amix=inputs={len(plan.audio_tracks)}:duration=longest:"
                    "dropout_transition=0:normalize=1[mixed]")
    return ["-filter_complex", ";".join(filters), "-map", "[mixed]", *options]

def publish_output(temporary: Path, source: Path) -> Path:
    # Guardar la copia al superar la verificacion, sin reemplazar archivos existentes
    # Usa un archivo nuevo junto al original para heredar los permisos de esa carpeta
    staging = source.parent / f".10gato-{uuid.uuid4().hex}.mp4"
    staging_created = False
    try:
        # Copiar solo los bytes verificados, sin conservar los permisos privados del temporal
        with staging.open("xb") as outgoing:
            staging_created = True
            with temporary.open("rb") as incoming:
                shutil.copyfileobj(incoming, outgoing)
        sequence = 1
        while True:
            suffix = "" if sequence == 1 else f"_{sequence}"
            destination = source.with_name(f"{source.stem}_10MB{suffix}.mp4")
            try:
                if os.name == "nt":
                    # Aprovechar que Windows rechaza un destino existente al renombrar
                    staging.rename(destination)
                else:
                    os.link(staging, destination)
                return destination
            except FileExistsError:
                sequence += 1
    finally:
        if staging_created:
            staging.unlink(missing_ok=True)

def compress_clip(media: MediaInfo, plan: CompressionPlan, ffmpeg: str, ffprobe: str,
                    confirm_long: Callable[[], bool] | None = None,
                    report: Callable[[str], None] | None = None) -> CompressionResult:
    # Aplicar el plan cuando la consola solicita convertir un clip
    # Necesita FFmpeg y ffprobe; recibe funciones para pedir confirmacion e informar del avance
    report = report or (lambda message: None)
    if plan.mode == "ready":
        return CompressionResult(media.path, True)
    if plan.needs_warning and (confirm_long is None or not confirm_long()):
        raise CompressionCancelled("Conversión cancelada. Puedes elegir otro clip.")

    common = [ffmpeg, "-v", "error", "-nostdin", "-n", "-copyts", "-start_at_zero", "-i", str(media.path)]
    audio = audio_arguments(media, plan, ffprobe)
    with tempfile.TemporaryDirectory(prefix=".10gato-", dir=media.path.parent) as directory:
        working = Path(directory)
        output = working / "pending.mp4"
        if plan.mode == "copy_video":
            report("Preparando el audio y copiando el video sin recodificar…")
            run_tool([*common, "-map", f"0:{media.video_index}", "-c:v", "copy", *audio,
                        "-map_metadata", "-1", "-movflags", "+faststart", str(output)])
        else:
            video = ["-map", f"0:{media.video_index}", "-vf",
                        f"scale={plan.width}:{plan.height}:flags=lanczos,setsar=1,fps={plan.fps}",
                        "-c:v", "libx264", "-preset", "slow", "-b:v", str(plan.video_bitrate),
                        "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-passlogfile", str(working / "x264")]
            report("Comprimiendo video: pasada 1 de 2…")
            run_tool([*common, *video, "-pass", "1", "-an", "-f", "null", os.devnull])
            report("Comprimiendo video: pasada 2 de 2…")
            run_tool([*common, *video, "-pass", "2", *audio, "-map_metadata", "-1",
                        "-movflags", "+faststart", str(output)])
        report("Verificando el tamaño y las pistas completas de video y audio…")
        verify_output(media, plan, output, ffprobe)
        destination = publish_output(output, media.path)
    return CompressionResult(destination, False)
