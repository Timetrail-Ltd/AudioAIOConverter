"""GUI tests.

These drive the real MainWindow *headless* (QT_QPA_PLATFORM=offscreen): widgets
are filled in and buttons are clicked programmatically, QThread workers and
FFmpeg are real. Modal dialogs are replaced by recorders. This is NOT a
substitute for a manual look at the window on a real desktop.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from audio_converter import gui  # noqa: E402

from .helpers import assert_valid_output, leftover_temp_files, make_audio  # noqa: E402


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialogs(monkeypatch):
    shown = []
    for kind in ("information", "critical", "warning"):
        monkeypatch.setattr(
            QMessageBox, kind,
            staticmethod(lambda parent, title, text, _k=kind: shown.append((_k, title, text))),
        )
    return shown


@pytest.fixture
def window(app, dialogs):
    w = gui.MainWindow()
    w.show()
    yield w
    if w.worker is not None and w.worker.isRunning():
        w.worker.cancel()
        w.worker.wait(10000)
    w.close()


def pump(ms):
    """Process Qt events for `ms` milliseconds.

    Deliberately not QTest.qWait(): that holds the GIL, which starves the
    worker thread that reads FFmpeg's output (a 0.2 s conversion then takes
    ~10 s). time.sleep() releases it, like the real app.exec() loop does.
    """
    end = time.time() + ms / 1000
    while time.time() < end:
        QApplication.processEvents()
        time.sleep(0.005)


def wait_until(cond, timeout=60.0):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        QApplication.processEvents()
        time.sleep(0.005)
    return cond()


def choose_input(win, monkeypatch, path):
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), "")))
    win.browse_input()


def convert_and_wait(win):
    win.convert_btn.click()
    assert wait_until(lambda: win.convert_btn.isEnabled()), "conversion did not finish in time"
    pump(50)


@pytest.fixture
def song(tmp_path):
    d = tmp_path / "Zenék és más"
    d.mkdir()
    return make_audio(d / "Árvíztűrő dal.wav", metadata={"title": "Cím", "artist": "Előadó"})


def test_loading_a_file_shows_real_audio_info(window, monkeypatch, song):
    """Regression: status used to read 'Loaded: x |  | 0Hz | 0ch'."""
    choose_input(window, monkeypatch, song)
    status = window.status_label.text()
    assert "pcm_s16le" in status and "44100Hz" in status and "2ch" in status
    assert window.meta_fields["title"].text() == "Cím"
    assert window.output_folder_edit.text() == str(song.parent)


def test_failed_load_does_not_keep_previous_metadata(window, monkeypatch, song, tmp_path):
    """Regression: stale tags of the previous file stayed in the fields."""
    choose_input(window, monkeypatch, song)
    assert window.meta_fields["title"].text() == "Cím"
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(bytes(range(256)) * 40)
    choose_input(window, monkeypatch, bad)
    assert all(e.text() == "" for e in window.meta_fields.values())
    assert "bad.mp3" in window.status_label.text()


def test_default_conversion(window, monkeypatch, dialogs, song):
    choose_input(window, monkeypatch, song)
    convert_and_wait(window)
    out = song.parent / "Árvíztűrő dal.mp3"
    assert_valid_output(out, "mp3")
    assert dialogs and dialogs[-1][0] == "information"
    assert window.status_label.text().startswith("Done")
    assert window.convert_btn.isEnabled() and not window.cancel_btn.isEnabled()
    assert window.progress_bar.value() == 100


@pytest.mark.parametrize("fmt", ["mp3", "aac", "m4a", "ogg", "opus", "flac", "wav", "aiff"])
def test_every_output_format_from_gui_with_default_settings(window, monkeypatch, dialogs, song, fmt):
    """The GUI defaults (192 kbps, 48000 Hz, quality 4) must work for every format."""
    choose_input(window, monkeypatch, song)
    window.format_combo.setCurrentText(fmt)
    convert_and_wait(window)
    assert [d[0] for d in dialogs] == ["information"], dialogs
    out = song.parent / f"Árvíztűrő dal.{fmt}" if fmt != "wav" else song.parent / "Árvíztűrő dal_1.wav"
    assert_valid_output(out, fmt)


def test_rapid_repeated_clicks_start_a_single_conversion(window, monkeypatch, dialogs, song):
    choose_input(window, monkeypatch, song)
    for _ in range(5):
        window.convert_btn.click()
    assert wait_until(lambda: window.convert_btn.isEnabled())
    pump(300)
    mp3s = sorted(p.name for p in song.parent.glob("*.mp3"))
    assert mp3s == ["Árvíztűrő dal.mp3"]
    assert len(dialogs) == 1


def test_converting_again_never_overwrites(window, monkeypatch, song):
    choose_input(window, monkeypatch, song)
    convert_and_wait(window)
    convert_and_wait(window)
    assert sorted(p.name for p in song.parent.glob("*.mp3")) == ["Árvíztűrő dal.mp3", "Árvíztűrő dal_1.mp3"]


def test_broken_input_shows_readable_error_and_ui_recovers(window, monkeypatch, dialogs, tmp_path):
    bad = tmp_path / "broken.mp3"
    bad.write_bytes(bytes(range(256)) * 40)
    choose_input(window, monkeypatch, bad)
    convert_and_wait(window)
    kind, title, text = dialogs[-1]
    assert kind == "critical"
    assert "damaged, empty or not a supported audio file" in text
    assert window.status_label.text() == "Error"
    assert window.convert_btn.isEnabled() and not window.cancel_btn.isEnabled()
    assert list(p.name for p in tmp_path.iterdir()) == ["broken.mp3"]


def test_unsupported_sample_rate_selection_shows_clear_error(window, monkeypatch, dialogs, song):
    choose_input(window, monkeypatch, song)
    window.format_combo.setCurrentText("opus")
    window.sample_rate_combo.setCurrentText("44100")
    convert_and_wait(window)
    assert dialogs[-1][0] == "critical"
    assert "does not support 44100 Hz" in dialogs[-1][2]


def test_nonexistent_output_folder_shows_clear_error(window, monkeypatch, dialogs, song):
    choose_input(window, monkeypatch, song)
    window.output_folder_edit.setText(str(song.parent / "nope" / "deeper"))
    convert_and_wait(window)
    assert dialogs[-1][0] == "critical"
    assert "Output folder does not exist" in dialogs[-1][2]
    assert not (song.parent / "nope").exists()


def test_start_without_input_file_warns(window, dialogs):
    window.convert_btn.click()
    assert dialogs[-1][0] == "warning"
    assert window.convert_btn.isEnabled()


def test_cancel_is_not_reported_as_an_error_and_retry_works(window, monkeypatch, dialogs, tmp_path):
    """Regression: cancelling showed 'Conversion failed' and status 'Error'."""
    long_src = make_audio(tmp_path / "long.wav", duration=300.0)
    choose_input(window, monkeypatch, long_src)
    window.format_combo.setCurrentText("flac")
    window.convert_btn.click()
    assert window.cancel_btn.isEnabled()
    window.cancel_btn.click()
    assert wait_until(lambda: window.convert_btn.isEnabled())
    pump(50)
    assert dialogs == []
    assert window.status_label.text() == "Cancelled"
    assert window.progress_bar.value() == 0
    assert not window.cancel_btn.isEnabled()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["long.wav"]
    assert not leftover_temp_files(tmp_path)

    # retry the same conversion on a short file works afterwards
    short = make_audio(tmp_path / "short.wav")
    choose_input(window, monkeypatch, short)
    window.format_combo.setCurrentText("mp3")
    convert_and_wait(window)
    assert_valid_output(tmp_path / "short.mp3", "mp3")
    assert dialogs[-1][0] == "information"


def test_closing_window_during_conversion_stops_ffmpeg(window, monkeypatch, tmp_path):
    """Regression: the worker thread kept running after the window was closed."""
    long_src = make_audio(tmp_path / "long.wav", duration=300.0)
    choose_input(window, monkeypatch, long_src)
    window.format_combo.setCurrentText("flac")
    window.convert_btn.click()
    worker = window.worker
    assert worker.isRunning()
    window.close()
    assert not worker.isRunning()
    assert not leftover_temp_files(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["long.wav"]


# --------------------------------------------------------------------------
# Extended GUI & Converter Features (Batch, Drag&Drop, Presets, Trim, Info)
# --------------------------------------------------------------------------

def test_detailed_input_info_labels_populated(window, monkeypatch, song):
    choose_input(window, monkeypatch, song)
    assert "WAV" in window.info_labels["format"].text()
    assert "pcm_s16le" in window.info_labels["format"].text()
    assert "00:02" in window.info_labels["duration"].text()
    assert "KB" in window.info_labels["size"].text() or "B" in window.info_labels["size"].text()
    assert "44100 Hz" in window.info_labels["sample_rate"].text()
    assert "Stereo" in window.info_labels["channels"].text()
    assert "16-bit" in window.info_labels["bit_depth"].text()


def test_default_sample_rate_is_keep_source(window, monkeypatch, song):
    assert window.sample_rate_combo.currentText() == "Keep source"
    choose_input(window, monkeypatch, song)
    convert_and_wait(window)
    from .helpers import audio_stream
    out = song.parent / "Árvíztűrő dal.mp3"
    assert int(audio_stream(out)["sample_rate"]) == 44100


def test_format_dependent_controls_enable_and_disable(window):
    # MP3: lossy, VBR supported, bit_depth & flac_compression disabled
    window.format_combo.setCurrentText("mp3")
    assert window.bitrate_combo.isEnabled()
    assert window.vbr_check.isEnabled()
    assert not window.bit_depth_combo.isEnabled()
    assert not window.flac_compression.isEnabled()

    # Toggling VBR disables bitrate combo and enables VBR quality spinbox
    window.vbr_check.setChecked(True)
    assert not window.bitrate_combo.isEnabled()
    assert window.vbr_quality.isEnabled()
    window.vbr_check.setChecked(False)

    # FLAC: lossless, bitrate & VBR disabled, bit_depth & flac_compression enabled
    window.format_combo.setCurrentText("flac")
    assert not window.bitrate_combo.isEnabled()
    assert not window.vbr_check.isEnabled()
    assert not window.vbr_quality.isEnabled()
    assert window.bit_depth_combo.isEnabled()
    assert window.flac_compression.isEnabled()

    # WAV: lossless, bit_depth enabled, flac_compression disabled
    window.format_combo.setCurrentText("wav")
    assert not window.bitrate_combo.isEnabled()
    assert window.bit_depth_combo.isEnabled()
    assert not window.flac_compression.isEnabled()


def test_collapsible_advanced_section(window):
    assert not window.adv_toggle_btn.isChecked()
    assert window.adv_container.isHidden()
    window.adv_toggle_btn.click()
    assert window.adv_toggle_btn.isChecked()
    assert not window.adv_container.isHidden()
    window.adv_toggle_btn.click()
    assert window.adv_container.isHidden()


def test_batch_folder_preserve_subfolders_and_suffix(window, dialogs, tmp_path):
    in_root = tmp_path / "album"
    sub1 = in_root / "CD1"
    sub2 = in_root / "CD2"
    sub1.mkdir(parents=True)
    sub2.mkdir(parents=True)
    f1 = make_audio(sub1 / "track1.wav", duration=1.5)
    f2 = make_audio(sub2 / "track2.wav", duration=1.5)

    out_root = tmp_path / "converted_out"
    out_root.mkdir()

    # Simulate dropping the folder
    window.add_paths([str(in_root)])
    assert len(window.queue_items) == 2
    assert window.file_table.rowCount() == 2

    window.output_folder_edit.setText(str(out_root))
    window.preserve_subfolders_check.setChecked(True)
    window.name_suffix_edit.setText("_remastered")
    window.format_combo.setCurrentText("mp3")

    convert_and_wait(window)

    out1 = out_root / "CD1" / "track1_remastered.mp3"
    out2 = out_root / "CD2" / "track2_remastered.mp3"
    assert_valid_output(out1, "mp3", duration=1.5)
    assert_valid_output(out2, "mp3", duration=1.5)
    assert [it.status for it in window.queue_items] == ["Done", "Done"]
    assert window.file_progress_bar.value() == 100
    assert window.progress_bar.value() == 100


def test_overwrite_skip_and_overwrite_policies_in_gui(window, monkeypatch, dialogs, song):
    choose_input(window, monkeypatch, song)
    window.format_combo.setCurrentText("mp3")
    convert_and_wait(window)
    out = song.parent / "Árvíztűrő dal.mp3"
    assert out.exists()

    # Skip policy: does not fail, marks item as Skipped, does not create _1
    window.overwrite_combo.setCurrentText("Skip")
    dialogs.clear()
    convert_and_wait(window)
    assert window.queue_items[0].status == "Skipped"
    assert not (song.parent / "Árvíztűrő dal_1.mp3").exists()

    # Overwrite policy: replaces existing file in-place without creating _1
    out.write_bytes(b"OLD")
    window.overwrite_combo.setCurrentText("Overwrite")
    convert_and_wait(window)
    assert window.queue_items[0].status == "Done"
    assert_valid_output(out, "mp3")
    assert not (song.parent / "Árvíztűrő dal_1.mp3").exists()


def test_trim_start_and_end_in_gui(window, monkeypatch, dialogs, tmp_path):
    src = make_audio(tmp_path / "long_clip.wav", duration=4.0)
    choose_input(window, monkeypatch, src)
    window.format_combo.setCurrentText("wav")
    window.name_suffix_edit.setText("_trimmed")
    window.trim_start_edit.setText("00:01")
    window.trim_end_edit.setText("00:02.5")
    convert_and_wait(window)
    out = tmp_path / "long_clip_trimmed.wav"
    assert_valid_output(out, "wav", duration=1.5, tolerance=0.25)


def test_presets_and_custom_profile_persistence(app, dialogs, tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIOCONVERTER_CONFIG_DIR", str(tmp_path / "cfg"))
    from PySide6.QtWidgets import QInputDialog

    w1 = gui.MainWindow(load_saved_state=True)
    w1.preset_combo.setCurrentText("FLAC – Studio (24-bit)")
    assert w1.format_combo.currentText() == "flac"
    assert w1.bit_depth_combo.currentText() == "24"
    assert w1.flac_compression.value() == 8

    # Customize and save a custom profile
    w1.name_suffix_edit.setText("_studio24")
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("MyStudio", True)))
    w1.save_custom_preset()
    assert w1.preset_combo.currentText() == "[User] MyStudio"
    w1.close()

    # Reopen window and check that last settings + custom preset were persisted
    w2 = gui.MainWindow(load_saved_state=True)
    assert w2.format_combo.currentText() == "flac"
    assert w2.bit_depth_combo.currentText() == "24"
    assert w2.name_suffix_edit.setText is not None
    assert w2.name_suffix_edit.text() == "_studio24"
    assert w2.preset_combo.findText("[User] MyStudio") >= 0
    w2.close()


def test_copyable_error_dialog_copies_to_clipboard(window, monkeypatch, dialogs, tmp_path):
    bad = tmp_path / "corrupt.mp3"
    bad.write_bytes(b"corrupt" * 50)
    choose_input(window, monkeypatch, bad)
    convert_and_wait(window)
    assert not window.show_errors_btn.isHidden()
    assert "damaged, empty or not a supported audio file" in window.last_error_text

    dlg = gui.CopyableErrorDialog(window, "Errors", window.last_error_text)
    dlg._copy()
    assert QApplication.clipboard().text() == window.last_error_text
    assert dlg.copy_btn.text() == "Copied!"
