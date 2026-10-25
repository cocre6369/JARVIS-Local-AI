"""Voice: speech-to-text, wake word, and text-to-speech settings."""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..controller import K_SPEECH, K_STT, K_WAKE
from ..theme import Theme
from .base import Page, card, hint

STT_ENGINES = [("auto", "Automatic (best available)"),
               ("faster-whisper", "faster-whisper (local, neural)"),
               ("vosk", "Vosk (local, lightweight)"),
               ("none", "Off - no speech input")]
WHISPER_SIZES = ["tiny.en", "base.en", "small.en", "medium.en", "large-v3",
                 "tiny", "base", "small"]
DEVICES = [("cpu", "CPU"), ("cuda", "NVIDIA GPU (CUDA)"), ("auto", "Automatic")]
COMPUTE = [("int8", "int8 (fastest, least memory)"),
           ("int8_float16", "int8_float16 (balanced)"),
           ("float16", "float16 (best quality, needs VRAM)"),
           ("float32", "float32 (CPU fallback)")]
MODES = [("wakeword", "Wake word - 'Hey JARVIS'"),
         ("push_to_talk", "Push to talk - hold the hotkey"),
         ("button", "Microphone button only"),
         ("continuous", "Continuous dictation"),
         ("disabled", "Off - no microphone use")]
TTS_ENGINES = [("auto", "Automatic (best available)"),
               ("piper", "Piper (local, neural)"),
               ("sapi", "Windows SAPI (built in)"),
               ("espeak-ng", "espeak-ng (fallback)"),
               ("none", "Off - no speech output")]


def _combo(options: List[tuple], current: Any) -> QComboBox:
    combo = QComboBox()
    for value, label in options:
        combo.addItem(label, value)
    index = combo.findData(str(current))
    combo.setCurrentIndex(index if index >= 0 else 0)
    return combo


class VoicePage(Page):
    """Everything about the microphone and the voice."""

    def __init__(self, theme: Theme, controller: Any,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(theme, controller, "Voice / TTS",
                         "All speech processing happens on this PC. Audio is never "
                         "recorded to disk and never uploaded.", parent)

        # --- status ----------------------------------------------------------
        status_frame, status = card("What is available on this PC")
        self.engine_table = QTableWidget(0, 3)
        self.engine_table.setHorizontalHeaderLabels(["Component", "Installed", "Detail"])
        self.engine_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        self.engine_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.engine_table.setMaximumHeight(210)
        status.addWidget(self.engine_table)
        self.speech_status = hint("")
        status.addWidget(self.speech_status)
        self.body.addWidget(status_frame)

        # --- input -----------------------------------------------------------
        input_frame, input_layout = card("Speech input")
        form = QFormLayout()
        sp = self.settings().speech
        self.stt_engine = _combo(STT_ENGINES, sp.stt_engine)
        self.stt_size = QComboBox()
        self.stt_size.addItems(WHISPER_SIZES)
        self.stt_size.setCurrentText(str(sp.stt_model_size))
        self.stt_device = _combo(DEVICES, sp.stt_device)
        self.stt_compute = _combo(COMPUTE, sp.stt_compute_type)
        self.microphone = QComboBox()
        self.sample_rate = QSpinBox()
        self.sample_rate.setRange(8000, 48000)
        self.sample_rate.setSingleStep(1000)
        self.sample_rate.setValue(int(sp.sample_rate))
        self.mode = _combo(MODES, sp.activation_mode)
        self.wake_phrases = QLineEdit(", ".join(sp.wake_phrases or []))
        self.ptt_key = QLineEdit(str(sp.push_to_talk_key))
        self.vad = QSpinBox()
        self.vad.setRange(0, 3)
        self.vad.setValue(int(sp.vad_aggressiveness))
        self.barge_in = QCheckBox("Let me interrupt JARVIS while it is speaking")
        self.barge_in.setChecked(bool(sp.barge_in))
        self.stt_on = QCheckBox("Use the microphone at all (speech recognition)")
        self.stt_on.setChecked(bool(sp.stt_enabled))
        form.addRow("", self.stt_on)
        form.addRow("Engine", self.stt_engine)
        form.addRow("Model", self.stt_size)
        form.addRow("Run on", self.stt_device)
        form.addRow("Precision", self.stt_compute)
        form.addRow("Microphone", self.microphone)
        form.addRow("Sample rate (Hz)", self.sample_rate)
        form.addRow("Activation", self.mode)
        form.addRow("Wake phrases", self.wake_phrases)
        form.addRow("Push-to-talk key", self.ptt_key)
        form.addRow("Voice gate (0-3)", self.vad)
        form.addRow("", self.barge_in)
        input_layout.addLayout(form)
        input_layout.addWidget(hint(
            "Australian English: the '.en' Whisper models plus the prompt biasing "
            "below handle Australian accents and spelling. Wake phrases are "
            "compared after local transcription, so nothing is sent anywhere."))
        prompt_form = QFormLayout()
        self.initial_prompt = QLineEdit(str(sp.initial_prompt))
        self.initial_prompt.setPlaceholderText("Biasing text for the transcriber")
        prompt_form.addRow("Accent bias prompt", self.initial_prompt)
        input_layout.addLayout(prompt_form)
        buttons = QHBoxLayout()
        self.test_mic = QPushButton("Test microphone")
        self.test_mic.clicked.connect(self._test_mic)
        self.refresh_mics = QPushButton("Re-scan microphones")
        self.refresh_mics.clicked.connect(self._load_devices)
        for button in (self.test_mic, self.refresh_mics):
            buttons.addWidget(button)
        buttons.addStretch(1)
        input_layout.addLayout(buttons)
        self.body.addWidget(input_frame)

        # --- output ----------------------------------------------------------
        output_frame, output_layout = card("Voice output")
        out_form = QFormLayout()
        self.tts_on = QCheckBox("Let JARVIS speak its replies")
        self.tts_on.setChecked(bool(sp.tts_enabled))
        self.narrate = QCheckBox("Speak short progress updates while working")
        self.narrate.setChecked(bool(sp.speak_progress_updates))
        out_form.addRow("", self.tts_on)
        out_form.addRow("", self.narrate)
        self.tts_engine = _combo(TTS_ENGINES, sp.tts_engine)
        self.tts_voice = QComboBox()
        self.tts_voice.setEditable(True)
        self.rate = QSlider(Qt.Orientation.Horizontal)
        self.rate.setRange(50, 200)
        self.rate.setValue(int(float(sp.tts_rate) * 100))
        self.rate_label = QLabel(f"{float(sp.tts_rate):.2f}×")
        self.rate.valueChanged.connect(
            lambda v: self.rate_label.setText(f"{v / 100.0:.2f}×"))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(float(sp.tts_volume) * 100))
        self.volume_label = QLabel(f"{int(float(sp.tts_volume) * 100)}%")
        self.volume.valueChanged.connect(
            lambda v: self.volume_label.setText(f"{v}%"))
        rate_row = QHBoxLayout()
        rate_row.addWidget(self.rate, 1)
        rate_row.addWidget(self.rate_label)
        volume_row = QHBoxLayout()
        volume_row.addWidget(self.volume, 1)
        volume_row.addWidget(self.volume_label)
        out_form.addRow("Engine", self.tts_engine)
        out_form.addRow("Voice", self.tts_voice)
        out_form.addRow("Rate", self._wrap(rate_row))
        out_form.addRow("Volume", self._wrap(volume_row))
        output_layout.addLayout(out_form)
        out_buttons = QHBoxLayout()
        self.test_voice = QPushButton("Test voice")
        self.test_voice.clicked.connect(self._test_voice)
        self.stop_voice = QPushButton("Stop speaking")
        self.stop_voice.setObjectName("danger")
        self.stop_voice.clicked.connect(self._stop_voice)
        self.refresh_voices = QPushButton("Re-scan voices")
        self.refresh_voices.clicked.connect(self._load_voices)
        for button in (self.test_voice, self.stop_voice, self.refresh_voices):
            out_buttons.addWidget(button)
        out_buttons.addStretch(1)
        output_layout.addLayout(out_buttons)
        output_layout.addWidget(hint(
            "Speech is generated sentence by sentence so JARVIS starts talking "
            "almost immediately. Interrupting it (barge-in) stops playback at once."))
        self.body.addWidget(output_frame)

        self.add_footer_button("Save voice settings", self._save, "primary")
        self.add_footer_button("Reload", self.refresh, "ghost")

    # ------------------------------------------------------------------
    def _wrap(self, layout: QHBoxLayout) -> QWidget:
        holder = QWidget()
        holder.setLayout(layout)
        return holder

    def refresh(self) -> None:
        sp = self.settings().speech
        for box, value in ((self.stt_on, sp.stt_enabled),
                           (self.tts_on, sp.tts_enabled),
                           (self.narrate, sp.speak_progress_updates),
                           (self.barge_in, sp.barge_in)):
            box.blockSignals(True)
            box.setChecked(bool(value))
            box.blockSignals(False)
        self._load_devices()
        self._load_voices()
        self._load_engines()
        self._apply_speech_status(self.controller.status().get("speech") or {})

    def _load_devices(self) -> None:
        current = self.microphone.currentData()
        self.microphone.clear()
        self.microphone.addItem("System default", "")
        try:
            info = self.controller.runtime.speech.devices() \
                if self.controller.runtime.speech is not None else {"devices": []}
        except Exception:  # noqa: BLE001
            info = {"devices": []}
        for device in info.get("devices", []):
            label = f"{device.get('name', '?')} ({device.get('channels', 1)} ch)"
            self.microphone.addItem(label, str(device.get("index")))
        if current is not None:
            index = self.microphone.findData(str(current))
            if index >= 0:
                self.microphone.setCurrentIndex(index)
        self.set_status(f"{len(info.get('devices', []))} microphone(s) found.")

    def _load_voices(self) -> None:
        current = str(self.settings().speech.tts_voice or "")
        self.tts_voice.clear()
        self.tts_voice.addItem("(engine default)", "")
        runtime = self.controller.runtime
        if runtime.speech is None:
            return
        try:
            voices = runtime.speech.voices()
        except Exception:  # noqa: BLE001
            voices = []
        for voice in voices:
            label = f"{voice.get('name', '?')} [{voice.get('engine', '')}]"
            self.tts_voice.addItem(label, str(voice.get("id", "")))
        index = self.tts_voice.findData(current)
        self.tts_voice.setCurrentIndex(index if index >= 0 else 0)

    def _load_engines(self) -> None:
        runtime = self.controller.runtime
        table = self.engine_table
        table.setRowCount(0)
        rows: List[tuple] = []
        if runtime.speech is None:
            rows.append(("speech subsystem", False, "not loaded"))
        else:
            try:
                engines = runtime.speech.engines()
            except Exception:  # noqa: BLE001
                engines = {"stt": [], "tts": []}
            for entry in engines.get("stt", []):
                rows.append((f"STT · {entry.get('engine')}", bool(entry.get("installed")),
                             str(entry.get("detail", ""))[:160]))
            for entry in engines.get("tts", []):
                rows.append((f"TTS · {entry.get('engine')}", bool(entry.get("installed")),
                             str(entry.get("detail", ""))[:160]))
            try:
                from ...speech.audio_io import list_input_devices

                _devices, note = list_input_devices()
                rows.append(("Audio capture", bool(note.startswith(("sounddevice", "pyaudio"))),
                             note[:160]))
            except Exception:  # noqa: BLE001
                pass
        for label, installed, detail in rows:
            row = table.rowCount()
            table.insertRow(row)
            name_item = QTableWidgetItem(label)
            state_item = QTableWidgetItem("yes" if installed else "no")
            state_item.setForeground(QColor(self.theme.colour(
                "success" if installed else "warning")))
            detail_item = QTableWidgetItem(detail)
            for item in (name_item, state_item, detail_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, name_item)
            table.setItem(row, 1, state_item)
            table.setItem(row, 2, detail_item)

    def _apply_speech_status(self, status: Dict[str, Any]) -> None:
        if not status:
            self.speech_status.setText(
                "The speech subsystem is not loaded, so voice input and output are "
                "unavailable. Text commands still work.")
            return
        stt = status.get("stt") or {}
        tts = status.get("tts") or {}
        wake = status.get("wake") or {}
        self.speech_status.setText(
            f"Mode: {status.get('mode', '?')}  ·  listening: "
            f"{'yes' if status.get('listening') else 'no'}\n"
            f"STT: {stt.get('engine', '?')} - {stt.get('detail', '')}\n"
            f"TTS: {tts.get('engine', '?')} - {tts.get('detail', '')}\n"
            f"Wake word: {wake.get('backend', '?')} (heard {wake.get('wake_count', 0)} "
            f"time(s)){(' - ' + wake.get('last_error')) if wake.get('last_error') else ''}")

    # ------------------------------------------------------------------
    def _save(self) -> None:
        result = self.controller.save_settings({"speech": {
            "stt_enabled": bool(self.stt_on.isChecked()),
            "tts_enabled": bool(self.tts_on.isChecked()),
            "speak_progress_updates": bool(self.narrate.isChecked()),
            "stt_engine": str(self.stt_engine.currentData()),
            "stt_model_size": self.stt_size.currentText(),
            "stt_device": str(self.stt_device.currentData()),
            "stt_compute_type": str(self.stt_compute.currentData()),
            "microphone": str(self.microphone.currentData() or ""),
            "sample_rate": int(self.sample_rate.value()),
            "activation_mode": str(self.mode.currentData()),
            "wake_phrases": [p.strip() for p in
                             self.wake_phrases.text().split(",") if p.strip()],
            "push_to_talk_key": self.ptt_key.text().strip() or "ctrl+space",
            "vad_aggressiveness": int(self.vad.value()),
            "barge_in": bool(self.barge_in.isChecked()),
            "initial_prompt": self.initial_prompt.text().strip(),
            "tts_engine": str(self.tts_engine.currentData()),
            "tts_voice": str(self.tts_voice.currentData() or ""),
            "tts_rate": round(self.rate.value() / 100.0, 2),
            "tts_volume": round(self.volume.value() / 100.0, 2),
        }})
        if result.get("ok"):
            self.set_status("Voice settings saved. Restarting the speech engines…", "ok")
            threading.Thread(target=self._restart_speech, daemon=True).start()
        else:
            self.set_status(f"Could not save: {result.get('error')}", "danger")

    def _restart_speech(self) -> None:
        runtime = self.controller.runtime
        if runtime.speech is None:
            return
        try:
            runtime.speech.stt.unload()
            runtime.speech.set_mode(self.settings().speech.activation_mode)
        except Exception:  # noqa: BLE001
            pass

    def _test_mic(self) -> None:
        runtime = self.controller.runtime
        if runtime.speech is None:
            QMessageBox.warning(self, "Voice unavailable",
                                "The speech subsystem is not loaded on this machine.")
            return
        self.set_status("Listening for five seconds…")
        self.test_mic.setEnabled(False)

        def _run() -> None:
            transcript = runtime.speech.listen_once(timeout_s=5.0, source="test")
            text = transcript.text.strip() if transcript.ok else ""
            message = (f"Heard: '{text}' ({transcript.engine}, "
                       f"{transcript.took_ms:.0f} ms)") if text else \
                ("Nothing was recognised. Check the microphone selection and the "
                 "voice gate setting.")
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, lambda: self._mic_done(message, bool(text)))

        threading.Thread(target=_run, name="jarvis-mic-test", daemon=True).start()

    def _mic_done(self, message: str, ok: bool) -> None:
        self.test_mic.setEnabled(True)
        self.set_status(message, "ok" if ok else "warn")

    def _test_voice(self) -> None:
        runtime = self.controller.runtime
        if runtime.speech is None:
            QMessageBox.warning(self, "Voice unavailable",
                                "The speech subsystem is not loaded on this machine.")
            return
        name = self.settings().general.assistant_name
        spoken = runtime.speech.speak(
            f"{name} voice check. Speech is generated locally, so nothing leaves "
            "this computer.")
        self.set_status("Speaking a test sentence…" if spoken else
                        "No voice engine is available. Install Piper or use "
                        "Windows SAPI.", "ok" if spoken else "warn")

    def _stop_voice(self) -> None:
        runtime = self.controller.runtime
        if runtime.speech is not None:
            runtime.speech.stop_speaking()
        self.set_status("Stopped speaking.")

    # ------------------------------------------------------------------
    def on_event(self, kind: str, data: Any) -> None:
        if kind in (K_SPEECH, K_STT, K_WAKE):
            self._apply_speech_status(self.controller.status().get("speech") or {})
            if kind == K_STT and (data or {}).get("kind") == "final":
                self.set_status(f"Heard: {(data or {}).get('text', '')}", "ok")

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.refresh()
