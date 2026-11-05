"""Entrada por consola de 10gato para probar el motor antes de crear la ventana"""

import argparse
import os
from pathlib import Path
import sys

from compressor import (
    CompressionCancelled,
    CompressionError,
    MediaInfo,
    compress_clip,
    inspect_media,
    make_plan,
    resolve_tool,
)

LONG_WARNING = ("Este clip dura más de 2 minutos. Para que el video completo quepa en 10 MB, "
                "puede perder bastante detalle")

def create_parser() -> argparse.ArgumentParser:
    # Definir las opciones cuando arranca la consola
    # Usa argparse y toma las rutas predeterminadas de las variables de entorno
    parser = argparse.ArgumentParser(description="10gato: comprimir un clip de video para que quepa en 10 MB.")
    parser.add_argument("clip", type=Path, help="Ruta del clip original.")
    parser.add_argument("--ffmpeg", default=os.environ.get("TEN_GATO_FFMPEG"), help="Ruta de ffmpeg.exe.")
    parser.add_argument("--ffprobe", default=os.environ.get("TEN_GATO_FFPROBE"), help="Ruta de ffprobe.exe.")
    parser.add_argument("--inspect", action="store_true", help="Consultar los datos sin convertir el archivo.")
    audio = parser.add_mutually_exclusive_group()
    audio.add_argument("--audio-track", type=int, help="Indice global de la pista que ya contiene el audio deseado.")
    audio.add_argument("--mix-audio", action="store_true", help="Mezclar todas las pistas; usar solo si son independientes.")
    parser.add_argument("--continue-long", action="store_true", help="Aceptar el aviso de duración sin una pregunta interactiva.")
    return parser

def describe_media(media: MediaInfo) -> None:
    # Mostrar los datos despues de analizar el clip
    # Recibe el resultado del analisis y conserva la duracion sin redondear
    print(f"Original: {media.path}")
    print(f"Duración: {media.duration} s · Tamaño: {media.size:,} bytes")
    print(f"Video: {media.width}×{media.height} · FPS: {media.fps}")
    for track in media.audio:
        print(f"Pista {track.index}: audio {track.codec}")
    if not media.audio:
        print("Sin pistas de audio.")

def choose_audio(media: MediaInfo, arguments: argparse.Namespace) -> tuple[int, ...] | None:
    # Resolver la seleccion de audio antes de calcular la compresion
    # Usa las pistas detectadas y las opciones recibidas; pregunta si falta una eleccion
    if arguments.audio_track is not None:
        return (arguments.audio_track,)
    if arguments.mix_audio:
        return tuple(track.index for track in media.audio)
    if len(media.audio) <= 1:
        return None
    print("Hay varias pistas. Usar una pista que ya incluya todo evita duplicar la voz.")
    try:
        answer = input("Indica el índice de una pista, o escribe 'mezclar' si son independientes: ").strip()
        if answer.casefold() == "mezclar":
            return tuple(track.index for track in media.audio)
        return (int(answer),)
    except (EOFError, ValueError) as error:
        raise CompressionError("Elige el audio con --audio-track ÍNDICE o --mix-audio. No se ha convertido el clip.") from error

def confirm_long_clip(continue_long: bool) -> bool:
    # Pedir confirmacion cuando hace falta comprimir la imagen de un clip largo
    # La opcion de continuar permite aceptar el aviso sin una pregunta interactiva
    print(LONG_WARNING)
    if continue_long:
        return True
    try:
        return input("1. Comprimir de todos modos\n2. Elegir otro clip\nElige una opción: ").strip() == "1"
    except EOFError:
        return False

def main(argv: list[str] | None = None) -> int:
    # Coordinar el flujo al ejecutar el programa desde la consola
    # Usa las opciones de entrada, el motor de compresion y las herramientas configuradas
    arguments = create_parser().parse_args(argv)
    try:
        ffprobe = resolve_tool(arguments.ffprobe, "ffprobe")
        media = inspect_media(arguments.clip, ffprobe)
        describe_media(media)
        if arguments.inspect:
            return 0
        plan = make_plan(media, choose_audio(media, arguments))
        ffmpeg = resolve_tool(arguments.ffmpeg, "ffmpeg") if plan.mode != "ready" else ""
        if plan.mode == "compress":
            print(f"Salida prevista: {plan.width}×{plan.height} · FPS: {plan.fps}")
            print(f"Video: {plan.video_bitrate} b/s · Audio: {plan.audio_bitrate} b/s")
        result = compress_clip(media, plan, ffmpeg, ffprobe,
                                confirm_long=lambda: confirm_long_clip(arguments.continue_long), report=print)
        print("Tu clip ya está listo" if result.reused_original else "Tu copia verificada está lista")
        print(result.path)
        return 0
    except CompressionCancelled as error:
        print(str(error), file=sys.stderr)
        return 2
    except (CompressionError, OSError) as error:
        print(f"No se pudo completar: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Operación cancelada.", file=sys.stderr)
        return 2

if __name__ == "__main__":
    # Emitir textos legibles tambien al redirigir la consola de Windows a otro proceso
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
