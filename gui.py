"""Ventana de 10gato"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

from PySide6.QtCore import QSettings, Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QKeySequence, QPalette, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QComboBox, QDialogButtonBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QStyledItemDelegate, QVBoxLayout, QWidget,
)

import compressor

LONG_WARNING = ("Este clip dura más de 2 minutos. Para que el video completo quepa en 10 MB, "
                "puede perder bastante detalle")
ASSETS = Path(__file__).resolve().parent / "assets"
LOGO = ASSETS / "Julin_.png"
HEADER_MASCOT = ASSETS / "julin_(1).png"
FONT_SETTING = "appearance/fontSize"
DEFAULT_FONT_SIZE = 12
MIN_FONT_SIZE = 10
MAX_FONT_SIZE = 20
STATUS_COLORS = {"success": "#a9e6ac", "warning": "#f3d391", "error": "#ffacba"}

def resolve_gui_tool(explicit, name):
    # Buscar la herramienta cuando la operacion la necesita
    # Priorizar configuracion y archivos junto a la aplicacion antes del PATH
    variable = f"TEN_GATO_{name.upper()}"
    configured = explicit if explicit is not None else os.environ.get(variable)
    if configured is not None:
        if configured and (executable := shutil.which(str(Path(configured).expanduser().absolute()))):
            return executable
        source = f"--{name}" if explicit is not None else variable
        raise compressor.CompressionError(f"No se encontró {name} en la ruta indicada por {source}: {configured}")

    application_file = sys.executable if getattr(sys, "frozen", False) else __file__
    directory = Path(application_file).resolve().parent
    filename = f"{name}.exe" if sys.platform == "win32" else name
    for folder in (directory, directory / "bin"):
        if executable := shutil.which(str(folder / filename)):
            return executable
    for entry in os.get_exec_path():
        # Consultar rutas absolutas para que la carpeta del clip no afecte la busqueda
        folder = Path(entry.strip('"'))
        if folder.is_absolute() and (executable := shutil.which(str(folder / filename))):
            return executable
    raise compressor.CompressionError(
        f"No se encontró {name}. Comprueba los archivos de la aplicación. "
        f"En desarrollo, indica su ruta con --{name} o {variable}."
    )

def apply_theme(app: QApplication) -> None:
    # Aplicar el tema al arrancar la ventana y los dialogos de Qt
    # Usar Fusion y una paleta propia para no depender del tema de Windows
    app.setAttribute(Qt.AA_UseStyleSheetPropagationInWidgetStyles)
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in (
        (QPalette.Window, "#272936"), (QPalette.WindowText, "#e7e9f3"),
        (QPalette.Base, "#212330"), (QPalette.AlternateBase, "#2c2e3d"),
        (QPalette.Text, "#e7e9f3"), (QPalette.Button, "#2c2e3d"),
        (QPalette.ButtonText, "#e7e9f3"), (QPalette.Highlight, "#c9affb"),
        (QPalette.HighlightedText, "#212330"), (QPalette.ToolTipBase, "#2c2e3d"),
        (QPalette.ToolTipText, "#e7e9f3"), (QPalette.PlaceholderText, "#aeb3cb"),
    ):
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#9095ac"))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#9095ac"))
    app.setPalette(palette)
    app.setStyleSheet("""
        QMainWindow, QDialog, QScrollArea { background: #272936; }
        QLabel#muted { color: #b5bbd1; }
        QLabel#panelTitle { color: #c9affb; font-weight: bold; }
        QLabel#data, QLineEdit[readOnly="true"] { color: #a1dbe5; }
        QFrame#panel, QFrame#filePanel { background: #242634; border: 1px solid #55586d; }
        QFrame#filePanel { border-style: dashed; }
        QPushButton { padding: 5px 12px; border: 1px solid #62677f; background: #2c2e3d; }
        QPushButton:hover { background: #3c4054; }
        QPushButton#primary { background: #c9affb; color: #212330; font-weight: bold; border-color: #c9affb; }
        QPushButton#primary:hover { background: #dac7ff; }
        QPushButton:disabled, QPushButton#primary:disabled {
            background: #292b39; color: #9095ac; border-color: #494c62;
        }
        QComboBox, QLineEdit { padding: 4px 8px; background: #212330; border: 1px solid #62677f; }
        QListView#audioChoices {
            background: #272936; color: #e7e9f3;
            selection-background-color: #c9affb; selection-color: #212330;
        }
        QPushButton:focus, QPushButton#primary:focus, QComboBox:focus, QLineEdit:focus {
            border: 1px solid #e3ceff; background: #42425a;
        }
        QPushButton#primary:focus { background: #dac7ff; }
        QProgressBar { border: 1px solid #55586d; background: #212330; min-height: 7px; max-height: 7px; }
        QProgressBar::chunk { background: #a1dbe5; }
    """)

class OperationWorker(QThread):
    """Ejecutar analisis o conversion sin tocar widgets desde el hilo de trabajo."""

    inspected = Signal(object)
    converted = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)
    progress = Signal(str)
    confirmation_requested = Signal()

    def __init__(self, ffmpeg, ffprobe, *, path=None, media=None, plan=None):
        # Preparar un trabajo antes de que la ventana inicie el hilo
        # Recibir datos inmutables del motor y eventos propios para cancelar o responder
        super().__init__()
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe
        self.path, self.media, self.plan = path, media, plan
        self.cancel_event = threading.Event()
        self.decision_event = threading.Event()
        self.accepted = False

    def cancel(self):
        # Solicitar el cierre al pulsar Cancelar o cerrar la ventana
        # Despertar tambien una posible espera del aviso de duracion
        self.cancel_event.set()
        self.decision_event.set()

    def answer_warning(self, accepted):
        # Entregar la respuesta cuando el usuario cierra el aviso de duracion
        # Usar un evento para despertar el hilo sin bloquear el bucle de Qt
        self.accepted = accepted
        self.decision_event.set()

    def confirm_long(self):
        # Esperar una decision si el motor necesita el aviso de duracion
        # Emitir una senal para que el dialogo se abra exclusivamente en el hilo de la ventana
        self.decision_event.clear()
        self.accepted = False
        self.confirmation_requested.emit()
        while not self.decision_event.wait(0.1):
            compressor.check_cancelled()
        compressor.check_cancelled()
        return self.accepted

    def run(self):
        # Ejecutar el trabajo cuando Qt arranca el hilo
        # Asociar la cancelacion a todas las herramientas y verificaciones de esta operacion
        try:
            with compressor.cancellable_operation(self.cancel_event.is_set):
                if self.path is not None:
                    ffprobe = resolve_gui_tool(self.ffprobe, "ffprobe")
                    media = compressor.inspect_media(self.path, ffprobe)
                    compressor.check_cancelled()
                    self.inspected.emit(media)
                else:
                    ffmpeg = ffprobe = ""
                    if self.plan.mode != "ready":
                        ffmpeg = resolve_gui_tool(self.ffmpeg, "ffmpeg")
                        ffprobe = resolve_gui_tool(self.ffprobe, "ffprobe")
                    result = compressor.compress_clip(self.media, self.plan, ffmpeg, ffprobe,
                        confirm_long=self.confirm_long, report=self.progress.emit)
                    self.converted.emit(result)
        except compressor.CompressionCancelled as error:
            self.cancelled.emit(str(error))
        except Exception as error:
            self.failed.emit(str(error))

class MainWindow(QMainWindow):
    """Coordinar seleccion, decisiones del usuario y resultados del motor."""

    def __init__(self, ffmpeg=None, ffprobe=None):
        # Abrir la ventana con la preferencia de letra y las rutas explicitas
        # Dejar la busqueda de ejecutables al trabajador que los necesite
        super().__init__()
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe
        self.media = None
        self.plan = None
        self.worker = None
        self.result_path = None
        self.closing = False
        self.settings = QSettings("10gato", "10gato")
        self.setWindowTitle("10gato")
        self.setWindowIcon(QIcon(str(LOGO)))
        self.setAcceptDrops(True)
        self.build_widgets()

        size = self.settings.value(FONT_SETTING, DEFAULT_FONT_SIZE)
        try:
            size = int(str(size))
        except ValueError:
            size = DEFAULT_FONT_SIZE
        if not MIN_FONT_SIZE <= size <= MAX_FONT_SIZE:
            size = DEFAULT_FONT_SIZE

        self.apply_font_size(size)
        available = self.screen().availableGeometry()
        self.setMinimumWidth(min(390, available.width()))
        # Reservar espacio para el marco nativo al abrir en pantallas pequenas
        self.resize(min(900, available.width() - 16), min(700, available.height() - 40))
        self.update_actions()

    def build_widgets(self):
        # Distribuir cabecera y acciones fuera del area de desplazamiento
        # Mantener los mismos widgets al cambiar el ancho o la letra
        content = QWidget()
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setCentralWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.header_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self.header_layout.setSpacing(8)
        self.brand = QLabel()
        mascot = QPixmap(str(HEADER_MASCOT))
        if not mascot.isNull():
            # Conservar detalle en pantallas de alta densidad sin agrandar la cabecera
            mascot = mascot.scaled(320, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            mascot.setDevicePixelRatio(2)
            self.brand.setPixmap(mascot)
        self.header_layout.addWidget(self.brand, alignment=Qt.AlignLeft | Qt.AlignTop)
        self.header_layout.addStretch()
        self.font_controls = QWidget()
        font_layout = QHBoxLayout(self.font_controls)
        font_layout.setContentsMargins(0, 12, 16, 0)
        self.decrease_font_button = QPushButton("A−")
        self.decrease_font_button.setAccessibleName("Reducir tamaño de letra")
        self.decrease_font_button.setToolTip("Reducir tamaño de letra (Ctrl+-)")
        self.decrease_font_button.clicked.connect(lambda: self.change_font_size(self.font_size - 1))
        self.font_label = QLabel()
        self.font_label.setAlignment(Qt.AlignCenter)
        self.increase_font_button = QPushButton("A+")
        self.increase_font_button.setAccessibleName("Aumentar tamaño de letra")
        self.increase_font_button.setToolTip("Aumentar tamaño de letra (Ctrl++ o Ctrl+=)")
        self.increase_font_button.clicked.connect(lambda: self.change_font_size(self.font_size + 1))

        for widget in (self.decrease_font_button, self.font_label, self.increase_font_button):
            font_layout.addWidget(widget)

        self.header_layout.addWidget(self.font_controls, alignment=Qt.AlignRight | Qt.AlignTop)
        layout.addLayout(self.header_layout)

        # Mantener el margen de los paneles y dejar la imagen pegada a la esquina
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(16, 0, 16, 12)
        main_layout.setSpacing(10)
        layout.addLayout(main_layout, stretch=1)

        for keys, action in (
            (("Ctrl+-",), lambda: self.change_font_size(self.font_size - 1)),
            # Admitir el signo + con o sin Shift segun la distribucion del teclado
            (("Ctrl++", "Ctrl+Shift++", "Ctrl+=", "Ctrl+Shift+="),
                lambda: self.change_font_size(self.font_size + 1)),
            (("Ctrl+0",), lambda: self.change_font_size(DEFAULT_FONT_SIZE)),
        ):
            shortcut = QShortcut(self)
            shortcut.setKeys([QKeySequence(key) for key in keys])
            shortcut.activated.connect(action)

        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFocusPolicy(Qt.NoFocus)
        main_layout.addWidget(self.scroll, stretch=1)
        body = QWidget()
        self.scroll.setWidget(body)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(8)

        file_panel, file_layout = self.make_panel("Archivo", "filePanel")
        self.path_label = self.make_file_field("Archivo seleccionado")
        self.path_label.setPlaceholderText("Arrastra un clip aquí")
        file_layout.addWidget(self.path_label)
        self.select_button = QPushButton("Seleccionar")
        self.select_button.setAccessibleName("Seleccionar clip")
        self.select_button.clicked.connect(self.choose_clip)
        file_layout.addWidget(self.select_button, alignment=Qt.AlignLeft)
        body_layout.addWidget(file_panel)

        self.details_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self.details_layout.setSpacing(8)
        original, original_layout = self.make_panel("Original")
        self.info_label = self.make_message("Sin archivo", "data")
        original_layout.addWidget(self.info_label)
        original_layout.addStretch()
        self.details_layout.addWidget(original, 1)

        audio, audio_layout = self.make_panel("Audio y salida")
        # Usar los colores de la lista cuando se resalta una pista con el raton o el teclado
        self.audio_combo = QComboBox()
        self.audio_combo.view().setObjectName("audioChoices")
        self.audio_combo.setItemDelegate(QStyledItemDelegate(self.audio_combo.view()))
        self.audio_combo.setAccessibleName("Audio de la copia")
        self.audio_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.audio_combo.setPlaceholderText("Sin selección")
        self.audio_combo.currentIndexChanged.connect(self.audio_changed)
        audio_layout.addWidget(self.audio_combo)
        self.audio_hint = self.make_message("Mezcla solo señales independientes. Si una pista ya contiene juego y voz, elige esa pista.", "muted")
        self.audio_hint.hide()
        audio_layout.addWidget(self.audio_hint)
        self.preview_label = self.make_message("", "data")
        self.preview_label.hide()
        audio_layout.addWidget(self.preview_label)
        audio_layout.addStretch()
        self.details_layout.addWidget(audio, 1)
        body_layout.addLayout(self.details_layout, stretch=1)

        process, process_layout = self.make_panel("Proceso")
        self.status_label = self.make_message("En espera")
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        process_layout.addWidget(self.status_label)
        self.result_name = self.make_file_field("Archivo de salida")
        self.result_name.hide()
        process_layout.addWidget(self.result_name)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setAccessibleName("Progreso de la operación")
        process_layout.addWidget(self.progress_bar)
        body_layout.addWidget(process)

        self.actions_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self.actions_layout.setSpacing(8)
        self.show_button = QPushButton("Mostrar en carpeta")
        self.show_button.clicked.connect(self.show_in_folder)
        self.show_button.hide()
        self.actions_layout.addWidget(self.show_button)
        self.actions_layout.addStretch()
        self.conversion_actions = QBoxLayout(QBoxLayout.LeftToRight)
        self.conversion_actions.setSpacing(8)
        self.actions_layout.addLayout(self.conversion_actions)
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.clicked.connect(self.cancel_operation)
        self.conversion_actions.addWidget(self.cancel_button)
        self.start_button = QPushButton("Comprimir")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_conversion)
        self.conversion_actions.addWidget(self.start_button)
        main_layout.addLayout(self.actions_layout)

    def make_panel(self, title, name="panel"):
        # Crear un panel rectangular con su titulo al montar la ventana
        # Devolver el layout para que cada panel aloje sus propios controles
        panel = QFrame()
        panel.setObjectName(name)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        layout.addWidget(QLabel(title, objectName="panelTitle"))
        return panel, layout

    def make_message(self, text, name=""):
        # Crear texto plano que se ajuste al ancho del panel
        # Evitar que los mensajes largos fijen el ancho de la ventana
        label = QLabel(text, objectName=name)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        return label

    def make_file_field(self, accessible_name):
        # Mostrar un nombre seleccionable al cargar un archivo o recibir el resultado
        # Usar una linea desplazable para nombres largos sin espacios
        field = QLineEdit()
        field.setReadOnly(True)
        field.setAccessibleName(accessible_name)
        field.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        return field

    def apply_font_size(self, size):
        # Aplicar la letra al abrir o cambiar la preferencia
        # Usar la fuente de la aplicacion para que la hereden los dialogos de Qt
        fallback = QFontDatabase.systemFont(QFontDatabase.FixedFont).family()
        font = QFont()
        font.setFamilies(["Cascadia Mono", "Consolas", fallback])
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(size)
        QApplication.instance().setFont(font)
        self.setFont(font)
        self.font_size = size
        self.font_label.setText(f"{size} pt")
        for button, opposite, limit in (
            (self.decrease_font_button, self.increase_font_button, MIN_FONT_SIZE),
            (self.increase_font_button, self.decrease_font_button, MAX_FONT_SIZE),
        ):
            if size == limit and button.hasFocus():
                # Mover el foco antes de desactivar el boton para seguir ajustando la letra
                opposite.setEnabled(True)
                opposite.setFocus()
            button.setEnabled(size != limit)
        self.adapt_layouts()

    def change_font_size(self, size):
        # Guardar el ajuste al pulsar un control o atajo
        # Respetar los limites sin tocar los datos del clip ni el trabajador
        size = min(MAX_FONT_SIZE, max(MIN_FONT_SIZE, size))
        if size != self.font_size:
            self.apply_font_size(size)
            self.settings.setValue(FONT_SETTING, size)

    def adapt_layouts(self):
        # Redistribuir los controles al cambiar la ventana, la fuente o las acciones
        # Comparar el espacio disponible con lo que necesitan sus textos
        width = self.width() - 32
        header_width = self.brand.sizeHint().width() + self.font_controls.sizeHint().width() + 16
        self.header_layout.setDirection(
            QBoxLayout.LeftToRight if self.width() >= header_width else QBoxLayout.TopToBottom
        )
        conversion_width = self.cancel_button.sizeHint().width() + self.start_button.sizeHint().width() + 8
        actions_width = conversion_width
        if not self.show_button.isHidden():
            actions_width += self.show_button.sizeHint().width() + 8
        panels_width = 2 * (self.fontMetrics().horizontalAdvance("0" * 25) + 26)
        panels_width += 8 + self.scroll.verticalScrollBar().sizeHint().width()
        for layout, needed in ((self.details_layout, panels_width),
                                (self.actions_layout, actions_width),
                                (self.conversion_actions, conversion_width)):
            direction = QBoxLayout.LeftToRight if width >= needed else QBoxLayout.TopToBottom
            layout.setDirection(direction)

    def resizeEvent(self, event):
        # Ajustar las filas al cambiar el tamano disponible de la ventana
        # Esperar a que Qt haya aplicado la nueva geometria
        super().resizeEvent(event)
        self.adapt_layouts()

    def set_status(self, message, tone=None):
        # Mostrar una etapa, un resultado o un error en el panel de proceso
        # Acompanar el texto con un color cuando indica un estado especial
        self.status_label.setText(message)
        color = STATUS_COLORS.get(tone)
        self.status_label.setStyleSheet(f"color: {color};" if color else "")

    def set_preview(self, text):
        # Mostrar la prevision solo cuando hay un plan antes de convertir
        # Quitar su espacio cuando la etapa real o el resultado la sustituyen
        self.preview_label.setText(text)
        self.preview_label.setVisible(bool(text))

    def update_actions(self):
        # Ajustar las acciones al cambiar la seleccion o el estado de trabajo
        # Impedir otro archivo o conversion mientras exista un trabajador activo
        busy = self.worker is not None
        self.select_button.setEnabled(not busy)
        self.audio_combo.setEnabled(not busy and self.media is not None and bool(self.media.audio))
        self.cancel_button.setEnabled(busy and not self.worker.cancel_event.is_set())
        self.start_button.setEnabled(not busy and self.plan is not None)
        self.show_button.setEnabled(not busy)
        self.adapt_layouts()

    def choose_clip(self):
        # Pedir un archivo al pulsar Seleccionar clip
        # Usar el dialogo nativo de Windows con los filtros de la aplicacion
        path, _ = QFileDialog.getOpenFileName(
            self, "Seleccionar clip", filter="Videos (*.mp4 *.mkv *.mov *.avi *.webm);;Todos (*)",
            options=QFileDialog.HideNameFilterDetails,
        )
        if path:
            self.load_clip(Path(path))

    def load_clip(self, path):
        # Analizar el clip elegido por boton o arrastre
        # Limpiar el resultado anterior y delegar ffprobe en un hilo de trabajo
        if self.worker is not None:
            return
        self.media = None
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        self.audio_combo.blockSignals(False)
        self.audio_changed()
        self.audio_hint.hide()
        self.path_label.setText(Path(path).name)
        self.path_label.setCursorPosition(0)
        self.path_label.setToolTip(str(path))
        self.info_label.setText("Sin datos")
        self.begin_worker(OperationWorker(self.ffmpeg, self.ffprobe, path=Path(path)), "Analizando el clip…")

    def begin_worker(self, worker, message):
        # Conectar el trabajador antes de comenzar cualquier operacion asincrona
        # Mantener su referencia hasta finished para que Qt no destruya un hilo activo
        self.worker = worker
        worker.inspected.connect(self.media_loaded)
        worker.converted.connect(self.conversion_done)
        worker.failed.connect(self.operation_failed)
        worker.cancelled.connect(self.operation_cancelled)
        worker.progress.connect(self.show_progress)
        worker.confirmation_requested.connect(self.ask_long_warning)
        worker.finished.connect(self.worker_finished)
        self.set_status(message)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.update_actions()
        worker.start()

    def show_progress(self, message):
        # Mostrar las etapas recibidas del motor durante la operacion
        # Conservar el aviso de cancelacion mientras termina la limpieza
        if self.worker is not None and not self.worker.cancel_event.is_set():
            self.set_status(message)

    def media_loaded(self, media):
        # Mostrar los datos cuando el analisis termina correctamente
        # Conservar los indices globales del motor y exigir eleccion si hay varias pistas
        self.media = media
        self.info_label.setText(f"Duración  {media.duration:.3f} s\n"
                                f"Tamaño    {media.size / 1_000_000:.2f} MB\n"
                                f"Video     {media.width} × {media.height}\n"
                                f"FPS       {float(media.fps):.2f}")
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        if not media.audio:
            self.audio_combo.addItem("Sin audio", ())
        else:
            if len(media.audio) > 1:
                self.audio_combo.addItem("Elige una pista", None)
            for track in media.audio:
                self.audio_combo.addItem(f"Pista {track.index} · {track.codec}", (track.index,))
            if len(media.audio) > 1:
                self.audio_combo.addItem("Mezclar todas", tuple(track.index for track in media.audio))
        self.audio_combo.setCurrentIndex(0)
        self.audio_combo.blockSignals(False)
        self.audio_hint.setVisible(len(media.audio) > 1)
        self.audio_changed()

    def audio_changed(self):
        # Actualizar la accion y la explicacion al elegir audio
        # Calcular el plan con la seleccion explicita sin ejecutar herramientas
        self.result_path = None
        self.result_name.clear()
        self.result_name.setToolTip("")
        self.result_name.hide()
        self.progress_bar.setValue(0)
        self.show_button.hide()
        self.plan = None
        self.set_preview("")
        self.start_button.setText("Comprimir")
        if self.media is not None:
            tracks = self.audio_combo.currentData()
            if tracks is None:
                self.set_status("Selecciona una pista o la mezcla.", "warning")
            else:
                try:
                    self.plan = compressor.make_plan(self.media, tracks)
                except compressor.CompressionError as error:
                    self.set_status(str(error), "error")
                else:
                    self.show_preview()
                    if self.plan.mode == "ready":
                        self.set_status("El original cumple el límite de 10 MB.", "success")
                    else:
                        self.set_status("Análisis completo")
        self.update_actions()

    def show_preview(self):
        # Presentar el plan al analizar el clip o cambiar de audio
        # Distinguir el original, la preparacion de audio y la compresion de video
        plan = self.plan
        if plan.mode == "ready":
            self.start_button.setText("Usar original")
            self.set_preview("Salida prevista: archivo original")
        elif plan.mode == "copy_video":
            self.set_preview("Salida prevista: video sin recodificar · audio AAC")
        else:
            self.set_preview(f"Salida prevista: {plan.width} × {plan.height} · {float(plan.fps):g} FPS")

    def start_conversion(self):
        # Iniciar la conversion al pulsar la accion principal
        # Usar el plan de la seleccion actual y dejar el aviso largo al motor
        if self.worker is not None or self.plan is None:
            return
        self.result_path = None
        self.result_name.hide()
        self.show_button.hide()
        self.set_preview("")
        message = "Localizando el original…" if self.plan.mode == "ready" else "Iniciando conversión…"
        self.begin_worker(OperationWorker(self.ffmpeg, self.ffprobe, media=self.media, plan=self.plan), message)
        QTimer.singleShot(0, self, lambda: self.scroll.ensureWidgetVisible(self.status_label, 0, 6))

    def ask_long_warning(self):
        # Mostrar el aviso cuando lo solicita el motor desde el trabajador
        # Responder en el hilo de Qt sin ejecutar widgets desde el hilo de conversion
        worker = self.worker
        if worker is None or worker.cancel_event.is_set():
            return
        dialog = QMessageBox(QMessageBox.Warning, "Clip de más de 2 minutos", LONG_WARNING, parent=self)
        proceed = dialog.addButton("Comprimir de todos modos", QMessageBox.AcceptRole)
        other = dialog.addButton("Elegir otro clip", QMessageBox.RejectRole)
        dialog.setDefaultButton(other)
        buttons = dialog.findChild(QDialogButtonBox)
        available_width = self.screen().availableGeometry().width() - 40
        if buttons.sizeHint().width() > available_width:
            buttons.setOrientation(Qt.Vertical)
        if proceed.sizeHint().width() > available_width:
            proceed.setText("Comprimir de todos\nmodos")
        dialog.exec()
        worker.answer_warning(dialog.clickedButton() == proceed)

    def conversion_done(self, result):
        # Habilitar la ubicacion solo tras recibir un resultado aceptado por el motor
        # Conservar la ruta del original si ya estaba listo o la copia verificada
        self.result_path = result.path
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.progress_bar.hide()
        label = "Original reutilizado" if result.reused_original else "Copia verificada"
        self.set_status(f"{label} · {result.path.stat().st_size / 1_000_000:.2f} MB", "success")
        self.result_name.setText(result.path.name)
        self.result_name.setCursorPosition(0)
        self.result_name.setToolTip(str(result.path))
        self.result_name.show()
        self.show_button.show()

    def operation_failed(self, message):
        # Explicar un fallo cuando el trabajador no puede terminar
        # Mostrar el motivo para que se pueda corregir o elegir otro clip
        self.set_status("No se pudo completar: " + message, "error")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

    def operation_cancelled(self, message):
        # Mostrar la cancelacion solo cuando el motor ya ha limpiado sus temporales
        # Conservar el clip seleccionado para que se pueda cambiar o volver a intentar
        self.set_status(message, "warning")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

    def worker_finished(self):
        # Liberar la referencia cuando Qt confirma que termino el hilo
        # Restaurar las acciones y cerrar si el usuario habia solicitado salir
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
        self.update_actions()
        if self.closing:
            self.close()
        elif self.result_path is not None:
            QTimer.singleShot(0, self, self.reveal_result)
        elif worker is not None and (worker.path is None or self.media is None):
            if self.plan is not None:
                self.show_preview()
            QTimer.singleShot(0, self, lambda: self.scroll.ensureWidgetVisible(self.status_label, 0, 6))

    def reveal_result(self):
        # Desplazar al panel final cuando la nueva fila de acciones ya esta visible
        # Mostrar juntos el estado y el nombre del resultado
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def cancel_operation(self):
        # Solicitar cancelacion sin esperar en el hilo de la ventana
        # El trabajador termina el subproceso y limpia los temporales antes de emitir finished
        if self.worker is not None:
            self.worker.cancel()
            self.set_status("Cancelando y limpiando temporales…", "warning")
            self.update_actions()

    def closeEvent(self, event):
        # Aplazar el cierre si todavia hay analisis o conversion activos
        # Cancelar y esperar a finished sin destruir el hilo ni bloquear Qt
        if self.worker is not None:
            self.closing = True
            self.cancel_operation()
            event.ignore()
        else:
            event.accept()

    def dropped_path(self, mime):
        # Validar el arrastre antes de aceptar o cargar un archivo
        # Admitir un solo archivo local y rechazar carpetas o enlaces remotos
        urls = mime.urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            path = Path(urls[0].toLocalFile())
            if path.is_file():
                return path
        return None

    def dragEnterEvent(self, event):
        # Aceptar un arrastre valido mientras la ventana esta libre
        # Consultar el mismo criterio que se aplicara al soltar el archivo
        if self.worker is None and self.dropped_path(event.mimeData()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        # Cargar el archivo al soltarlo sobre la ventana disponible
        # Revalidar el contenido del arrastre para no procesar carpetas ni varios clips
        path = self.dropped_path(event.mimeData())
        if self.worker is None and path is not None:
            event.acceptProposedAction()
            self.load_clip(path)
        else:
            event.ignore()

    def show_in_folder(self):
        # Localizar el resultado al pulsar Mostrar en carpeta
        # Seleccionar el archivo con Explorer en Windows y abrir su carpeta en otros sistemas
        if self.result_path is None:
            return
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer.exe", "/select,", str(self.result_path)])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.result_path.parent)))
        except OSError as error:
            self.set_status(f"No se pudo abrir la carpeta: {error}", "error")

def main(argv=None):
    # Abrir la interfaz al ejecutar gui.py desde el entorno virtual
    # Admitir rutas de herramientas y un clip opcional conservando separada la consola existente
    parser = argparse.ArgumentParser(description="Ventana de 10gato")
    parser.add_argument("clip", type=Path, nargs="?")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("10gato")
    apply_theme(app)
    window = MainWindow(args.ffmpeg, args.ffprobe)
    window.show()
    if args.clip:
        QTimer.singleShot(0, lambda: window.load_clip(args.clip))
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
