"""Pruebas de la ventana y de la cancelacion compartida con el motor."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import struct
import threading
import time
import unittest
from unittest.mock import patch

import compressor

try:
    from PySide6.QtCore import QMimeData, QSettings, QUrl, Qt, QTimer
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:
    QApplication = None

class CancellationTests(unittest.TestCase):
    """Comprobar que cancelar no depende del teclado ni del hilo de la consola."""

    def test_cancel_stops_a_running_process_and_restores_the_context(self):
        # Cancelar durante una espera real para comprobar el cierre del subproceso
        # Usa un evento independiente y un proceso Python que tardaria en terminar
        cancelled = threading.Event()
        timer = threading.Timer(0.3, cancelled.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaises(compressor.CompressionCancelled):
            with compressor.cancellable_operation(cancelled.is_set):
                compressor.run_tool([sys.executable, "-c", "import time; time.sleep(20)"])
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(compressor.run_tool([sys.executable, "-c", "print('listo')"]).strip(), "listo")

    def test_cancel_before_publication_leaves_no_copy(self):
        # Impedir la entrega si se cancela despues de verificar y antes de publicar
        # Usa bytes temporales propios y comprueba que el original permanece intacto
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "original.mp4"
            pending = Path(directory) / "pending.mp4"
            source.write_bytes(b"original")
            pending.write_bytes(b"verificado")
            with self.assertRaises(compressor.CompressionCancelled):
                with compressor.cancellable_operation(lambda: True):
                    compressor.publish_output(pending, source)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(sorted(path.name for path in source.parent.iterdir()), ["original.mp4", "pending.mp4"])

@unittest.skipUnless(QApplication, "Instalar requirements.txt en el entorno de la interfaz")
class WindowTests(unittest.TestCase):
    """Comprobar acciones de la ventana mediante el bucle real de Qt."""

    @classmethod
    def setUpClass(cls):
        # Compartir la aplicacion al comenzar las pruebas de widgets
        # Necesita PySide6 y una unica instancia de QApplication
        import gui
        cls.gui = gui
        cls.app = QApplication.instance() or QApplication([])
        gui.apply_theme(cls.app)

    def setUp(self):
        # Abrir una ventana aislada antes de cada prueba
        # Usa rutas de herramientas configuradas y temporales propios
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.app.setFont, self.app.font())
        settings_path = str(Path(self.temporary.name) / "settings.ini")
        settings_patch = patch("gui.QSettings", side_effect=lambda *_: QSettings(settings_path, QSettings.IniFormat))
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.window = self.gui.MainWindow(os.environ.get("TEN_GATO_FFMPEG"), os.environ.get("TEN_GATO_FFPROBE"))
        self.window.show()
        self.addCleanup(self.close_window)

    def close_window(self):
        # Cerrar la ventana al terminar incluso si fallo una comprobacion
        # Esperar a que el trabajador cancele antes de destruir los objetos de Qt
        self.window.cancel_operation()
        self.wait_until(lambda: self.window.worker is None)
        self.window.close()

    def wait_until(self, condition, timeout=10):
        # Procesar eventos al esperar un resultado asincrono en las pruebas
        # Usa un plazo para detectar bloqueos en lugar de esperar indefinidamente
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(condition(), "La operacion no termino dentro del plazo")

    def test_initial_state_is_dark_and_requires_a_clip(self):
        # Comprobar el estado inicial antes de seleccionar un archivo
        # Usa la paleta efectiva y las acciones disponibles de la ventana
        self.assertLess(self.app.palette().window().color().lightness(), 70)
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertFalse(self.window.cancel_button.isEnabled())
        self.assertFalse(self.window.show_button.isVisible())
        self.assertTrue(self.window.select_button.isEnabled())

    def test_invalid_file_explains_the_error_and_allows_another_selection(self):
        # Comprobar la recuperacion al analizar un archivo inexistente
        # Usa una ruta local ausente sin abrir dialogos bloqueantes
        self.window.load_clip(Path(self.temporary.name) / "ausente.mp4")
        self.wait_until(lambda: self.window.worker is None)
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.select_button.isEnabled())
        self.assertIn("No se", self.window.status_label.text())

    def test_file_selection_loads_only_an_accepted_path(self):
        # Comprobar que cancelar conserva el clip y que aceptar entrega la ruta completa
        # Simular la respuesta del selector para no abrir un dialogo en la suite
        path = Path(self.temporary.name) / "clip con espacios.mp4"
        self.window.path_label.setText("Clip anterior.mp4")
        with patch.object(self.gui.QFileDialog, "exec", return_value=0), \
                patch.object(self.gui.QFileDialog, "getOpenFileName", side_effect=[("", ""), (str(path), "")]), \
                patch.object(self.window, "load_clip") as load:
            QTest.mouseClick(self.window.select_button, Qt.LeftButton)
            load.assert_not_called()
            self.assertEqual(self.window.path_label.text(), "Clip anterior.mp4")
            QTest.mouseClick(self.window.select_button, Qt.LeftButton)
            load.assert_called_once_with(path)

    def test_font_focus_moves_to_the_other_control_at_each_limit(self):
        # Seguir ajustando con el teclado cuando se desactiva el boton enfocado
        # Comprobar ambos limites con una ventana activa y eventos de Qt
        window = self.window
        window.activateWindow()
        self.app.processEvents()
        cases = (
            (19, 20, window.increase_font_button, window.decrease_font_button, Qt.Key_Right),
            (11, 10, window.decrease_font_button, window.increase_font_button, Qt.Key_Left),
        )
        for initial, limit, button, opposite, arrow in cases:
            with self.subTest(limit=limit):
                window.change_font_size(initial)
                button.setFocus()
                QTest.keyClick(button, Qt.Key_Space)
                self.assertEqual(window.font_size, limit)
                self.assertIs(window.focusWidget(), opposite)
                QTest.keyClick(opposite, Qt.Key_Space)
                self.assertEqual(window.font_size, initial)
                QTest.keyClick(opposite, arrow)
                self.assertIs(window.focusWidget(), button)

        window.decrease_font_button.setFocus()
        QTest.keyClick(window.decrease_font_button, Qt.Key_Tab)
        self.assertIs(window.focusWidget(), window.increase_font_button)

    def test_zoom_shortcuts_accept_plus_variants_and_keep_the_file_field_focused(self):
        # Aumentar una sola vez con las variantes que Qt recibe segun el teclado
        # Mantener el foco del archivo incluso cuando se alcanza el maximo
        window = self.window
        window.activateWindow()
        self.app.processEvents()
        field = window.path_label
        field.setFocus()
        cases = (
            (Qt.Key_Plus, Qt.ControlModifier),
            (Qt.Key_Plus, Qt.ControlModifier | Qt.ShiftModifier),
            (Qt.Key_Plus, Qt.ControlModifier | Qt.KeypadModifier),
            (Qt.Key_Equal, Qt.ControlModifier),
            (Qt.Key_Equal, Qt.ControlModifier | Qt.ShiftModifier),
        )
        for key, modifiers in cases:
            with self.subTest(key=key, modifiers=modifiers):
                window.change_font_size(12)
                QTest.keyClick(field, key, modifiers)
                self.assertEqual(window.font_size, 13)
                self.assertIs(window.focusWidget(), field)

        window.change_font_size(19)
        QTest.keyClick(field, Qt.Key_Plus, Qt.ControlModifier)
        self.assertEqual(window.font_size, 20)
        self.assertIs(window.focusWidget(), field)

    def test_font_controls_restore_and_remember_size(self):
        # Ajustar la letra con botones y atajos y volver a abrir con la misma preferencia
        # Usar un archivo temporal de Qt y recuperar la fuente de las otras pruebas
        settings = QSettings(str(Path(self.temporary.name) / "appearance.ini"), QSettings.IniFormat)
        settings.setValue("appearance/fontSize", 99)
        settings.sync()
        self.window.close()
        with patch("gui.QSettings", side_effect=lambda *_: QSettings(settings.fileName(), QSettings.IniFormat)):
            window = self.gui.MainWindow()
            window.show()
            self.addCleanup(window.close)
            window.activateWindow()
            self.app.processEvents()
            self.assertEqual(window.font().pointSize(), 12)
            QTest.mouseClick(window.increase_font_button, Qt.LeftButton)
            self.assertEqual(window.audio_combo.font().pointSize(), 13)
            QTest.keyClick(window, Qt.Key_Minus, Qt.ControlModifier)
            self.assertEqual(window.audio_combo.font().pointSize(), 12)
            QTest.keyClick(window, Qt.Key_Plus, Qt.ControlModifier)
            self.assertEqual(window.audio_combo.font().pointSize(), 13)
            for _ in range(10):
                QTest.mouseClick(window.increase_font_button, Qt.LeftButton)
            self.assertEqual(window.font().pointSize(), 20)
            self.assertFalse(window.increase_font_button.isEnabled())
            window.settings.sync()
            window.close()
            reopened = self.gui.MainWindow()
            reopened.show()
            self.addCleanup(reopened.close)
            reopened.activateWindow()
            self.app.processEvents()
            self.assertEqual(reopened.audio_combo.font().pointSize(), 20)
            for _ in range(12):
                QTest.mouseClick(reopened.decrease_font_button, Qt.LeftButton)
            self.assertEqual(reopened.font().pointSize(), 10)
            self.assertFalse(reopened.decrease_font_button.isEnabled())
            QTest.keyClick(reopened, Qt.Key_0, Qt.ControlModifier)
            self.assertEqual(reopened.font().pointSize(), 12)

    def test_tool_paths_follow_priority_from_packaged_executable(self):
        # Resolver rutas con espacios desde el ejecutable simulado
        # Aislar PATH y el entorno para no encontrar herramientas ajenas a esta prueba
        root = Path(self.temporary.name)
        app_dir = root / "Aplicación 10gato"
        name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        candidates = [root / "opción" / name, root / "entorno" / name,
                        app_dir / name, app_dir / "bin" / name, root / "PATH" / name]
        for path in candidates:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            path.chmod(0o755)
        explicit, environment, bundled, in_bin, in_path = candidates
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(app_dir / "10gato.exe")), \
                patch.dict(os.environ, {"TEN_GATO_FFMPEG": str(environment), "PATH": str(in_path.parent)}):
            resolve = self.gui.resolve_gui_tool
            self.assertEqual(resolve(str(explicit), "ffmpeg"), str(explicit))
            self.assertEqual(resolve(None, "ffmpeg"), str(environment))
            with self.assertRaisesRegex(compressor.CompressionError, "ffmpeg"):
                resolve(str(root / "ausente" / name), "ffmpeg")
            os.environ["TEN_GATO_FFMPEG"] = str(root / "ausente" / name)
            with self.assertRaisesRegex(compressor.CompressionError, "ffmpeg"):
                resolve(None, "ffmpeg")
            del os.environ["TEN_GATO_FFMPEG"]
            for expected in (bundled, in_bin, in_path):
                self.assertEqual(resolve(None, "ffmpeg"), str(expected))
                expected.unlink()
            with self.assertRaisesRegex(compressor.CompressionError, "ffmpeg"):
                resolve(None, "ffmpeg")

    @unittest.skipUnless(os.environ.get("TEN_GATO_FFMPEG") and os.environ.get("TEN_GATO_FFPROBE"), "Indicar herramientas para generar los clips de prueba")
    def test_only_conversion_requires_ffmpeg_and_new_file_clears_result(self):
        # Analizar con ffprobe y comprobar que solo una conversion necesita FFmpeg
        # Retirar la ruta de FFmpeg despues de generar los archivos de prueba
        ready = self.make_silent_source(2, padded=False)
        to_convert = self.make_silent_source(3)
        self.window.ffmpeg = str(Path(self.temporary.name) / "ffmpeg-ausente.exe")
        self.window.load_clip(ready)
        self.wait_until(lambda: self.window.worker is None)
        # Retirar tambien ffprobe tras analizar para comprobar que reutilizar solo necesita la ruta
        previous_probe = self.window.ffprobe
        self.window.ffprobe = str(Path(self.temporary.name) / "ffprobe-ausente.exe")
        try:
            QTest.mouseClick(self.window.start_button, Qt.LeftButton)
            self.wait_until(lambda: self.window.worker is None)
        finally:
            self.window.ffprobe = previous_probe
        self.assertEqual(self.window.result_path, ready)
        self.assertTrue(self.window.show_button.isVisible())
        self.window.load_clip(ready.with_name("ausente.mp4"))
        self.wait_until(lambda: self.window.worker is None)
        self.assertIsNone(self.window.result_path)
        self.assertFalse(self.window.show_button.isVisible())
        self.assertNotIn(ready.name, self.window.status_label.text())
        self.assertEqual(self.window.preview_label.text(), "")
        self.assertEqual(self.window.result_name.text(), "")
        self.assertEqual(self.window.info_label.text(), "Sin datos")
        self.window.load_clip(to_convert)
        self.wait_until(lambda: self.window.worker is None)
        QTest.mouseClick(self.window.start_button, Qt.LeftButton)
        self.wait_until(lambda: self.window.worker is None)
        self.assertIn("ffmpeg", self.window.status_label.text())
        self.assertTrue(self.window.select_button.isEnabled())
        self.assertFalse(self.window.show_button.isVisible())

    def make_silent_source(self, duration, rate=1, padded=True):
        # Crear una fuente real que requiere compresion para las pruebas de la ventana
        # Agregar relleno MP4 valido sin cambiar la duracion ni las pistas del contenido
        source = Path(self.temporary.name) / f"clip {duration}.mp4"
        subprocess.run([os.environ["TEN_GATO_FFMPEG"], "-v", "error", "-nostdin",
            "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={rate}:duration={duration}",
            "-c:v", "libx264", "-preset", "ultrafast", str(source)], check=True)
        if padded:
            with source.open("ab") as output:
                output.write(struct.pack(">I4s", 10_000_000, b"free"))
                output.write(b"\0" * 9_999_992)
        return source

    @unittest.skipUnless(os.environ.get("TEN_GATO_FFMPEG") and os.environ.get("TEN_GATO_FFPROBE"), "Indicar herramientas para probar el original listo")
    def test_long_ready_clip_uses_original_and_folder_button_selects_it(self):
        # Usar un clip largo que ya cabe sin convertir ni solicitar el aviso de calidad
        # Comprobar la accion de carpeta sin abrir Explorer durante la prueba
        source = self.make_silent_source(121, padded=False)
        self.window.load_clip(source)
        self.wait_until(lambda: self.window.worker is None)
        self.assertEqual(self.window.start_button.text(), "Usar original")
        QTest.mouseClick(self.window.start_button, Qt.LeftButton)
        self.wait_until(lambda: self.window.worker is None)
        self.assertEqual(self.window.result_path, source)
        self.assertEqual(list(source.parent.glob("*_10MB*.mp4")), [])
        if sys.platform == "win32":
            with patch("gui.subprocess.Popen") as explorer:
                QTest.mouseClick(self.window.show_button, Qt.LeftButton)
                explorer.assert_called_once_with(["explorer.exe", "/select,", str(source)])

    @unittest.skipUnless(os.environ.get("TEN_GATO_FFMPEG") and os.environ.get("TEN_GATO_FFPROBE"), "Indicar herramientas para probar reintentos desde la ventana")
    def test_window_delivers_only_after_two_size_retries(self):
        # Comprobar los reintentos a traves de la ventana con pistas y herramientas reales
        # Reutilizar el relleno MP4 de las regresiones del motor para provocar excesos estables
        from test_compressor import EncodingTests
        source = self.make_silent_source(1.25, 24)
        self.window.load_clip(source)
        self.wait_until(lambda: self.window.worker is None)
        messages = []
        existing = source.with_name(source.stem + "_10MB.mp4")
        existing.write_bytes(b"copia anterior")
        with EncodingTests.sized_attempts(self, [11_000_000, 10_000_001, 10_000_000]) as attempts:
            QTest.mouseClick(self.window.start_button, Qt.LeftButton)
            self.window.worker.progress.connect(messages.append)
            self.wait_until(lambda: self.window.worker is None, 20)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sum("Reintento" in message for message in messages), 2)
        self.assertEqual(self.window.result_path.stat().st_size, 10_000_000)
        self.assertEqual(self.window.result_path.name, source.stem + "_10MB_2.mp4")
        self.assertEqual(existing.read_bytes(), b"copia anterior")
        self.assertEqual(list(source.parent.glob(".10gato-*")), [])

    @unittest.skipUnless(os.environ.get("TEN_GATO_FFMPEG") and os.environ.get("TEN_GATO_FFPROBE"), "Indicar herramientas para probar cancelacion y cierre")
    def test_cancel_and_close_during_conversion_leave_no_output(self):
        # Comprobar cancelacion y cierre mientras el motor trabaja desde la ventana
        # Usar el mismo original real y verificar que ambos caminos limpian los temporales
        source = self.make_silent_source(10, 60)
        original = source.read_bytes()
        for close in (False, True):
            with self.subTest(close=close):
                self.window.load_clip(source)
                self.wait_until(lambda: self.window.worker is None)
                QTest.mouseClick(self.window.start_button, Qt.LeftButton)
                self.wait_until(lambda: "pasada" in self.window.status_label.text())
                if close:
                    self.window.close()
                else:
                    QTest.mouseClick(self.window.cancel_button, Qt.LeftButton)
                    self.assertFalse(self.window.cancel_button.isEnabled())
                self.wait_until(lambda: self.window.worker is None)
                self.assertEqual(list(source.parent.glob("*_10MB*.mp4")), [])
                self.assertEqual(list(source.parent.glob(".10gato-*")), [])
                self.assertEqual(source.read_bytes(), original)
        self.assertFalse(self.window.isVisible())

    @unittest.skipUnless(os.environ.get("TEN_GATO_FFMPEG") and os.environ.get("TEN_GATO_FFPROBE"), "Indicar herramientas para probar los avisos")
    def test_warning_threshold_and_both_choices_use_real_dialogs(self):
        # Comprobar el umbral y las dos decisiones del aviso en el bucle real de Qt
        # Pulsar los botones del dialogo automaticamente y dejar ejecutar el motor real
        real_exec = QMessageBox.exec
        for duration, accepted, expected_warnings in ((120, True, 0), (121, False, 1), (122, True, 1), (1000, True, 1)):
            with self.subTest(duration=duration, accepted=accepted):
                source = self.make_silent_source(duration)
                self.window.load_clip(source)
                self.wait_until(lambda: self.window.worker is None)
                warnings = []

                def answer_dialog(dialog):
                    # Responder al dialogo durante esta prueba sin sustituir su funcionamiento
                    # Registrar el aviso y comprobar que la opcion segura es la predeterminada
                    warnings.append(dialog.text())
                    self.assertEqual(dialog.defaultButton().text(), "Elegir otro clip")
                    label = "Comprimir de todos modos" if accepted else "Elegir otro clip"
                    button = next(button for button in dialog.buttons() if button.text() == label)
                    QTimer.singleShot(0, button.click)
                    return real_exec(dialog)

                with patch.object(QMessageBox, "exec", answer_dialog):
                    QTest.mouseClick(self.window.start_button, Qt.LeftButton)
                    self.wait_until(lambda: self.window.worker is None, 20)
                self.assertEqual(len(warnings), expected_warnings)
                self.assertEqual(self.window.show_button.isVisible(), accepted)
                if accepted:
                    self.assertTrue(self.window.result_path.is_file())
                else:
                    self.assertEqual(list(source.parent.glob(source.stem + "_10MB*.mp4")), [])

    def test_remote_or_multiple_drops_are_rejected(self):
        # Rechazar arrastres que no representan un unico archivo local
        # Usar URLs remotas y varias entradas sin iniciar un trabajador
        for urls in ([QUrl("https://example.com/clip.mp4")], [QUrl.fromLocalFile(__file__)] * 2):
            mime = QMimeData()
            mime.setUrls(urls)
            self.assertIsNone(self.window.dropped_path(mime))
        self.assertIsNone(self.window.worker)

    def test_audio_popup_highlights_tracks_without_selecting_until_confirmed(self):
        # Comprobar el contraste visible y la eleccion explicita en el desplegable
        # Usar dos pistas de prueba y eventos reales de la vista de Qt
        from test_compressor import sample_metadata

        path = Path(self.temporary.name) / "dos pistas.mp4"
        path.touch()
        self.window.media_loaded(compressor.parse_media(path, sample_metadata(audio_count=2)))
        combo = self.window.audio_combo
        QTest.mouseClick(combo, Qt.LeftButton)
        self.app.processEvents()
        view = combo.view()
        self.assertTrue(view.isVisible())
        self.assertTrue(view.viewport().rect().contains(view.visualRect(view.model().index(3, 0))))
        first_track = view.model().index(1, 0)
        QTest.mouseMove(view.viewport(), view.visualRect(first_track).center())
        self.wait_until(lambda: view.currentIndex() == first_track)
        self.assertIsNone(combo.currentData())

        row = view.viewport().grab(view.visualRect(first_track).adjusted(4, 2, -4, -2)).toImage()
        lightness = [row.pixelColor(x, y).lightness() for y in range(row.height()) for x in range(row.width())]
        self.assertGreater(sum(value > 170 for value in lightness), len(lightness) // 2)
        self.assertGreater(sum(value < 80 for value in lightness), 20)

        QTest.keyClick(view, Qt.Key_Down)
        self.assertEqual(view.currentIndex().row(), 2)
        self.assertIsNone(combo.currentData())
        QTest.keyClick(view, Qt.Key_Escape)
        self.assertFalse(view.isVisible())
        self.assertFalse(self.window.start_button.isEnabled())

        combo.showPopup()
        QTest.keyClick(view, Qt.Key_Home)
        QTest.keyClick(view, Qt.Key_Down)
        self.assertEqual(view.currentIndex(), first_track)
        QTest.keyClick(view, Qt.Key_Return)
        self.assertEqual(combo.currentData(), (1,))
        self.assertTrue(self.window.start_button.isEnabled())

        combo.showPopup()
        self.app.processEvents()
        second_track = view.model().index(2, 0)
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualRect(second_track).center())
        self.assertEqual(combo.currentData(), (2,))

    @unittest.skipUnless(os.environ.get("TEN_GATO_FFMPEG") and os.environ.get("TEN_GATO_FFPROBE"), "Indicar herramientas para probar la ventana con clips reales")
    def test_drop_requires_audio_choice_then_mixes_and_reveals_verified_output(self):
        # Comprobar arrastre, eleccion explicita y conversion desde la ventana
        # Usar dos tonos reales y forzar que copiar el video exceda el limite
        from test_compressor import EncodingTests
        source = Path(self.temporary.name) / "dos pistas.mp4"
        subprocess.run([os.environ["TEN_GATO_FFMPEG"], "-v", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24:duration=1.25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1.25",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=1.25",
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-c:a", "aac", str(source)], check=True)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source))])
        drag = QDragEnterEvent(self.window.rect().center().toPointF(), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        self.window.dragEnterEvent(drag)
        self.assertTrue(drag.isAccepted())
        drop = QDropEvent(self.window.rect().center().toPointF(), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        self.window.dropEvent(drop)
        self.wait_until(lambda: self.window.worker is None)
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertEqual(self.window.preview_label.text(), "")
        self.window.audio_combo.setCurrentIndex(1)
        self.assertIn("video sin recodificar", self.window.preview_label.text())
        self.window.audio_combo.setCurrentIndex(0)
        self.assertEqual(self.window.preview_label.text(), "")
        self.window.audio_combo.setCurrentIndex(self.window.audio_combo.count() - 1)
        self.assertTrue(self.window.start_button.isEnabled())
        tracks = self.window.audio_combo.currentData()
        with EncodingTests.sized_attempts(self, [11_000_000, 10_000_000]) as attempts:
            QTest.mouseClick(self.window.start_button, Qt.LeftButton)
            worker = self.window.worker
            self.assertFalse(self.window.select_button.isEnabled())
            self.assertEqual(self.window.preview_label.text(), "")
            QTest.mouseClick(self.window.increase_font_button, Qt.LeftButton)
            self.assertEqual(self.window.audio_combo.font().pointSize(), 13)
            self.assertIs(self.window.worker, worker)
            self.assertEqual(self.window.audio_combo.currentData(), tracks)
            self.assertTrue(self.window.cancel_button.isEnabled())
            self.wait_until(lambda: self.window.worker is None, 20)
        self.assertEqual(len(attempts), 2)
        self.assertNotIn("-b:v", attempts[0])
        self.assertIn("-b:v", attempts[1])
        self.assertEqual(self.window.preview_label.text(), "")
        self.assertIn("Copia verificada", self.window.status_label.text())
        self.assertEqual(self.window.result_name.text(), self.window.result_path.name)
        self.assertTrue(self.window.show_button.isVisible())
        self.assertEqual(self.window.progress_bar.value(), 100)
        self.assertTrue(self.window.result_path.is_file())
        self.assertLessEqual(self.window.result_path.stat().st_size, compressor.MAX_BYTES)
        self.assertEqual(list(source.parent.glob(".10gato-*")), [])

if __name__ == "__main__":
    unittest.main()
