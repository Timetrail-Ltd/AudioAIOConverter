"""Main GUI window for the audio converter."""

import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QLabel, QLineEdit, QPushButton,
    QComboBox, QSpinBox, QCheckBox, QProgressBar, QMessageBox,
    QFileDialog,
)

from .converter import ConversionSettings, convert_audio, ConversionError
from .ffmpeg_utils import probe_media_info, get_supported_output_formats
from .metadata import read_metadata


class ConversionWorker(QThread):
    """Background worker for audio conversion."""

    progress = Signal(int)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, settings: ConversionSettings):
        super().__init__()
        self.settings = settings
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            def progress_cb(value):
                self.progress.emit(int(value * 100))

            def cancel_cb():
                return self._cancelled

            output = convert_audio(
                self.settings,
                progress_callback=progress_cb,
                cancel_flag=cancel_cb,
            )
            self.finished.emit(output)
        except Exception as e:
            self.error.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AudioAIOConverter")
        self.setMinimumSize(600, 500)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Input
        input_group = QGroupBox("Input File")
        input_layout = QHBoxLayout(input_group)
        self.input_path_edit = QLineEdit()
        self.input_path_edit.setReadOnly(True)
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self.browse_input)
        input_layout.addWidget(self.input_path_edit)
        input_layout.addWidget(self.browse_btn)
        layout.addWidget(input_group)

        # Format
        fmt_layout = QHBoxLayout()
        fmt_layout.addWidget(QLabel("Output Format:"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(get_supported_output_formats())
        fmt_layout.addWidget(self.format_combo)
        layout.addLayout(fmt_layout)

        # Output folder
        out_layout = QHBoxLayout()
        out_layout.addWidget(QLabel("Output Folder:"))
        self.output_folder_edit = QLineEdit()
        self.out_browse_btn = QPushButton("Browse...")
        self.out_browse_btn.clicked.connect(self.browse_output_folder)
        out_layout.addWidget(self.output_folder_edit)
        out_layout.addWidget(self.out_browse_btn)
        layout.addLayout(out_layout)

        # Settings
        settings_group = QGroupBox("Settings")
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
        sgrid.addWidget(self.sample_rate_combo, 1, 1)

        sgrid.addWidget(QLabel("Channels:"), 2, 0)
        self.channels_combo = QComboBox()
        self.channels_combo.addItems(["Keep source", "Mono", "Stereo"])
        sgrid.addWidget(self.channels_combo, 2, 1)

        layout.addWidget(settings_group)

        # Metadata
        meta_group = QGroupBox("Metadata")
        mgrid = QGridLayout(meta_group)

        self.meta_fields = {}
        fields = ["title", "artist", "album", "year", "genre", "comment"]
        for i, field in enumerate(fields):
            label = QLabel(field.capitalize() + ":")
            edit = QLineEdit()
            mgrid.addWidget(label, i, 0)
            mgrid.addWidget(edit, i, 1)
            self.meta_fields[field] = edit

        layout.addWidget(meta_group)

        # Advanced
        adv_group = QGroupBox("Advanced Settings")
        agrid = QGridLayout(adv_group)

        agrid.addWidget(QLabel("VBR (MP3):"), 0, 0)
        self.vbr_check = QCheckBox()
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

        agrid.addWidget(QLabel("FLAC Compression:"), 3, 0)
        self.flac_compression = QSpinBox()
        self.flac_compression.setRange(0, 12)
        self.flac_compression.setValue(5)
        agrid.addWidget(self.flac_compression, 3, 1)

        layout.addWidget(adv_group)

        # Progress
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)

        # Buttons
        btn_layout = QHBoxLayout()
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.clicked.connect(self.start_conversion)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_conversion)
        btn_layout.addWidget(self.convert_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        self.status_label = QLabel("Ready")
        layout.addWidget(self.status_label)

        self.worker = None

    def browse_input(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Audio File",
            "",
            "Audio Files (*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.opus *.aiff);;All Files (*)"
        )
        if file_path:
            self.input_path_edit.setText(file_path)
            self._load_file_info(file_path)

    def browse_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.output_folder_edit.setText(folder)

    def _load_file_info(self, file_path):
        try:
            info = probe_media_info(file_path)
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
            self.status_label.setText(f"Error reading file: {e}")

    def start_conversion(self):
        input_path = self.input_path_edit.text()
        if not input_path:
            QMessageBox.warning(self, "Warning", "Please select an input file.")
            return

        output_folder = self.output_folder_edit.text() or str(Path(input_path).parent)
        output_format = self.format_combo.currentText().lower()
        output_name = Path(input_path).stem + "." + output_format
        output_path = str(Path(output_folder) / output_name)

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

        metadata = {k: e.text() for k, e in self.meta_fields.items() if e.text()}

        settings = ConversionSettings(
            input_path=input_path,
            output_path=output_path,
            output_format=output_format,
            bitrate=bitrate,
            sample_rate=sample_rate,
            channels=channels,
            vbr=self.vbr_check.isChecked(),
            vbr_quality=self.vbr_quality.value(),
            bit_depth=bit_depth,
            flac_compression=self.flac_compression.value(),
            metadata=metadata,
            overwrite_policy="rename",
        )

        self.convert_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.status_label.setText("Converting...")

        self.worker = ConversionWorker(settings)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.finished.connect(self._conversion_finished)
        self.worker.error.connect(self._conversion_error)
        self.worker.start()

    def _conversion_finished(self, output_path: str):
        self.convert_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.status_label.setText(f"Done: {output_path}")
        QMessageBox.information(self, "Success", f"File converted:\n{output_path}")

    def _conversion_error(self, error_msg: str):
        self.convert_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.status_label.setText("Error")
        QMessageBox.critical(self, "Error", f"Conversion failed:\n{error_msg}")

    def cancel_conversion(self):
        if self.worker:
            self.worker.cancel()
            self.status_label.setText("Cancelling...")


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())