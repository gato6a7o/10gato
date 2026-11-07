"""Pruebas del motor: decisiones de compresion y archivos reales de FFmpeg."""

from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import compressor

CLI_SCRIPT = Path(__file__).resolve().parents[1] / "10gato.py"

def sample_metadata(duration="33.194542", audio_count=1):
    # Preparar metadatos al probar el calculo de ajustes
    # Parte de una muestra fija y permite variar la duracion y la cantidad de pistas
    return {
        "format": {"duration": duration, "start_time": "0", "format_name": "mov,mp4"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264",
                "width": 2560, "height": 1440, "avg_frame_rate": "60/1",
                "sample_aspect_ratio": "1:1"},
            *[{"index": index + 1, "codec_type": "audio", "codec_name": "aac",
                "sample_rate": "48000", "channels": 2}
                for index in range(audio_count)],
        ],
    }

class PlanningTests(unittest.TestCase):
    """Proteger el presupuesto real, los perfiles y las decisiones de audio."""

    def setUp(self):
        # Crear una fuente aislada antes de cada prueba de planificacion
        # Usa un directorio temporal que unittest limpia al terminar la prueba
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "jugada con espacios.mp4"
        with self.source.open("wb") as output:
            output.truncate(10_000_001)

    def media(self, duration="33.194542", audio_count=1):
        # Analizar una fuente controlada cuando una prueba necesita sus datos
        # Usa el archivo temporal, los metadatos de ejemplo y el analizador del motor
        return compressor.parse_media(self.source, sample_metadata(duration, audio_count))

    def test_sample_uses_calibrated_60fps_without_rounding_duration(self):
        # Comprobar el presupuesto y el perfil al planificar la muestra de Elden Ring
        # Usa su duracion exacta y los valores esperados calculados a mano
        media = self.media()
        plan = compressor.make_plan(media)
        self.assertEqual(media.duration, Decimal("33.194542"))
        self.assertEqual(plan.video_bitrate, 2_265_834)
        self.assertEqual(plan.audio_bitrate, 96_000)
        self.assertEqual((plan.width, plan.height, plan.fps), (1280, 720, Fraction(60)))
        self.assertEqual(plan.mode, "compress")

    def test_profiles_change_at_the_video_budget_boundaries(self):
        # Comprobar la eleccion de perfil al variar el presupuesto disponible
        # Usa duraciones de ejemplo y resoluciones y FPS esperados para cada caso
        for seconds, size in [("10", (1920, 1080, 60)), ("34", (1280, 720, 30)),
                                ("70", (852, 480, 30))]:
            with self.subTest(seconds=seconds):
                plan = compressor.make_plan(self.media(seconds))
                self.assertEqual((plan.width, plan.height, plan.fps), size)

    def test_source_resolution_and_fractional_fps_are_not_increased(self):
        # Comprobar que el plan no aumenta la resolucion ni los FPS de una fuente pequena
        # Usa metadatos con una frecuencia de fotogramas fraccionaria
        data = sample_metadata("10")
        data["streams"][0].update(width=640, height=360, avg_frame_rate="24000/1001")
        plan = compressor.make_plan(compressor.parse_media(self.source, data))
        self.assertEqual((plan.width, plan.height, plan.fps), (640, 360, Fraction(24000, 1001)))

    def test_portrait_and_anamorphic_sources_keep_display_proportions(self):
        # Comprobar la proporcion al planificar fuentes verticales, giradas o con pixeles no cuadrados
        # Usa metadatos de ejemplo y dimensiones de salida calculadas a mano
        for changes, expected in [
            ({"width": 1080, "height": 1920}, (606, 1080)),
            ({"width": 720, "height": 576, "sample_aspect_ratio": "16:15"}, (720, 540)),
            ({"width": 1920, "height": 1080, "side_data_list": [{"rotation": 90}]}, (606, 1080)),
        ]:
            with self.subTest(changes=changes):
                data = sample_metadata("10")
                data["streams"][0].update(changes)
                plan = compressor.make_plan(compressor.parse_media(self.source, data))
                self.assertEqual((plan.width, plan.height), expected)

    def test_long_warning_is_strictly_above_threshold_and_only_for_image_compression(self):
        # Comprobar cuando corresponde el aviso de duracion al planificar un clip
        # Usa casos en el umbral, por encima de el y con un original que ya cabe
        self.assertFalse(compressor.make_plan(self.media("120")).needs_warning)
        self.assertTrue(compressor.make_plan(self.media("120.000001")).needs_warning)
        self.source.write_bytes(b"original pequeno")
        self.assertFalse(compressor.make_plan(self.media("500")).needs_warning)

    def test_audio_budget_can_fall_below_quality_references(self):
        # Comprobar que un presupuesto bajo no provoca un rechazo automatico
        # Usa una duracion larga para reducir los bitrates de video y audio
        plan = compressor.make_plan(self.media("10000"))
        self.assertGreater(plan.video_bitrate, 0)
        self.assertGreater(plan.audio_bitrate, 0)
        self.assertLess(plan.video_bitrate, 100_000)
        self.assertLess(plan.audio_bitrate, 32_000)

    def test_silent_source_reserves_no_audio_budget(self):
        # Comprobar el presupuesto al planificar un clip sin audio
        # Usa metadatos sin pistas de audio y el bitrate de video esperado
        plan = compressor.make_plan(self.media("33.194542", 0))
        self.assertEqual(plan.audio_bitrate, 0)
        self.assertEqual(plan.video_bitrate, 2_361_834)

    def test_retry_budget_uses_actual_excess_and_preserves_audio_selection(self):
        # Comprobar la reduccion proporcional al planificar otro intento
        # Usa un exceso conocido y presupuestos calculados a mano con y sin audio
        for audio_count, video_bitrate, audio_bitrate in ((0, 627_200, 0), (1, 531_200, 96_000)):
            with self.subTest(audio_count=audio_count):
                media = self.media("100", audio_count)
                plan = compressor.make_plan(media)
                retry = compressor.make_retry_plan(media, plan, 12_250_000)
                self.assertEqual((retry.video_bitrate, retry.audio_bitrate), (video_bitrate, audio_bitrate))
                self.assertEqual((retry.width, retry.height, retry.fps), (plan.width, plan.height, plan.fps))
                self.assertEqual(retry.audio_tracks, plan.audio_tracks)

    def test_retry_can_reduce_audio_below_quality_references(self):
        # Comprobar la reduccion del audio cuando el nuevo presupuesto es bajo
        # Usa un clip largo y un exceso conocido sin imponer un minimo de calidad
        media = self.media("1000")
        retry = compressor.make_retry_plan(media, compressor.make_plan(media), 12_250_000)
        self.assertEqual((retry.video_bitrate, retry.audio_bitrate), (47_040, 15_680))

    def test_retry_explains_an_unrepresentable_bitrate(self):
        # Comprobar el fallo si otro intento no puede expresar un bitrate positivo
        # Usa el menor presupuesto representable para que una reduccion no sea posible
        media = self.media(audio_count=0)
        plan = replace(compressor.make_plan(media), video_bitrate=1)
        with self.assertRaisesRegex(compressor.CompressionError, "bitrate"):
            compressor.make_retry_plan(media, plan, 12_250_000)

    def test_unreliable_duration_offers_another_file(self):
        # Comprobar el error al analizar una duracion que no sirve para calcular ajustes
        # Usa metadatos con tiempos ausentes, no finitos o no positivos
        for duration in (None, "N/A", "0", "-1", "NaN", "Infinity"):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(compressor.CompressionError, "otro archivo"):
                    self.media(duration)

    def test_video_duration_can_supply_missing_container_duration(self):
        # Comprobar el analisis cuando falta la duracion del contenedor
        # Usa una duracion fiable en la pista de video como alternativa
        data = sample_metadata(None)
        data["streams"][0]["duration"] = "12.3456789"
        media = compressor.parse_media(self.source, data)
        self.assertEqual(media.duration, Decimal("12.3456789"))

    def test_nonzero_timestamps_do_not_inflate_duration_or_trigger_a_warning(self):
        # Comprobar el presupuesto cuando los tiempos del archivo no empiezan en cero
        # Usa pistas con un origen desplazado y una duracion de contenido conocida
        data = sample_metadata("122.250000")
        data["format"]["start_time"] = "121.000000"
        for stream in data["streams"]:
            stream.update(start_time="121.000000", duration="1.250000")
        media = compressor.parse_media(self.source, data)
        plan = compressor.make_plan(media)
        self.assertEqual(media.duration, Decimal("1.250000"))
        self.assertEqual(plan.video_bitrate, 62_624_000)
        self.assertFalse(plan.needs_warning)

    def test_missing_video_and_unreliable_fps_are_explained(self):
        # Comprobar el error al analizar un archivo sin video o sin FPS fiables
        # Usa metadatos incompletos para comprobar ambos casos por separado
        data = sample_metadata()
        data["streams"][0]["avg_frame_rate"] = "0/0"
        with self.assertRaises(compressor.CompressionError):
            compressor.parse_media(self.source, data)
        data["streams"] = data["streams"][1:]
        with self.assertRaisesRegex(compressor.CompressionError, "video"):
            compressor.parse_media(self.source, data)

    def test_multiple_tracks_need_an_explicit_audio_choice(self):
        # Comprobar la seleccion de audio al planificar un archivo con varias pistas
        # Usa dos pistas para probar la falta de eleccion, una seleccion y una mezcla
        with self.assertRaisesRegex(compressor.CompressionError, "pistas"):
            compressor.make_plan(self.media(audio_count=2))
        plan = compressor.make_plan(self.media(audio_count=2), (1,))
        self.assertEqual(plan.audio_tracks, (1,))
        mixed = compressor.make_plan(self.media(audio_count=2), (1, 2))
        self.assertEqual(mixed.audio_tracks, (1, 2))

    def test_invalid_audio_choices_are_rejected(self):
        # Comprobar el rechazo de una seleccion de audio que no corresponde al archivo
        # Usa indices ausentes, repetidos o pertenecientes al video
        for tracks in ((), (0,), (7,), (1, 1)):
            with self.subTest(tracks=tracks):
                with self.assertRaises(compressor.CompressionError):
                    compressor.make_plan(self.media(audio_count=2), tracks)

    def test_small_mixed_audio_copies_video_instead_of_reencoding(self):
        # Comprobar que el plan copia el video cuando solo falta mezclar el audio
        # Usa un archivo pequeno con dos pistas seleccionadas
        self.source.write_bytes(b"clip pequeno")
        plan = compressor.make_plan(self.media(audio_count=2), (1, 2))
        self.assertEqual(plan.mode, "copy_video")
        self.assertFalse(plan.needs_warning)

    def test_exact_decimal_size_limit_can_use_the_original(self):
        # Comprobar si se puede usar el original cuando alcanza el limite exacto de bytes
        # Usa un archivo temporal de ese tamano con audio ya preparado
        with self.source.open("wb") as output:
            output.truncate(10_000_000)
        self.assertEqual(compressor.make_plan(self.media()).mode, "ready")

    def test_an_extremely_long_ready_clip_does_not_need_an_encoding_budget(self):
        # Comprobar que un clip listo no necesita un presupuesto de conversion
        # Usa un archivo pequeno con una duracion muy larga en sus metadatos
        self.source.write_bytes(b"clip que ya cabe")
        self.assertEqual(compressor.make_plan(self.media("1000000000")).mode, "ready")

@unittest.skipUnless(os.name == "nt", "Comprobar permisos heredados especificos de Windows")
class PublicationPermissionsTests(unittest.TestCase):
    """Comprobar que la copia publicada tiene los permisos de su carpeta de destino."""

    def test_published_file_inherits_destination_permissions(self):
        # Comprobar los permisos al publicar un archivo creado en una carpeta privada
        # Usa una copia de referencia junto al destino y consulta sus permisos con Windows
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "destino"
            destination.mkdir()
            source = destination / "original.mp4"
            source.write_bytes(b"original")
            reference = destination / "referencia.mp4"
            reference.write_bytes(b"referencia")
            expected = subprocess.check_output([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-Acl -LiteralPath $env:TEN_GATO_ACL_TEST_FILE).Sddl",
            ], env={**{key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"},
                    "TEN_GATO_ACL_TEST_FILE": str(reference)}, text=True).strip()
            with tempfile.TemporaryDirectory(dir=destination) as private:
                pending = Path(private) / "pending.mp4"
                pending.write_bytes(b"copia verificada")
                # Reproducir los permisos protegidos observados en el archivo que no abria
                subprocess.run([
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "$acl = Get-Acl -LiteralPath $env:TEN_GATO_ACL_TEST_FILE; "
                    "$acl.SetAccessRuleProtection($true, $true); "
                    "Set-Acl -LiteralPath $env:TEN_GATO_ACL_TEST_FILE -AclObject $acl",
                ], env={**{key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"},
                        "TEN_GATO_ACL_TEST_FILE": str(pending)}, check=True)
                result = compressor.publish_output(pending, source)
            actual = subprocess.check_output([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-Acl -LiteralPath $env:TEN_GATO_ACL_TEST_FILE).Sddl",
            ], env={**{key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"},
                    "TEN_GATO_ACL_TEST_FILE": str(result)}, text=True).strip()
            self.assertEqual(actual, expected)
            self.assertEqual(result.read_bytes(), b"copia verificada")
            self.assertEqual(source.read_bytes(), b"original")

FFMPEG = os.environ.get("TEN_GATO_FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("TEN_GATO_FFPROBE") or shutil.which("ffprobe")

@unittest.skipUnless(FFMPEG and FFPROBE, "Indicar TEN_GATO_FFMPEG y TEN_GATO_FFPROBE para las pruebas reales")
class EncodingTests(unittest.TestCase):
    """Comprobar archivos reales, integridad del video y decisiones interactivas."""

    def setUp(self):
        # Crear un video con dos tonos distinguibles antes de cada prueba de conversion
        # Necesita FFmpeg y un directorio temporal para guardar la fuente
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "jugada ñ con espacios.mp4"
        self.run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=1.25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.25",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1.25",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(self.source),
        ])
        self.original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def run_ffmpeg(self, arguments):
        # Ejecutar FFmpeg cuando una prueba necesita preparar o leer un archivo real
        # Usa el ejecutable configurado y los argumentos de la prueba, sin una consola intermedia
        return subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-n", *arguments],
                                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def video_hashes(self, path):
        # Obtener las huellas de los paquetes al comprobar que el video no se recodifica
        # Usa ffprobe y el archivo indicado para comparar el contenido de cada paquete
        output = subprocess.check_output([
            FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_packets",
            "-show_data_hash", "sha256", "-show_entries", "packet=data_hash", "-of", "json", str(path),
        ])
        return [packet["data_hash"] for packet in json.loads(output)["packets"]]

    @contextmanager
    def sized_attempts(self, sizes):
        # Forzar excesos reproducibles durante conversiones con las herramientas reales
        # Agregar relleno MP4 valido al terminar cada salida, sin cambiar sus pistas
        attempts = []
        real_run = compressor.run_tool

        def run_and_pad(arguments):
            # Completar cada ejecucion y ampliar solo los MP4 pendientes de verificar
            # Usa la herramienta real y los tamanos solicitados por la prueba
            result = real_run(arguments)
            output = Path(arguments[-1])
            if arguments[0] == FFMPEG and output.name == "pending.mp4":
                size = sizes[len(attempts)]
                padding = size - output.stat().st_size
                self.assertGreaterEqual(padding, 8)
                with output.open("ab") as pending:
                    pending.write(struct.pack(">I4s", padding, b"free"))
                    pending.write(b"\0" * (padding - 8))
                attempts.append(arguments)
            return result

        with patch("compressor.run_tool", side_effect=run_and_pad):
            yield attempts

    def test_oversized_result_retries_twice_and_accepts_exact_byte_limit(self):
        # Comprobar los reintentos y el limite inclusive al recibir dos excesos seguidos
        # Usa conversiones reales con relleno y conserva el original y una copia anterior
        media = replace(compressor.inspect_media(self.source, FFPROBE), size=10_000_001)
        existing = self.source.with_name(f"{self.source.stem}_10MB.mp4")
        existing.write_bytes(b"copia anterior")
        messages = []
        with self.sized_attempts([11_000_000, 10_000_001, 10_000_000]) as attempts:
            result = compressor.compress_clip(media, compressor.make_plan(media, (1,)),
                                                FFMPEG, FFPROBE, report=messages.append)
        self.assertEqual(len(attempts), 3)
        bitrates = [int(arguments[arguments.index("-b:v") + 1]) for arguments in attempts]
        self.assertGreater(bitrates[0], bitrates[1])
        self.assertGreater(bitrates[1], bitrates[2])
        self.assertEqual(result.path.stat().st_size, 10_000_000)
        self.assertEqual(result.path.name, f"{self.source.stem}_10MB_2.mp4")
        self.assertEqual(existing.read_bytes(), b"copia anterior")
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_hash)
        self.assertEqual(list(self.source.parent.glob(".10gato-*")), [])
        self.assertEqual(sum("Reintento" in message for message in messages), 2)

    def test_three_oversized_results_fail_without_publishing_or_leaving_temporaries(self):
        # Comprobar el fin de los reintentos cuando ninguna salida cabe
        # Usa excesos reales repetidos y comprueba que no se publica ninguna copia
        media = replace(compressor.inspect_media(self.source, FFPROBE), size=10_000_001)
        with self.sized_attempts([11_000_000] * 4) as attempts:
            with self.assertRaisesRegex(compressor.CompressionError, "reintentos"):
                compressor.compress_clip(media, compressor.make_plan(media, (1,)), FFMPEG, FFPROBE)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(list(self.source.parent.glob("*_10MB*.mp4")), [])
        self.assertEqual(list(self.source.parent.glob(".10gato-*")), [])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_hash)

    def test_oversized_audio_preparation_falls_back_to_image_compression(self):
        # Comprobar la compresion de imagen si copiar el video y mezclar audio excede el limite
        # Usa una primera salida con relleno y comprueba la conversion completa posterior
        media = compressor.inspect_media(self.source, FFPROBE)
        with self.sized_attempts([11_000_000, 10_000_000]) as attempts:
            result = compressor.compress_clip(media, compressor.make_plan(media, (1, 2)), FFMPEG, FFPROBE)
        self.assertEqual(len(attempts), 2)
        self.assertNotIn("-b:v", attempts[0])
        self.assertIn("-b:v", attempts[1])
        self.assertEqual(result.path.stat().st_size, 10_000_000)
        self.assertEqual(len(compressor.inspect_media(result.path, FFPROBE).audio), 1)

    def test_long_audio_fallback_can_be_cancelled_or_accepted_once(self):
        # Comprobar el aviso cuando un clip largo necesita comprimir imagen solo tras preparar el audio
        # Usa una fuente que cabe y fuerza excesos para probar la cancelacion y ambos reintentos
        long_source = self.source.with_name("largo con dos pistas.mp4")
        self.run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=64x36:rate=1:duration=121",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=121",
                            "-f", "lavfi", "-i", "sine=frequency=880:duration=121",
                            "-map", "0:v", "-map", "1:a", "-map", "2:a",
                            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(long_source)])
        media = compressor.inspect_media(long_source, FFPROBE)
        plan = compressor.make_plan(media, (1, 2))
        self.assertEqual(plan.mode, "copy_video")
        self.assertFalse(plan.needs_warning)
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                confirmations = []
                with self.sized_attempts([11_000_000, 10_000_001, 10_000_000]) as attempts:
                    if accepted:
                        result = compressor.compress_clip(media, plan, FFMPEG, FFPROBE,
                            confirm_long=lambda: confirmations.append(True) or True)
                        self.assertEqual(result.path.stat().st_size, 10_000_000)
                        self.assertEqual(len(attempts), 3)
                    else:
                        with self.assertRaises(compressor.CompressionCancelled):
                            compressor.compress_clip(media, plan, FFMPEG, FFPROBE,
                                confirm_long=lambda: confirmations.append(False) or False)
                        self.assertEqual(len(attempts), 1)
                        self.assertEqual(list(self.source.parent.glob("*_10MB*.mp4")), [])
                self.assertEqual(confirmations, [accepted])
                self.assertEqual(list(self.source.parent.glob(".10gato-*")), [])

    def test_interrupting_a_real_encoder_during_retry_kills_it_and_cleans_files(self):
        # Comprobar la cancelacion durante un subproceso real de un reintento
        # Interrumpir su espera como hace Ctrl+C y comprobar que termina sin publicar
        media = replace(compressor.inspect_media(self.source, FFPROBE), size=10_000_001)
        real_communicate = subprocess.Popen.communicate
        interrupted = []
        with self.sized_attempts([11_000_000, 10_000_000]) as attempts:
            def interrupt_wait(process, *args, **kwargs):
                # Interrumpir la espera al comenzar FFmpeg en el segundo intento
                # Usa el proceso real para comprobar despues que fue terminado y recogido
                if process.args[0] == FFMPEG and len(attempts) == 1 and not interrupted:
                    interrupted.append(process)
                    self.assertIsNone(process.poll())
                    raise KeyboardInterrupt
                return real_communicate(process, *args, **kwargs)

            with patch.object(subprocess.Popen, "communicate", interrupt_wait):
                with self.assertRaises(compressor.CompressionCancelled):
                    compressor.compress_clip(media, compressor.make_plan(media, (1,)), FFMPEG, FFPROBE)
        self.assertEqual(len(interrupted), 1)
        self.assertIsNotNone(interrupted[0].poll())
        self.assertEqual(list(self.source.parent.glob(".10gato-*")), [])
        self.assertEqual(list(self.source.parent.glob("*_10MB*.mp4")), [])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_hash)

    def test_missing_video_start_or_audio_end_is_rejected(self):
        # Comprobar el comienzo del video y el final del audio por separado
        # Usa pistas recortadas de forma independiente y un resultado ausente como alternativa
        media = compressor.inspect_media(self.source, FFPROBE)
        plan = compressor.make_plan(media, (1,))
        for kind, filters, message in (
                ("inicio", ["-vf", "select=gte(t\\,0.5)", "-fps_mode", "vfr"], "video"),
                ("audio", ["-af", "atrim=end=0.5"], "audio"),
                ("sin audio", ["-an"], "pistas")):
            with self.subTest(kind=kind):
                output = self.source.with_name(f"recorte {kind}.mp4")
                self.run_ffmpeg(["-i", str(self.source), "-map", "0:v:0", "-map", "0:a:0",
                                    "-c:v", "libx264", "-c:a", "aac", *filters, str(output)])
                with self.assertRaisesRegex(compressor.CompressionError, message):
                    compressor.verify_output(media, plan, output, FFPROBE)

    def test_mixing_preserves_both_signals_and_copies_video(self):
        # Comprobar las dos senales y el video al convertir un clip que solo necesita mezclar audio
        # Usa la fuente de prueba, sus paquetes de video y los tonos del audio resultante
        media = compressor.inspect_media(self.source, FFPROBE)
        result = compressor.compress_clip(media, compressor.make_plan(media, (1, 2)), FFMPEG, FFPROBE)
        self.assertFalse(result.reused_original)
        self.assertLessEqual(result.path.stat().st_size, 10_000_000)
        self.assertEqual(self.video_hashes(self.source), self.video_hashes(result.path))
        audio = self.run_ffmpeg(["-i", str(result.path), "-map", "0:a:0", "-ac", "1",
                                    "-ar", "8000", "-f", "f32le", "pipe:1"])
        values = struct.unpack(f"<{len(audio) // 4}f", audio)
        for frequency in (440, 880):
            real = sum(value * math.cos(2 * math.pi * frequency * index / 8000)
                        for index, value in enumerate(values))
            imaginary = sum(value * math.sin(2 * math.pi * frequency * index / 8000)
                            for index, value in enumerate(values))
            self.assertGreater(2 * math.hypot(real, imaginary) / len(values), 0.03)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_hash)

    def test_a_ready_clip_returns_the_original_without_ffmpeg(self):
        # Comprobar que el motor devuelve el original cuando el clip ya esta listo
        # Usa una fuente con una sola pista y una ruta de FFmpeg que no existe
        ready = self.source.with_name("listo.mp4")
        self.run_ffmpeg(["-i", str(self.source), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", str(ready)])
        media = compressor.inspect_media(ready, FFPROBE)
        result = compressor.compress_clip(media, compressor.make_plan(media), "no-existe", FFPROBE)
        self.assertTrue(result.reused_original)
        self.assertEqual(result.path, ready)

    def test_two_pass_compression_does_not_overwrite_existing_outputs(self):
        # Comprobar la compresion y el nombre de salida cuando ya existe una copia
        # Usa una fuente ampliada con relleno y un archivo previo que debe conservarse
        with self.source.open("r+b") as source:
            source.seek(0, 2)
            source.write(struct.pack(">I4s", 10_000_000, b"free"))
            source.seek(9_999_991, 1)
            source.write(b"\0")
        media = compressor.inspect_media(self.source, FFPROBE)
        plan = compressor.make_plan(media, (1,))
        self.assertEqual(plan.mode, "compress")
        existing = self.source.with_name(f"{self.source.stem}_10MB.mp4")
        existing.write_bytes(b"trabajo anterior")
        result = compressor.compress_clip(media, plan, FFMPEG, FFPROBE)
        self.assertEqual(existing.read_bytes(), b"trabajo anterior")
        self.assertEqual(result.path.name, f"{self.source.stem}_10MB_2.mp4")
        output = compressor.inspect_media(result.path, FFPROBE)
        self.assertEqual((output.width, output.height, output.fps), (320, 180, Fraction(24)))
        self.assertEqual(len(output.audio), 1)
        self.assertLessEqual(output.size, 10_000_000)
        self.assertEqual(list(self.source.parent.glob(".10gato-*")), [])

    def test_truncated_video_is_rejected_even_with_complete_audio(self):
        # Comprobar el rechazo de una salida cuya imagen termina antes que la original
        # Usa un video recortado que conserva la pista de audio completa
        truncated = self.source.with_name("truncado.mp4")
        self.run_ffmpeg(["-i", str(self.source), "-map", "0:v:0", "-map", "0:a:0",
                            "-vf", "select=lt(t\\,0.5)", "-fps_mode", "vfr", "-c:v", "libx264",
                            "-c:a", "copy", str(truncated)])
        media = compressor.inspect_media(self.source, FFPROBE)
        with self.assertRaisesRegex(compressor.CompressionError, "video"):
            compressor.verify_output(media, compressor.make_plan(media, (1,)), truncated, FFPROBE)
        with truncated.open("ab") as output:
            padding = 10_000_001 - truncated.stat().st_size
            output.write(struct.pack(">I4s", padding, b"free") + b"\0" * (padding - 8))
        with self.assertRaisesRegex(compressor.CompressionError, "video"):
            compressor.verify_output(media, compressor.make_plan(media, (1,)), truncated, FFPROBE)

    def test_encoder_failure_cleans_temporary_files(self):
        # Comprobar la limpieza cuando falla la ejecucion del codificador
        # Usa una ruta inexistente de FFmpeg y busca restos junto al original
        media = compressor.inspect_media(self.source, FFPROBE)
        with self.assertRaises(compressor.CompressionError):
            compressor.compress_clip(media, compressor.make_plan(media, (1, 2)), "no-existe", FFPROBE)
        self.assertEqual(list(self.source.parent.glob(".10gato-*")), [])
        self.assertEqual(list(self.source.parent.glob("*_10MB*.mp4")), [])

    def test_long_clip_can_be_cancelled_or_compressed_after_one_warning(self):
        # Comprobar las opciones de continuar y cancelar al convertir un clip largo
        # Usa una fuente real que supera el umbral y registra las confirmaciones solicitadas
        long_source = self.source.with_name("largo.mp4")
        self.run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=64x36:rate=1:duration=121",
                            "-c:v", "libx264", "-preset", "ultrafast", str(long_source)])
        with long_source.open("r+b") as source:
            source.seek(0, 2)
            source.write(struct.pack(">I4s", 10_000_000, b"free"))
            source.seek(9_999_991, 1)
            source.write(b"\0")
        media = compressor.inspect_media(long_source, FFPROBE)
        plan = compressor.make_plan(media)
        with self.assertRaises(compressor.CompressionCancelled):
            compressor.compress_clip(media, plan, "no-existe", FFPROBE, confirm_long=lambda: False)
        confirmations = []
        with self.sized_attempts([11_000_000, 10_000_001, 10_000_000]):
            result = compressor.compress_clip(media, plan, FFMPEG, FFPROBE,
                                                confirm_long=lambda: confirmations.append(True) or True)
        self.assertEqual(confirmations, [True])
        self.assertEqual(compressor.inspect_media(result.path, FFPROBE).duration, Decimal("121"))

    def test_cli_inspection_does_not_convert_or_require_an_audio_choice(self):
        # Comprobar la opcion de inspeccion al ejecutar la consola en otro proceso
        # Usa la fuente con dos pistas y comprueba los datos mostrados y la ausencia de copias
        command = [os.sys.executable, str(CLI_SCRIPT), str(self.source),
                    "--ffprobe", FFPROBE, "--inspect"]
        inspected = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertIn("1.250000", inspected.stdout)
        self.assertIn("Pista 1", inspected.stdout)
        self.assertIn("Pista 2", inspected.stdout)
        self.assertEqual(list(self.source.parent.glob("*_10MB*.mp4")), [])

    def test_cli_missing_audio_choice_does_not_silently_mix(self):
        # Comprobar el error cuando la consola recibe varias pistas sin una eleccion de audio
        # Usa una entrada vacia para impedir la respuesta interactiva y comprobar que no hay conversion
        command = [os.sys.executable, str(CLI_SCRIPT), str(self.source),
                    "--ffprobe", FFPROBE, "--ffmpeg", FFMPEG]
        result = subprocess.run(command, input="", capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--audio-track", result.stderr)
        self.assertEqual(list(self.source.parent.glob("*_10MB*.mp4")), [])

    def test_mixing_a_delayed_track_preserves_its_onset(self):
        # Comprobar el comienzo de una pista retrasada al mezclarla con otra
        # Usa dos tonos y comprueba que el segundo no aparece antes de tiempo
        delayed = self.source.with_name("audio desplazado.mp4")
        self.run_ffmpeg(["-i", str(self.source), "-itsoffset", "0.25", "-i", str(self.source),
                            "-map", "0:v:0", "-map", "0:a:0", "-map", "1:a:1", "-c", "copy", str(delayed)])
        media = compressor.inspect_media(delayed, FFPROBE)
        result = compressor.compress_clip(media, compressor.make_plan(media, (1, 2)), FFMPEG, FFPROBE)
        raw = self.run_ffmpeg(["-i", str(result.path), "-map", "0:a:0", "-ac", "1",
                                "-ar", "8000", "-f", "f32le", "pipe:1"])
        values = struct.unpack(f"<{len(raw) // 4}f", raw)[:1600]
        for frequency, is_present in ((440, True), (880, False)):
            amplitude = 2 * abs(sum(value * complex(math.cos(2 * math.pi * frequency * index / 8000),
                                                    math.sin(2 * math.pi * frequency * index / 8000))
                                    for index, value in enumerate(values))) / len(values)
            self.assertGreater(amplitude, 0.03) if is_present else self.assertLess(amplitude, 0.005)

    def test_nonzero_container_origin_is_shared_by_video_and_audio(self):
        # Comprobar la alineacion al convertir un archivo cuyo tiempo no empieza en cero
        # Usa un contenedor desplazado y compara los paquetes de video antes y despues
        shifted = self.source.with_name("origen desplazado.mp4")
        self.run_ffmpeg(["-i", str(self.source), "-map", "0:v:0", "-map", "0:a:0",
                            "-map", "0:a:1", "-c", "copy", "-output_ts_offset", "2", str(shifted)])
        media = compressor.inspect_media(shifted, FFPROBE)
        self.assertGreater(media.origin, 1)
        self.assertLess(media.duration, Decimal("1.3"))
        result = compressor.compress_clip(media, compressor.make_plan(media, (1, 2)), FFMPEG, FFPROBE)
        self.assertFalse(result.reused_original)
        self.assertEqual(self.video_hashes(shifted), self.video_hashes(result.path))

if __name__ == "__main__":
    unittest.main()
