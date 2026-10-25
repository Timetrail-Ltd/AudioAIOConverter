"""Main GUI window for the audio converter."""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtCore import QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QLabel, QLineEdit, QPushButton,
    QComboBox, QSpinBox, QCheckBox, QProgressBar, QMessageBox,
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .converter import ConversionSettings, convert_audio, ConversionError, ConversionCancelled
from .ffmpeg_utils import probe_media_info, get_supported_output_formats
from .converter import (
    ConversionCancelled,
    ConversionError,
    ConversionSettings,
    convert_audio,
    format_duration,
    parse_timecode,
)
from .ffmpeg_utils import (
    find_ffplay,
    get_supported_input_formats,
    get_supported_output_formats,
    probe_media_info,
)
from .metadata import read_metadata


LOSSY_FORMATS = {"mp3", "aac", "m4a", "ogg", "opus"}
LOSSLESS_FORMATS = {"flac", "wav", "aiff"}

BUILTIN_PRESETS = {
    "Custom": None,
    "MP3 – 192 kbps (Standard)": {
        "format": "mp3",
        "bitrate": "192",
        "sample_rate": "Keep source",
        "channels": "Keep source",
        "vbr": False,
    },
    "MP3 – 320 kbps (High Quality)": {
        "format": "mp3",
        "bitrate": "320",
        "sample_rate": "Keep source",
        "channels": "Keep source",
        "vbr": False,
    },
    "MP3 – VBR V2 (Transparent)": {
        "format": "mp3",
        "bitrate": "192",
        "sample_rate": "Keep source",
        "channels": "Keep source",
        "vbr": True,
        "vbr_quality": 2,
    },
    "FLAC – Lossless (16-bit)": {
        "format": "flac",
        "sample_rate": "Keep source",
        "channels": "Keep source",
        "bit_depth": "16",
        "flac_compression": 5,
    },
    "FLAC – Studio (24-bit)": {
        "format": "flac",
        "sample_rate": "Keep source",
        "channels": "Keep source",
        "bit_depth": "24",
        "flac_compression": 8,
    },
    "WAV – Uncompressed (16-bit PCM)": {
        "format": "wav",
        "sample_rate": "Keep source",
        "channels": "Keep source",
        "bit_depth": "16",
    },
    "M4A / AAC – 256 kbps": {
        "format": "m4a",
        "bitrate": "256",
        "sample_rate": "Keep source",
        "channels": "Keep source",
    },
    "Opus – 128 kbps (Streaming)": {
        "format": "opus",
        "bitrate": "128",
        "sample_rate": "48000",
        "channels": "Keep source",
    },
    "Voice / Podcast – MP3 Mono 96k": {
        "format": "mp3",
        "bitrate": "128",
        "sample_rate": "44100",
        "channels": "Mono",
        "vbr": False,
    },
}


def _default_config_dir() -> Path:
    env_dir = os.environ.get("AUDIOCONVERTER_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "AudioAIOConverter"


def _format_size(num_bytes: int) -> str:
    if not num_bytes or num_bytes <= 0:
        return "0 B"
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.2f} MB"


@dataclass
class QueueItem:
    path: str
    root_dir: Optional[str] = None
    status: str = "Ready"
    progress: int = 0
    output_path: str = ""
    error_msg: str = ""


class ConversionWorker(QThread):
    """Background worker for single or batch audio conversion."""

    progress = Signal(int)
    file_started = Signal(int, str)
    file_progress = Signal(int, int)
    file_finished = Signal(int, str)
    file_skipped = Signal(int, str)
    file_error = Signal(int, str)
    overall_progress = Signal(int)
    finished = Signal(str)
    error = Signal(str)
    cancelled = Signal()

    def __init__(self, jobs: list[ConversionSettings] | ConversionSettings):
        super().__init__()
        if isinstance(jobs, ConversionSettings):
            self.jobs = [jobs]
        else:
            self.jobs = list(jobs)
        self.settings = self.jobs[0] if self.jobs else None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        total = len(self.jobs)
        if total == 0:
            self.finished.emit("")
            return

        last_output = ""
        errors: list[tuple[str, str]] = []

        for idx, job in enumerate(self.jobs):
            if self._cancelled:
                self.cancelled.emit()
                return

            self.file_started.emit(idx, job.input_path)

            def progress_cb(value: float, _idx: int = idx):
                pct = int(value * 100)
                self.file_progress.emit(_idx, pct)
                overall = int(((_idx + value) / total) * 100)
                self.overall_progress.emit(overall)
                self.progress.emit(overall if total > 1 else pct)

            def cancel_cb() -> bool:
                return self._cancelled

            try:
                output = convert_audio(
                    job,
                    progress_callback=progress_cb,
                    cancel_flag=cancel_cb,
                )
                last_output = output
                self.file_progress.emit(idx, 100)
                self.file_finished.emit(idx, output)
                overall_done = int(((idx + 1) / total) * 100)
                self.overall_progress.emit(overall_done)
                self.progress.emit(overall_done)
            except ConversionCancelled:
                self.cancelled.emit()
                return
            except FileExistsError as e:
                self.file_skipped.emit(idx, str(e))
                overall_done = int(((idx + 1) / total) * 100)
                self.overall_progress.emit(overall_done)
                self.progress.emit(overall_done)
            except Exception as e:
                msg = str(e)
                errors.append((job.input_path, msg))
                self.file_error.emit(idx, msg)
                overall_done = int(((idx + 1) / total) * 100)
                self.overall_progress.emit(overall_done)
                self.progress.emit(overall_done)

        if errors:
            if total == 1:
                self.error.emit(errors[0][1])
            else:
                summary = "\n\n".join(
                    f"[{Path(p).name}]\n{m}" for p, m in errors
                )
                self.error.emit(summary)
        else:
            self.finished.emit(last_output)


class CopyableErrorDialog(QDialog):
    """Dialog that displays an error message in a read-only text box with a Copy button."""

    def __init__(self, parent: QWidget, title: str, message: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(520, 280)
        layout = QVBoxLayout(self)

        lbl = QLabel("Conversion encountered error(s). You can copy the details below:")
        layout.addWidget(lbl)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setPlainText(message)
        layout.addWidget(self.text_edit)

        btn_layout = QHBoxLayout()
        self.copy_btn = QPushButton("Copy to Clipboard")
        self.copy_btn.clicked.connect(self._copy)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.copy_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

    def _copy(self):
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.text_edit.toPlainText())
        self.copy_btn.setText("Copied!")


class FileQueueTable(QTableWidget):
    """Table showing queued files with drag-and-drop support."""

    paths_dropped = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels(["File", "Info", "Status", "Output"])
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setMinimumHeight(140)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            if paths:
                self.paths_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class MainWindow(QMainWindow):
    def __init__(self, load_saved_state: Optional[bool] = None):
        super().__init__()
        self.setWindowTitle("AudioAIOConverter")
        self.setMinimumSize(600, 500)
        self.setMinimumSize(720, 660)
        self.setAcceptDrops(True)

        self.queue_items: list[QueueItem] = []
        self.custom_presets: dict[str, dict] = {}
        self.last_error_text: str = ""
        self._preview_process: Optional[subprocess.Popen] = None
        self._applying_preset = False

        if load_saved_state is None:
            load_saved_state = "PYTEST_CURRENT_TEST" not in os.environ
        self._persist_enabled = load_saved_state

        central = QWidget()
        self.setCentralWidget(central)
        outer_layout = QVBoxLayout(central)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        layout = QVBoxLayout(scroll_content)
        scroll.setWidget(scroll_content)
        outer_layout.addWidget(scroll)

        # --- Input / Queue Section ---
        input_group = QGroupBox("Input Files (Drag && Drop files or folders)")
        input_vlayout = QVBoxLayout(input_group)

        top_input_row = QHBoxLayout()
        self.input_path_edit = QLineEdit()
        self.input_path_edit.setReadOnly(True)
        self.input_path_edit.setPlaceholderText("Select or drop audio files / folders...")
        self.input_path_edit.textChanged.connect(self._on_input_path_text_changed)

        self.browse_btn = QPushButton("Add Files...")
        self.browse_btn.clicked.connect(self.browse_input)
        self.add_folder_btn = QPushButton("Add Folder...")
        self.add_folder_btn.clicked.connect(self.browse_input_folder)
        self.remove_selected_btn = QPushButton("Remove Selected")
        self.remove_selected_btn.clicked.connect(self.remove_selected_files)
        self.clear_queue_btn = QPushButton("Clear List")
        self.clear_queue_btn.clicked.connect(self.clear_queue)

        top_input_row.addWidget(self.input_path_edit, stretch=1)
        top_input_row.addWidget(self.browse_btn)
        top_input_row.addWidget(self.add_folder_btn)
        top_input_row.addWidget(self.remove_selected_btn)
        top_input_row.addWidget(self.clear_queue_btn)
        input_vlayout.addLayout(top_input_row)

        self.file_table = FileQueueTable()
        self.file_table.paths_dropped.connect(self.add_paths)
        self.file_table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.file_table.cellDoubleClicked.connect(self._on_table_cell_double_clicked)
        input_vlayout.addWidget(self.file_table)

        # Detailed input info panel
        info_grid = QGridLayout()
        self.info_labels: dict[str, QLabel] = {}
        info_fields = [
            ("format", "Format:"),
            ("duration", "Duration:"),
            ("size", "Size:"),
            ("sample_rate", "Sample Rate:"),
            ("channels", "Channels:"),
            ("bit_depth", "Bit Depth:"),
        ]
        for idx, (key, title) in enumerate(info_fields):
            r, c = divmod(idx, 3)
            lbl_title = QLabel(f"<b>{title}</b>")
            lbl_val = QLabel("-")
            info_grid.addWidget(lbl_title, r, c * 2)
            info_grid.addWidget(lbl_val, r, c * 2 + 1)
            self.info_labels[key] = lbl_val
        input_vlayout.addLayout(info_grid)
        layout.addWidget(input_group)

        # --- Preset & Format Row ---
        fmt_group = QGroupBox("Format && Preset")
        fmt_grid = QGridLayout(fmt_group)

        fmt_grid.addWidget(QLabel("Preset:"), 0, 0)
        self.preset_combo = QComboBox()
        self._rebuild_preset_combo()
        self.preset_combo.currentTextChanged.connect(self._on_preset_selected)
        fmt_grid.addWidget(self.preset_combo, 0, 1)

        self.save_preset_btn = QPushButton("Save Profile...")
        self.save_preset_btn.clicked.connect(self.save_custom_preset)
        fmt_grid.addWidget(self.save_preset_btn, 0, 2)

        self.delete_preset_btn = QPushButton("Delete Profile")
        self.delete_preset_btn.clicked.connect(self.delete_custom_preset)
        fmt_grid.addWidget(self.delete_preset_btn, 0, 3)

        fmt_grid.addWidget(QLabel("Output Format:"), 1, 0)
        self.format_combo = QComboBox()
        self.format_combo.addItems(get_supported_output_formats())
        self.format_combo.currentTextChanged.connect(self._update_format_dependent_controls)
        fmt_grid.addWidget(self.format_combo, 1, 1, 1, 3)
        layout.addWidget(fmt_group)

        # --- Output Destination & Naming ---
        out_group = QGroupBox("Output && File Handling")
        out_grid = QGridLayout(out_group)

        self.same_as_source_check = QCheckBox("Save in same folder as source file")
        self.same_as_source_check.toggled.connect(self._on_same_as_source_toggled)
        out_grid.addWidget(self.same_as_source_check, 0, 0, 1, 2)

        self.preserve_subfolders_check = QCheckBox("Keep subfolder structure (when adding folders)")
        self.preserve_subfolders_check.setChecked(True)
        out_grid.addWidget(self.preserve_subfolders_check, 0, 2, 1, 2)

        out_grid.addWidget(QLabel("Output Folder:"), 1, 0)
        self.output_folder_edit = QLineEdit()
        self.out_browse_btn = QPushButton("Browse...")
        self.out_browse_btn.clicked.connect(self.browse_output_folder)
        self.open_output_btn = QPushButton("Open Folder")
        self.open_output_btn.clicked.connect(self.open_output_folder)
        out_grid.addWidget(self.output_folder_edit, 1, 1)
        out_grid.addWidget(self.out_browse_btn, 1, 2)
        out_grid.addWidget(self.open_output_btn, 1, 3)

        out_grid.addWidget(QLabel("Name Suffix:"), 2, 0)
        self.name_suffix_edit = QLineEdit()
        self.name_suffix_edit.setPlaceholderText("e.g. _converted (optional)")
        out_grid.addWidget(self.name_suffix_edit, 2, 1)

        out_grid.addWidget(QLabel("If Output Exists:"), 2, 2)
        self.overwrite_combo = QComboBox()
        self.overwrite_combo.addItems(["Rename", "Overwrite", "Skip"])
        out_grid.addWidget(self.overwrite_combo, 2, 3)
        layout.addWidget(out_group)

        # --- Main Audio Settings ---
        settings_group = QGroupBox("Audio Settings")
        sgrid = QGridLayout(settings_group)

        sgrid.addWidget(QLabel("Bitrate (kbps):"), 0, 0)
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(["Keep source", "128", "192", "256", "320"])
        self.bitrate_combo.setCurrentIndex(2)
        sgrid.addWidget(self.bitrate_combo, 0, 1)

        sgrid.addWidget(QLabel("Sample Rate (Hz):"), 1, 0)
        self.sample_rate_combo = QComboBox()
        self.sample_rate_combo.addItems(["Keep source", "22050", "44100", "48000", "96000"])
        self.sample_rate_combo.setCurrentIndex(3)
        # Default to "Keep source" so we never resample unexpectedly
        self.sample_rate_combo.setCurrentIndex(0)
        sgrid.addWidget(self.sample_rate_combo, 1, 1)

        sgrid.addWidget(QLabel("Channels:"), 2, 0)
        self.channels_combo = QComboBox()
        self.channels_combo.addItems(["Keep source", "Mono", "Stereo"])
        sgrid.addWidget(self.channels_combo, 2, 1)
        layout.addWidget(settings_group)

        # --- Collapsible Advanced Section ---
        self.adv_toggle_btn = QToolButton()
        self.adv_toggle_btn.setText("Advanced Settings (Codec, Trim && Preview, Metadata)")
        self.adv_toggle_btn.setCheckable(True)
        self.adv_toggle_btn.setChecked(False)
        self.adv_toggle_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.adv_toggle_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.adv_toggle_btn.toggled.connect(self._toggle_advanced_section)
        layout.addWidget(self.adv_toggle_btn)

        self.adv_container = QWidget()
        adv_container_layout = QVBoxLayout(self.adv_container)
        adv_container_layout.setContentsMargins(0, 0, 0, 0)

        # Codec-specific advanced settings
        adv_group = QGroupBox("Codec && Quality Options")
        agrid = QGridLayout(adv_group)

        agrid.addWidget(QLabel("VBR (MP3 / OGG):"), 0, 0)
        self.vbr_check = QCheckBox()
        self.vbr_check.toggled.connect(self._update_format_dependent_controls)
        agrid.addWidget(self.vbr_check, 0, 1)

        agrid.addWidget(QLabel("VBR Quality (0-9):"), 1, 0)
        self.vbr_quality = QSpinBox()
        self.vbr_quality.setRange(0, 9)
        self.vbr_quality.setValue(4)
        agrid.addWidget(self.vbr_quality, 1, 1)

        agrid.addWidget(QLabel("Bit Depth:"), 2, 0)
        self.bit_depth_combo = QComboBox()
        self.bit_depth_combo.addItems(["Default", "16", "24", "32"])
        agrid.addWidget(self.bit_depth_combo, 2, 1)

        agrid.addWidget(QLabel("FLAC Compression (0-12):"), 3, 0)
        self.flac_compression = QSpinBox()
        self.flac_compression.setRange(0, 12)
        self.flac_compression.setValue(5)
        agrid.addWidget(self.flac_compression, 3, 1)

        adv_container_layout.addWidget(adv_group)

        # Trim & Preview section
        trim_group = QGroupBox("Trim && Preview")
        tgrid = QGridLayout(trim_group)
        tgrid.addWidget(QLabel("Start Time:"), 0, 0)
        self.trim_start_edit = QLineEdit()
        self.trim_start_edit.setPlaceholderText("00:00 (or seconds)")
        tgrid.addWidget(self.trim_start_edit, 0, 1)

        tgrid.addWidget(QLabel("End Time:"), 0, 2)
        self.trim_end_edit = QLineEdit()
        self.trim_end_edit.setPlaceholderText("End (MM:SS or seconds)")
        tgrid.addWidget(self.trim_end_edit, 0, 3)

        self.preview_btn = QPushButton("Preview Selection")
        self.preview_btn.clicked.connect(self.toggle_preview)
        tgrid.addWidget(self.preview_btn, 0, 4)
        adv_container_layout.addWidget(trim_group)

        # Metadata section
        meta_group = QGroupBox("Metadata && Cover Art")
        mgrid = QGridLayout(meta_group)

        self.preserve_metadata_check = QCheckBox("Keep original metadata")
        self.preserve_metadata_check.setChecked(True)
        self.preserve_metadata_check.toggled.connect(self._on_preserve_metadata_toggled)
        mgrid.addWidget(self.preserve_metadata_check, 0, 0)

        self.preserve_cover_check = QCheckBox("Keep embedded cover art")
        self.preserve_cover_check.setChecked(True)
        mgrid.addWidget(self.preserve_cover_check, 0, 1)

        self.meta_fields = {}
        fields = ["title", "artist", "album", "year", "genre", "comment"]
        for i, field_name in enumerate(fields, start=1):
            label = QLabel(field_name.capitalize() + ":")
            edit = QLineEdit()
            mgrid.addWidget(label, i, 0)
            mgrid.addWidget(edit, i, 1)
            self.meta_fields[field_name] = edit

        adv_container_layout.addWidget(meta_group)
        self.adv_container.setVisible(False)
        layout.addWidget(self.adv_container)

        # --- Progress Bars ---
        prog_layout = QGridLayout()
        prog_layout.addWidget(QLabel("Current File:"), 0, 0)
        self.file_progress_bar = QProgressBar()
        prog_layout.addWidget(self.file_progress_bar, 0, 1)

        prog_layout.addWidget(QLabel("Overall:"), 1, 0)
        self.progress_bar = QProgressBar()
        prog_layout.addWidget(self.progress_bar, 1, 1)
        layout.addLayout(prog_layout)

        # Buttons
        # --- Action Buttons ---
        btn_layout = QHBoxLayout()
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.clicked.connect(self.start_conversion)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_conversion)
        self.show_errors_btn = QPushButton("Show / Copy Errors")
        self.show_errors_btn.setVisible(False)
        self.show_errors_btn.clicked.connect(self._show_last_errors_dialog)
        btn_layout.addWidget(self.convert_btn)
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.show_errors_btn)
        layout.addLayout(btn_layout)

        self.status_label = QLabel("Ready")
        layout.addWidget(self.status_label)

        self.worker = None
        self.worker: Optional[ConversionWorker] = None
        self._load_config()
        self._update_format_dependent_controls()

    # ------------------------------------------------------------------
    # Drag & Drop on MainWindow
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            if paths:
                self.add_paths(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    # ------------------------------------------------------------------
    # Collapsible Advanced section & Format-dependent controls
    # ------------------------------------------------------------------

    def _toggle_advanced_section(self, checked: bool):
        self.adv_container.setVisible(checked)
        self.adv_toggle_btn.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )

    def _on_same_as_source_toggled(self, checked: bool):
        self.output_folder_edit.setEnabled(not checked)
        self.out_browse_btn.setEnabled(not checked)

    def _on_preserve_metadata_toggled(self, checked: bool):
        self.preserve_cover_check.setEnabled(checked)

    def _update_format_dependent_controls(self):
        fmt = self.format_combo.currentText().lower()
        is_lossy = fmt in LOSSY_FORMATS
        is_lossless = fmt in LOSSLESS_FORMATS
        supports_vbr = fmt in ("mp3", "ogg")
        is_flac = fmt == "flac"

        self.vbr_check.setEnabled(supports_vbr)
        vbr_active = supports_vbr and self.vbr_check.isChecked()
        self.vbr_quality.setEnabled(vbr_active)
        self.bitrate_combo.setEnabled(is_lossy and not vbr_active)
        self.bit_depth_combo.setEnabled(is_lossless)
        self.flac_compression.setEnabled(is_flac)
        self.preserve_cover_check.setEnabled(
            self.preserve_metadata_check.isChecked() and fmt in ("mp3", "flac", "m4a")
        )

    # ------------------------------------------------------------------
    # Presets & Persistent Config
    # ------------------------------------------------------------------

    def _config_file_path(self) -> Path:
        return _default_config_dir() / "settings.json"

    def _rebuild_preset_combo(self):
        current = self.preset_combo.currentText() if hasattr(self, "preset_combo") else ""
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for name in BUILTIN_PRESETS:
            self.preset_combo.addItem(name)
        for name in sorted(self.custom_presets):
            self.preset_combo.addItem(f"[User] {name}")
        if current:
            idx = self.preset_combo.findText(current)
            if idx >= 0:
                self.preset_combo.setCurrentIndex(idx)
        self.preset_combo.blockSignals(False)

    def _collect_profile_dict(self) -> dict:
        return {
            "format": self.format_combo.currentText(),
            "bitrate": self.bitrate_combo.currentText(),
            "sample_rate": self.sample_rate_combo.currentText(),
            "channels": self.channels_combo.currentText(),
            "vbr": self.vbr_check.isChecked(),
            "vbr_quality": self.vbr_quality.value(),
            "bit_depth": self.bit_depth_combo.currentText(),
            "flac_compression": self.flac_compression.value(),
            "same_as_source": self.same_as_source_check.isChecked(),
            "preserve_subfolders": self.preserve_subfolders_check.isChecked(),
            "output_folder": self.output_folder_edit.text(),
            "name_suffix": self.name_suffix_edit.text(),
            "overwrite_policy": self.overwrite_combo.currentText(),
            "preserve_metadata": self.preserve_metadata_check.isChecked(),
            "preserve_cover": self.preserve_cover_check.isChecked(),
        }

    def _apply_profile_dict(self, data: dict):
        if not data:
            return
        self._applying_preset = True
        try:
            if "format" in data:
                self.format_combo.setCurrentText(str(data["format"]))
            if "bitrate" in data:
                self.bitrate_combo.setCurrentText(str(data["bitrate"]))
            if "sample_rate" in data:
                self.sample_rate_combo.setCurrentText(str(data["sample_rate"]))
            if "channels" in data:
                self.channels_combo.setCurrentText(str(data["channels"]))
            if "vbr" in data:
                self.vbr_check.setChecked(bool(data["vbr"]))
            if "vbr_quality" in data:
                self.vbr_quality.setValue(int(data["vbr_quality"]))
            if "bit_depth" in data:
                self.bit_depth_combo.setCurrentText(str(data["bit_depth"]))
            if "flac_compression" in data:
                self.flac_compression.setValue(int(data["flac_compression"]))
            if "same_as_source" in data:
                self.same_as_source_check.setChecked(bool(data["same_as_source"]))
            if "preserve_subfolders" in data:
                self.preserve_subfolders_check.setChecked(bool(data["preserve_subfolders"]))
            if "output_folder" in data and data["output_folder"]:
                self.output_folder_edit.setText(str(data["output_folder"]))
            if "name_suffix" in data:
                self.name_suffix_edit.setText(str(data["name_suffix"]))
            if "overwrite_policy" in data:
                self.overwrite_combo.setCurrentText(str(data["overwrite_policy"]))
            if "preserve_metadata" in data:
                self.preserve_metadata_check.setChecked(bool(data["preserve_metadata"]))
            if "preserve_cover" in data:
                self.preserve_cover_check.setChecked(bool(data["preserve_cover"]))
        finally:
            self._applying_preset = False
            self._update_format_dependent_controls()

    def _on_preset_selected(self, text: str):
        if self._applying_preset:
            return
        if text in BUILTIN_PRESETS and BUILTIN_PRESETS[text]:
            self._apply_profile_dict(BUILTIN_PRESETS[text])
        elif text.startswith("[User] "):
            name = text[len("[User] "):]
            if name in self.custom_presets:
                self._apply_profile_dict(self.custom_presets[name])

    def save_custom_preset(self):
        name, ok = QInputDialog.getText(self, "Save Profile", "Profile name:")
        name = (name or "").strip()
        if not ok or not name:
            return
        self.custom_presets[name] = self._collect_profile_dict()
        self._rebuild_preset_combo()
        self.preset_combo.setCurrentText(f"[User] {name}")
        self._save_config()

    def delete_custom_preset(self):
        current = self.preset_combo.currentText()
        if not current.startswith("[User] "):
            QMessageBox.information(self, "Info", "Select a custom [User] profile to delete.")
            return
        name = current[len("[User] "):]
        self.custom_presets.pop(name, None)
        self._rebuild_preset_combo()
        self._save_config()

    def _load_config(self):
        if not self._persist_enabled:
            return
        cfg_file = self._config_file_path()
        if not cfg_file.is_file():
            return
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            self.custom_presets = data.get("custom_presets") or {}
            self._rebuild_preset_combo()
            last = data.get("last_settings")
            if isinstance(last, dict):
                self._apply_profile_dict(last)
        except Exception:
            pass

    def _save_config(self):
        if not self._persist_enabled:
            return
        try:
            cfg_file = self._config_file_path()
            cfg_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "last_settings": self._collect_profile_dict(),
                "custom_presets": self.custom_presets,
            }
            cfg_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Adding / Removing Files & Folders
    # ------------------------------------------------------------------

    def browse_input(self):
        exts = " ".join(f"*.{ext}" for ext in get_supported_input_formats())
        filt = f"Audio Files ({exts});;All Files (*)"
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Audio File", "", filt)
        if file_path:
            self.add_paths([file_path], replace_single=len(self.queue_items) <= 1)

    def browse_input_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Input Folder")
        if folder:
            self.output_folder_edit.setText(folder)
            self.add_paths([folder])

    def _on_input_path_text_changed(self, text: str):
        # Backwards compatibility when a caller/test sets input_path_edit directly
        text = text.strip()
        if not text:
            return
        if not self.queue_items or (len(self.queue_items) == 1 and self.queue_items[0].path != text):
            if os.path.exists(text) or len(self.queue_items) == 0:
                self.queue_items = [QueueItem(path=text)]
                self._refresh_table()

    def add_paths(self, paths: list[str], replace_single: bool = False):
        if replace_single:
            self.queue_items.clear()

        supported = set(get_supported_input_formats())
        existing_paths = {os.path.normcase(os.path.abspath(it.path)) for it in self.queue_items}
        added_any = False

        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                root_str = str(p.resolve())
                for child in sorted(p.rglob("*")):
                    if child.is_file() and child.suffix.lstrip(".").lower() in supported:
                        norm = os.path.normcase(str(child.resolve()))
                        if norm not in existing_paths:
                            existing_paths.add(norm)
                            self.queue_items.append(QueueItem(path=str(child), root_dir=root_str))
                            added_any = True
            else:
                norm = os.path.normcase(os.path.abspath(str(p)))
                if norm not in existing_paths:
                    existing_paths.add(norm)
                    self.queue_items.append(QueueItem(path=str(p), root_dir=None))
                    added_any = True

        self._refresh_table()
        if self.queue_items and added_any:
            self.file_table.selectRow(len(self.queue_items) - 1)
            self._load_file_info(self.queue_items[-1].path)

    def remove_selected_files(self):
        rows = sorted({idx.row() for idx in self.file_table.selectedIndexes()}, reverse=True)
        for r in rows:
            if 0 <= r < len(self.queue_items):
                self.queue_items.pop(r)
        self._refresh_table()
        if self.queue_items:
            self.file_table.selectRow(0)
        else:
            self.input_path_edit.blockSignals(True)
            self.input_path_edit.clear()
            self.input_path_edit.blockSignals(False)
            self._clear_info_labels()

    def clear_queue(self):
        self.queue_items.clear()
        self._refresh_table()
        self.input_path_edit.blockSignals(True)
        self.input_path_edit.clear()
        self.input_path_edit.blockSignals(False)
        self._clear_info_labels()
        self.file_progress_bar.setValue(0)
        self.progress_bar.setValue(0)
        self.status_label.setText("Ready")

    def _refresh_table(self):
        self.file_table.setRowCount(len(self.queue_items))
        for row, item in enumerate(self.queue_items):
            name_item = QTableWidgetItem(Path(item.path).name)
            name_item.setToolTip(item.path)
            info_item = QTableWidgetItem("")
            status_item = QTableWidgetItem(item.status)
            if item.error_msg:
                status_item.setToolTip(item.error_msg)
            out_item = QTableWidgetItem(item.output_path)
            out_item.setToolTip(item.output_path)
            self.file_table.setItem(row, 0, name_item)
            self.file_table.setItem(row, 1, info_item)
            self.file_table.setItem(row, 2, status_item)
            self.file_table.setItem(row, 3, out_item)

        if self.queue_items:
            self.input_path_edit.blockSignals(True)
            if len(self.queue_items) == 1:
                self.input_path_edit.setText(self.queue_items[0].path)
            else:
                self.input_path_edit.setText(f"{len(self.queue_items)} files queued")
            self.input_path_edit.blockSignals(False)

    def _on_table_selection_changed(self):
        rows = sorted({idx.row() for idx in self.file_table.selectedIndexes()})
        if rows and 0 <= rows[0] < len(self.queue_items):
            self._load_file_info(self.queue_items[rows[0]].path)

    def _on_table_cell_double_clicked(self, row: int, _col: int):
        if 0 <= row < len(self.queue_items):
            item = self.queue_items[row]
            if item.error_msg:
                CopyableErrorDialog(self, f"Error – {Path(item.path).name}", item.error_msg).exec()

    # ------------------------------------------------------------------
    # Media Info & Metadata Loading
    # ------------------------------------------------------------------

    def _clear_info_labels(self):
        for lbl in self.info_labels.values():
            lbl.setText("-")
        for edit in self.meta_fields.values():
            edit.clear()

    def _load_file_info(self, file_path: str):
        # Never keep the previous file's tags: they would be written into
        # the next conversion if this file cannot be read.
        for edit in self.meta_fields.values():
            edit.clear()
        self.status_label.setToolTip("")
        try:
            info = probe_media_info(file_path)
            ch_str = "Mono (1)" if info.channels == 1 else (
                "Stereo (2)" if info.channels == 2 else f"{info.channels} ch"
            )
            bd_str = f"{info.bit_depth}-bit" if info.bit_depth else "-"
            dur_str = format_duration(info.duration)
            size_str = _format_size(info.file_size)
            fmt_str = f"{info.format_name.upper()} ({info.audio_codec})" if info.audio_codec else info.format_name.upper()

            self.info_labels["format"].setText(fmt_str)
            self.info_labels["duration"].setText(f"{dur_str} ({info.duration:.1f}s)")
            self.info_labels["size"].setText(size_str)
            self.info_labels["sample_rate"].setText(f"{info.sample_rate} Hz")
            self.info_labels["channels"].setText(ch_str)
            self.info_labels["bit_depth"].setText(bd_str)

            # Update Info column in table if present
            for row, it in enumerate(self.queue_items):
                if it.path == file_path and self.file_table.item(row, 1):
                    self.file_table.item(row, 1).setText(
                        f"{info.audio_codec} | {dur_str} | {info.sample_rate}Hz"
                    )

            self.status_label.setText(
                f"Loaded: {Path(file_path).name} | {info.audio_codec} | {info.sample_rate}Hz | {info.channels}ch"
            )

            meta = read_metadata(file_path)
            self.meta_fields["title"].setText(meta.get("title", ""))
            self.meta_fields["artist"].setText(meta.get("artist", ""))
            self.meta_fields["album"].setText(meta.get("album", ""))
            self.meta_fields["year"].setText(meta.get("year", ""))
            self.meta_fields["genre"].setText(meta.get("genre", ""))
            self.meta_fields["comment"].setText(meta.get("comment", ""))

            if not self.output_folder_edit.text():
                self.output_folder_edit.setText(str(Path(file_path).parent))

        except Exception as e:
            for lbl in self.info_labels.values():
                lbl.setText("-")
            self.status_label.setText(f"Cannot read audio information from: {Path(file_path).name}")
            self.status_label.setToolTip(str(e))

    # ------------------------------------------------------------------
    # Output Folder Helpers
    # ------------------------------------------------------------------

    def browse_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.output_folder_edit.setText(folder)

    def open_output_folder(self):
        folder = self.output_folder_edit.text().strip()
        if (not folder or self.same_as_source_check.isChecked()) and self.queue_items:
            folder = str(Path(self.queue_items[0].path).parent)
        if not folder or not Path(folder).is_dir():
            QMessageBox.warning(self, "Warning", "Output folder does not exist.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(folder).resolve())))

    # ------------------------------------------------------------------
    # Audio Preview (Trim / Listen)
    # ------------------------------------------------------------------

    def _current_selected_path(self) -> Optional[str]:
        rows = sorted({idx.row() for idx in self.file_table.selectedIndexes()})
        if rows and 0 <= rows[0] < len(self.queue_items):
            return self.queue_items[rows[0]].path
        if self.queue_items:
            return self.queue_items[0].path
        txt = self.input_path_edit.text().strip()
        return txt or None

    def toggle_preview(self):
        if self._preview_process is not None and self._preview_process.poll() is None:
            self.stop_preview()
            return

        path = self._current_selected_path()
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, "Warning", "Please select a valid audio file to preview.")
            return

        try:
            start_s = parse_timecode(self.trim_start_edit.text())
            end_s = parse_timecode(self.trim_end_edit.text())
            if start_s is not None and end_s is not None and end_s <= start_s:
                raise ConversionError("End time must be greater than start time.")
            ffplay = find_ffplay()
        except Exception as e:
            QMessageBox.critical(self, "Preview Error", str(e))
            return

        cmd = [ffplay, "-nodisp", "-autoexit", "-hide_banner", "-loglevel", "error"]
        if start_s is not None and start_s > 0:
            cmd.extend(["-ss", f"{start_s:.3f}"])
        if end_s is not None and end_s > 0:
            dur = end_s - (start_s or 0.0)
            cmd.extend(["-t", f"{dur:.3f}"])
        cmd.append(path)

        try:
            self._preview_process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.preview_btn.setText("Stop Preview")
            self.status_label.setText(f"Previewing: {Path(path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Preview Error", f"Could not start preview: {e}")

    def stop_preview(self):
        if self._preview_process is not None and self._preview_process.poll() is None:
            self._preview_process.terminate()
            try:
                self._preview_process.wait(timeout=2)
            except Exception:
                self._preview_process.kill()
        self._preview_process = None
        self.preview_btn.setText("Preview Selection")

    # ------------------------------------------------------------------
    # Conversion Execution (Single & Batch)
    # ------------------------------------------------------------------

    def _build_output_path_for_item(self, item: QueueItem, output_format: str) -> str:
        in_path = Path(item.path)
        suffix_str = self.name_suffix_edit.text().strip()
        output_name = f"{in_path.stem}{suffix_str}.{output_format}"

        if self.same_as_source_check.isChecked() or not self.output_folder_edit.text().strip():
            base_dir = in_path.parent
        else:
            base_dir = Path(self.output_folder_edit.text().strip())
            if (
                self.preserve_subfolders_check.isChecked()
                and item.root_dir
                and base_dir.is_dir()
            ):
                try:
                    rel_parent = in_path.resolve().parent.relative_to(Path(item.root_dir).resolve())
                    target_dir = base_dir / rel_parent
                    target_dir.mkdir(parents=True, exist_ok=True)
                    base_dir = target_dir
                except ValueError:
                    pass

        return str(base_dir / output_name)

    def start_conversion(self):
        if self.worker is not None and self.worker.isRunning():
            return

        if not self.queue_items:
            raw = self.input_path_edit.text().strip()
            if raw:
                self.queue_items = [QueueItem(path=raw)]
                self._refresh_table()

        if not self.queue_items:
            QMessageBox.warning(self, "Warning", "Please select an input file.")
            return

        self.stop_preview()

        try:
            trim_start = parse_timecode(self.trim_start_edit.text())
            trim_end = parse_timecode(self.trim_end_edit.text())
        except ConversionError as e:
            QMessageBox.critical(self, "Error", f"Conversion failed:\n{e}")
            return

        output_format = self.format_combo.currentText().lower()

        # Parse settings
        b = self.bitrate_combo.currentText()
        bitrate = None if b == "Keep source" else int(b)

        sr = self.sample_rate_combo.currentText()
        sample_rate = None if sr == "Keep source" else int(sr)

        channels = None
        c = self.channels_combo.currentText()
        if c == "Mono":
            channels = 1
        elif c == "Stereo":
            channels = 2

        bd = self.bit_depth_combo.currentText()
        bit_depth = None if bd == "Default" else int(bd)

        overwrite_policy = self.overwrite_combo.currentText().lower()
        preserve_meta = self.preserve_metadata_check.isChecked()
        preserve_cover = self.preserve_cover_check.isChecked()
        # Custom metadata fields apply when converting a single file (or if user edited them)
        custom_meta = (
            {k: e.text() for k, e in self.meta_fields.items() if e.text()}
            if len(self.queue_items) == 1
            else {}
        )

        jobs: list[ConversionSettings] = []
        for idx, item in enumerate(self.queue_items):
            item.status = "Queued"
            item.progress = 0
            item.error_msg = ""
            out_path = self._build_output_path_for_item(item, output_format)
            item.output_path = out_path
            if self.file_table.item(idx, 2):
                self.file_table.item(idx, 2).setText("Queued")
                self.file_table.item(idx, 2).setToolTip("")
            if self.file_table.item(idx, 3):
                self.file_table.item(idx, 3).setText(out_path)

            jobs.append(
                ConversionSettings(
                    input_path=item.path,
                    output_path=out_path,
                    output_format=output_format,
                    bitrate=bitrate,
                    sample_rate=sample_rate,
                    channels=channels,
                    vbr=self.vbr_check.isChecked(),
                    vbr_quality=self.vbr_quality.value(),
                    bit_depth=bit_depth,
                    flac_compression=self.flac_compression.value(),
                    preserve_metadata=preserve_meta,
                    preserve_cover_art=preserve_cover,
                    metadata=dict(custom_meta),
                    trim_start=trim_start,
                    trim_end=trim_end,
                    overwrite_policy=overwrite_policy,
                )
            )

        self.last_error_text = ""
        self.show_errors_btn.setVisible(False)
        self.convert_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.file_progress_bar.setValue(0)
        self.progress_bar.setValue(0)
        self.status_label.setText("Converting...")

        self._save_config()

        self.worker = ConversionWorker(jobs)
        self.worker.file_started.connect(self._on_file_started)
        self.worker.file_progress.connect(self._on_file_progress)
        self.worker.file_finished.connect(self._on_file_finished)
        self.worker.file_skipped.connect(self._on_file_skipped)
        self.worker.file_error.connect(self._on_file_error)
        self.worker.overall_progress.connect(self.progress_bar.setValue)
        self.worker.finished.connect(self._conversion_finished)
        self.worker.error.connect(self._conversion_error)
        self.worker.cancelled.connect(self._conversion_cancelled)
        self.worker.start()

    def _on_file_started(self, idx: int, input_path: str):
        self.file_progress_bar.setValue(0)
        if 0 <= idx < len(self.queue_items):
            self.queue_items[idx].status = "Converting..."
            if self.file_table.item(idx, 2):
                self.file_table.item(idx, 2).setText("Converting...")
        self.status_label.setText(
            f"Converting ({idx + 1}/{len(self.queue_items)}): {Path(input_path).name}"
        )

    def _on_file_progress(self, idx: int, pct: int):
        self.file_progress_bar.setValue(pct)
        if 0 <= idx < len(self.queue_items):
            self.queue_items[idx].progress = pct
            if self.file_table.item(idx, 2):
                self.file_table.item(idx, 2).setText(f"Converting ({pct}%)")

    def _on_file_finished(self, idx: int, output_path: str):
        if 0 <= idx < len(self.queue_items):
            self.queue_items[idx].status = "Done"
            self.queue_items[idx].output_path = output_path
            if self.file_table.item(idx, 2):
                self.file_table.item(idx, 2).setText("Done")
            if self.file_table.item(idx, 3):
                self.file_table.item(idx, 3).setText(output_path)
                self.file_table.item(idx, 3).setToolTip(output_path)

    def _on_file_skipped(self, idx: int, reason: str):
        if 0 <= idx < len(self.queue_items):
            self.queue_items[idx].status = "Skipped"
            if self.file_table.item(idx, 2):
                self.file_table.item(idx, 2).setText("Skipped")
                self.file_table.item(idx, 2).setToolTip(reason)

    def _on_file_error(self, idx: int, error_msg: str):
        if 0 <= idx < len(self.queue_items):
            self.queue_items[idx].status = "Error"
            self.queue_items[idx].error_msg = error_msg
            if self.file_table.item(idx, 2):
                self.file_table.item(idx, 2).setText("Error (double-click to view)")
                self.file_table.item(idx, 2).setToolTip(error_msg)

    def _conversion_finished(self, output_path: str):
        self.convert_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.file_progress_bar.setValue(100)
        self.progress_bar.setValue(100)
        if len(self.queue_items) <= 1:
            self.status_label.setText(f"Done: {output_path}")
            QMessageBox.information(self, "Success", f"File converted:\n{output_path}")
        else:
            done_count = sum(1 for it in self.queue_items if it.status == "Done")
            skip_count = sum(1 for it in self.queue_items if it.status == "Skipped")
            self.status_label.setText(
                f"Done: {done_count}/{len(self.queue_items)} converted"
                + (f", {skip_count} skipped" if skip_count else "")
            )
            QMessageBox.information(
                self,
                "Success",
                f"Batch conversion complete.\nConverted: {done_count}, Skipped: {skip_count}",
            )

    def _conversion_error(self, error_msg: str):
        self.convert_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.last_error_text = error_msg
        self.show_errors_btn.setVisible(True)
        self.status_label.setText("Error")
        QMessageBox.critical(self, "Error", f"Conversion failed:\n{error_msg}")

    def _show_last_errors_dialog(self):
        if self.last_error_text:
            CopyableErrorDialog(self, "Conversion Errors", self.last_error_text).exec()

    def _conversion_cancelled(self):
        self.convert_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.file_progress_bar.setValue(0)
        self.progress_bar.setValue(0)
        self.status_label.setText("Cancelled")

    def cancel_conversion(self):
        if self.worker:
            self.worker.cancel()
            self.status_label.setText("Cancelling...")

    def closeEvent(self, event):
        self.stop_preview()
        self._save_config()
        # Do not destroy the window (and the QThread) while FFmpeg is running.
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(10000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())