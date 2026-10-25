"""First-run setup wizard.

It walks a non-technical user through the only decisions that matter on day one:

1. what JARVIS is, and the promise that nothing leaves the PC;
2. finding Ollama and choosing a model (with honest guidance when it is missing);
3. voice: microphone test, spoken reply test, wake word;
4. security: what is always refused and what always needs approval;
5. a live screen-understanding check on this machine;
6. finishing, which saves settings and marks the first run complete.

Every test runs on a worker thread; the UI never blocks.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..speech.stt import WHISPER_RATE, describe_engines as stt_engines
from ..speech.tts import describe_engines as tts_engines
from .theme import Theme
from .widgets.core_visual import CorePanel

log = logging.getLogger(__name__)

WIZARD_STEPS = ("Welcome", "Ollama & model", "Voice", "Security", "Screen",
                "Finish")


class _Worker:
    """Run a callable off the GUI thread and report back here."""

    def __init__(self, parent: QWidget) -> None:
        self.parent = parent
        self._threads: List[threading.Thread] = []
        self.results: Dict[str, Any] = {}
        self._poll = QTimer(parent)
        self._poll.setInterval(80)
        self._poll.timeout.connect(self._tick)
        self._poll.start()
        self._done: List[Callable[[Any], None]] = []
        self._lock = threading.Lock()

    def run(self, key: str, fn: Callable[[], Any],
            on_done: Optional[Callable[[Any], None]] = None) -> None:
        def _work() -> None:
            try:
                value: Any = fn()
            except Exception as exc:  # noqa: BLE001
                value = {"error": str(exc)}
            with self._lock:
                self.results[key] = value
                if on_done is not None:
                    self._done.append((on_done, value))

        thread = threading.Thread(target=_work, name=f"wizard-{key}", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _tick(self) -> None:
        with self._lock:
            pending = list(self._done)
            self._done.clear()
        for callback, value in pending:
            try:
                callback(value)
            except Exception:  # noqa: BLE001
                log.debug("wizard callback failed", exc_info=True)

    def busy(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def stop(self) -> None:
        self._poll.stop()


class SetupWizard(QDialog):
    """Guided first run.  Finishing writes settings and marks setup complete."""

    def __init__(self, theme: Theme, controller: Any,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.controller = controller
        self.runtime = controller.runtime
        self.setWindowTitle("JARVIS first-time setup")
        self.setMinimumSize(760, 620)
        self.setModal(True)
        self.worker = _Worker(self)
        self._models: List[Dict[str, Any]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(12)

        header_row = QHBoxLayout()
        self.core = CorePanel(theme)
        self.core.setFixedSize(96, 96)
        header_row.addWidget(self.core)
        head = QVBoxLayout()
        self.title_label = QLabel("Welcome")
        self.title_label.setObjectName("sectionTitle")
        self.step_label = QLabel("Step 1 of 6")
        self.step_label.setObjectName("hint")
        head.addWidget(self.title_label)
        head.addWidget(self.step_label)
        head.addStretch(1)
        header_row.addLayout(head, 1)
        outer.addLayout(header_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, len(WIZARD_STEPS))
        self.progress.setValue(1)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        outer.addWidget(self.progress)

        self.stack = QStackedWidget()
        self.pages = [self._page_welcome(), self._page_ollama(), self._page_voice(),
                      self._page_security(), self._page_screen(), self._page_finish()]
        for page in self.pages:
            self.stack.addWidget(page)
        outer.addWidget(self.stack, 1)

        self.detail = QLabel("")
        self.detail.setObjectName("hint")
        self.detail.setWordWrap(True)
        self.detail.setMinimumHeight(34)
        outer.addWidget(self.detail)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.back_button = QPushButton("Back")
        self.back_button.setObjectName("ghost")
        self.back_button.clicked.connect(self._back)
        buttons.addWidget(self.back_button)
        self.next_button = QPushButton("Next")
        self.next_button.setObjectName("primary")
        self.next_button.clicked.connect(self._next)
        buttons.addWidget(self.next_button)
        self.cancel_button = QPushButton("Skip setup")
        self.cancel_button.setObjectName("ghost")
        self.cancel_button.clicked.connect(self._skip)
        buttons.addWidget(self.cancel_button)
        outer.addLayout(buttons)

        self.index = 0
        self._sync()

    # ------------------------------------------------------------------
    # pages
    # ------------------------------------------------------------------
    def _page_welcome(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        body = QLabel(
            "JARVIS is an AI assistant that lives entirely on this computer.\n\n"
            "It can look at your screen, control the mouse and keyboard, open and "
            "drive applications, remember what you tell it, and talk to you - and "
            "it checks that each action actually worked instead of guessing.\n\n"
            "Your privacy is not a setting, it is the design:\n"
            "• the model runs locally through Ollama - no cloud AI, no accounts\n"
            "• speech recognition and speech synthesis run locally\n"
            "• screenshots, voice and memories are never uploaded anywhere\n"
            "• there is no telemetry of any kind\n\n"
            "Setup takes about two minutes. Everything can be changed later in the "
            "pages on the left of the main window.")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(body)
        detected = QLabel("")
        detected.setObjectName("hint")
        detected.setWordWrap(True)
        try:
            install = self.runtime.ollama_install_info or {}
            detected.setText("Ollama: " + (
                f"found ({install.get('version', 'version unknown')})"
                if install.get("installed") else
                "not found yet - the next step explains what to do"))
        except Exception:  # noqa: BLE001
            pass
        layout.addWidget(detected)
        layout.addStretch(1)
        return page

    def _page_ollama(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)

        group = QGroupBox("Ollama server")
        form = QFormLayout(group)
        self.host_edit = QLineEdit(str(self.runtime.settings.ollama.host))
        self.port_edit = QLineEdit(str(self.runtime.settings.ollama.port))
        self.port_edit.setMaximumWidth(90)
        host_row = QHBoxLayout()
        host_row.addWidget(self.host_edit, 1)
        host_row.addWidget(QLabel("port"))
        host_row.addWidget(self.port_edit)
        form.addRow("Address", host_row)
        check_row = QHBoxLayout()
        self.check_button = QPushButton("Check connection")
        self.check_button.clicked.connect(self._check_ollama)
        self.check_status = QLabel("not checked yet")
        self.check_status.setObjectName("hint")
        self.check_status.setWordWrap(True)
        check_row.addWidget(self.check_button)
        check_row.addWidget(self.check_status, 1)
        form.addRow("", check_row)
        layout.addWidget(group)

        group2 = QGroupBox("Model")
        form2 = QFormLayout(group2)
        self.model_combo = QComboBox()
        self.refresh_button = QPushButton("Refresh list")
        self.refresh_button.clicked.connect(self._refresh_models)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.refresh_button)
        form2.addRow("Main model", model_row)
        self.auto_button = QPushButton("Pick the best installed model for me")
        self.auto_button.clicked.connect(self._auto_pick)
        form2.addRow("", self.auto_button)
        self.vision_combo = QComboBox()
        form2.addRow("Vision model (optional)", self.vision_combo)
        self.fast_combo = QComboBox()
        form2.addRow("Fast model (optional)", self.fast_combo)
        self.test_button = QPushButton("Test the selected model")
        self.test_button.clicked.connect(self._test_model)
        form2.addRow("", self.test_button)
        layout.addWidget(group2)

        install = QPushButton("Open the Ollama download page")
        install.setObjectName("ghost")
        install.clicked.connect(self._open_ollama_site)
        layout.addWidget(install)
        layout.addWidget(self._muted(
            "No model? Install Ollama (free, local) and run one command in its "
            "window, for example:\n"
            "    ollama pull qwen2.5:7b-instruct\n"
            "A 7-8B instruction-tuned model fits comfortably in 8 GB of VRAM and "
            "is a good starting point. JARVIS works best with a model that "
            "supports tool calling. Setup can be finished later - the Models page "
            "does the same job."))
        layout.addStretch(1)
        return page

    def _page_voice(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)

        self.speech_enabled = QCheckBox("Enable voice (speech recognition and spoken "
                                        "replies)")
        self.speech_enabled.setChecked(bool(self.runtime.settings.speech.stt_enabled)
                                       or bool(self.runtime.settings.speech.tts_enabled))
        layout.addWidget(self.speech_enabled)

        stt = stt_engines()
        tts = tts_engines()
        report = self._muted(
            "Speech recognition available on this PC:\n" +
            (self._engine_lines(stt) or "  none - voice input will be unavailable") +
            "\n\nSpeech synthesis available:\n" +
            (self._engine_lines(tts) or "  none - JARVIS will reply in text only"))
        layout.addWidget(report)

        group = QGroupBox("Listening")
        form = QFormLayout(group)
        self.mode_combo = QComboBox()
        for value, label in (("wakeword", 'Wake word ("Hey JARVIS")'),
                             ("push_to_talk", "Push to talk (Ctrl+Space)"),
                             ("button", "Microphone button only"),
                             ("continuous", "Always listening"),
                             ("disabled", "Off")):
            self.mode_combo.addItem(label, value)
        index = self.mode_combo.findData(
            str(self.runtime.settings.speech.activation_mode))
        self.mode_combo.setCurrentIndex(max(0, index))
        self.device_combo = QComboBox()
        self.rescan_button = QPushButton("Re-scan")
        self.rescan_button.setObjectName("ghost")
        self.rescan_button.clicked.connect(self._scan_devices)
        device_row = QHBoxLayout()
        device_row.addWidget(self.device_combo, 1)
        device_row.addWidget(self.rescan_button)
        form.addRow("Mode", self.mode_combo)
        form.addRow("Microphone", device_row)
        layout.addWidget(group)

        group2 = QGroupBox("Speaking")
        form2 = QFormLayout(group2)
        self.voice_combo = QComboBox()
        self.voice_button = QPushButton("Preview voice")
        self.voice_button.clicked.connect(self._preview_voice)
        self.mic_button = QPushButton("Test microphone")
        self.mic_button.clicked.connect(self._test_mic)
        voice_row = QHBoxLayout()
        voice_row.addWidget(self.voice_combo, 1)
        voice_row.addWidget(self.voice_button)
        form2.addRow("Voice", voice_row)
        form2.addRow("", self.mic_button)
        layout.addWidget(group2)
        layout.addStretch(1)
        return page

    def _page_security(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.addWidget(QLabel(
            "JARVIS enforces its rules in code. The model cannot talk its way past "
            "them, and neither can a prompt in a file or a web page."))
        self.master_switch = QCheckBox("Allow JARVIS to control this computer "
                                       "(mouse, keyboard, windows, files)")
        self.master_switch.setChecked(
            bool(self.runtime.settings.security.automation_master_switch))
        layout.addWidget(self.master_switch)
        self.strict_mode = QCheckBox("Start in strict mode: everything that changes "
                                     "something asks first")
        self.strict_mode.setChecked(str(self.runtime.settings.security.mode) == "strict")
        layout.addWidget(self.strict_mode)
        layout.addWidget(self._muted(
            "Always refused, no exceptions:\n"
            "  • uninstalling or installing system software, anything needing "
            "administrator rights or a UAC prompt\n"
            "  • reading or writing the Windows registry\n"
            "  • changing Windows Defender, the firewall or other security software\n"
            "  • touching system files, or creating/changing/removing user accounts\n"
            "  • reading passwords, saved credentials, cookies or tokens\n"
            "  • uploading anything - there is no cloud AI and no telemetry\n\n"
            "Always asks you first, every single time, showing what will happen, "
            "which app it affects, the consequences and whether it can be undone:\n"
            "  • downloads, sending messages or emails or posts\n"
            "  • deleting files permanently, overwriting files, bulk moves\n"
            "  • banking or payment sites, and sensitive personal information\n\n"
            "Password fields are masked in every screenshot JARVIS takes.\n"
            "You can make any of these stricter on the Permissions page. You can "
            "never make them weaker."))
        layout.addStretch(1)
        return page

    def _page_screen(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.addWidget(QLabel(
            "JARVIS understands your screen three ways: the window list, UI "
            "Automation (the control tree, which is the most reliable), and OCR on a "
            "screenshot. Let's see what this PC offers."))
        self.cap_label = self._muted("Probing…")
        self.cap_label.setWordWrap(True)
        layout.addWidget(self.cap_label)
        self.probe_button = QPushButton("Probe again")
        self.probe_button.clicked.connect(self._probe_screen)
        layout.addWidget(self.probe_button)
        self.look_button = QPushButton("Look at my screen now")
        self.look_button.setObjectName("primary")
        self.look_button.clicked.connect(self._observe_now)
        layout.addWidget(self.look_button)
        self.observe_label = self._muted("")
        self.observe_label.setWordWrap(True)
        layout.addWidget(self.observe_label)
        layout.addWidget(self._muted(
            "If nothing is available here, JARVIS still works: it falls back to the "
            "backends that are installed and tells you honestly what it cannot do. "
            "On Windows with the optional components installed, UI Automation and "
            "OCR are both available."))
        layout.addStretch(1)
        return page

    def _page_finish(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary_label)
        layout.addWidget(self._muted(
            "Resource ceilings are fixed in code: JARVIS will not let its own "
            "process exceed 12 GB of RAM or 8 GB of GPU memory. It monitors itself "
            "continuously and sheds load - smaller screenshots, no background "
            "observation, unloading models - before it approaches them, and says so "
            "when it does.\n\n"
            "Click Finish to save. Everything here can be changed later."))
        layout.addStretch(1)
        return page

    def _muted(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("hint")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _engine_lines(report: Any) -> str:
        if isinstance(report, dict):
            entries = report.items()
        elif isinstance(report, (list, tuple)):
            entries = [(item.get("name", "?"), item) if isinstance(item, dict)
                       else (str(item), {}) for item in report]
        else:
            return str(report)
        lines = []
        for name, info in entries:
            info = info if isinstance(info, dict) else {}
            state = "yes" if info.get("available") else "no"
            extra = info.get("reason") or info.get("hint") or ""
            lines.append(f"  • {name}: {state}" + (f" - {extra}" if extra else ""))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # navigation
    # ------------------------------------------------------------------
    def _sync(self) -> None:
        self.stack.setCurrentIndex(self.index)
        self.title_label.setText(WIZARD_STEPS[self.index])
        self.step_label.setText(f"Step {self.index + 1} of {len(WIZARD_STEPS)}")
        self.progress.setValue(self.index + 1)
        self.back_button.setEnabled(self.index > 0)
        self.next_button.setText("Finish" if self.index == len(WIZARD_STEPS) - 1
                                 else "Next")
        self.cancel_button.setVisible(self.index < len(WIZARD_STEPS) - 1)
        self.core.set_state(("thinking", "listening", "acting", "speaking",
                             "observing", "idle")[self.index % 6])
        details = [
            "What JARVIS is, and what it will never do.",
            "Connect to the local model server and choose a model.",
            "Test your microphone and hear JARVIS's voice.",
            "Read the rules that are written into the code.",
            "Check what JARVIS can see on this machine.",
            "Save and start.",
        ]
        self.detail.setText(details[self.index])
        if self.index == 1:
            self._check_ollama()
        elif self.index == 2:
            self._scan_devices()
        elif self.index == 4:
            self._probe_screen()
        elif self.index == 5:
            self._fill_summary()

    def _next(self) -> None:
        if self.index >= len(WIZARD_STEPS) - 1:
            self._finish()
            return
        if self.index == 2:
            self._collect_voice()
        elif self.index == 3:
            self._collect_security()
        self.index += 1
        self._sync()

    def _back(self) -> None:
        if self.index == 2:
            self._collect_voice()
        self.index = max(0, self.index - 1)
        self._sync()

    def _skip(self) -> None:
        answer = QMessageBox.question(
            self, "Skip setup",
            "Finish without saving these choices? JARVIS will start with its "
            "defaults, and you can run this wizard again from the General page.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.worker.stop()
        self.reject()

    def _finish(self) -> None:
        self._collect_voice()
        self._collect_security()
        changes: Dict[str, Any] = {"general": {"first_run_complete": True}}
        ollama_section: Dict[str, Any] = {}
        host = self.host_edit.text().strip() or "127.0.0.1"
        try:
            port = int(self.port_edit.text().strip() or 11434)
        except ValueError:
            port = 11434
        if host != str(self.runtime.settings.ollama.host):
            ollama_section["host"] = host
        if port != int(self.runtime.settings.ollama.port):
            ollama_section["port"] = port
        main_model = str(self.model_combo.currentData() or "")
        if main_model:
            ollama_section["main_model"] = main_model
        vision_model = str(self.vision_combo.currentData() or "")
        if vision_model:
            ollama_section["vision_model"] = vision_model
        fast_model = str(self.fast_combo.currentData() or "")
        if fast_model:
            ollama_section["fast_model"] = fast_model
        if ollama_section:
            changes["ollama"] = ollama_section
        result = self.controller.save_settings(changes)
        self.worker.stop()
        if not result.get("ok"):
            QMessageBox.warning(self, "Setup",
                                f"Settings could not be saved: {result.get('error')}")
        self.accept()

    def _collect_voice(self) -> None:
        enabled = bool(self.speech_enabled.isChecked())
        self.controller.save_settings({"speech": {
            "stt_enabled": enabled,
            "tts_enabled": enabled,
            "activation_mode": str(self.mode_combo.currentData() or "push_to_talk"),
            "microphone": str(self.device_combo.currentData() or ""),
            "tts_voice": str(self.voice_combo.currentData() or ""),
        }})

    def _collect_security(self) -> None:
        self.controller.save_settings({"security": {
            "automation_master_switch": bool(self.master_switch.isChecked()),
            "mode": "strict" if self.strict_mode.isChecked() else "balanced",
        }})

    # ------------------------------------------------------------------
    # Ollama
    # ------------------------------------------------------------------
    def _check_ollama(self) -> None:
        self.check_status.setText("checking…")
        host = self.host_edit.text().strip() or "127.0.0.1"
        try:
            port = int(self.port_edit.text().strip() or 11434)
        except ValueError:
            port = 11434

        def _work() -> Dict[str, Any]:
            client = self.runtime.ollama
            try:
                client.settings.host = host
                client.settings.port = port
            except Exception:  # noqa: BLE001
                pass
            return client.status()

        def _done(value: Any) -> None:
            status = value if isinstance(value, dict) else {}
            if status.get("connected"):
                version = status.get("version") or ""
                self.check_status.setText("Connected to Ollama"
                                          + (f" {version}." if version else ".")
                                          + " Loading your models…")
                self._refresh_models()
            else:
                self.check_status.setText(
                    "Not reachable: " + str(status.get("error", "unknown problem")) +
                    "\nStart Ollama, then click Check again. If it is not installed, "
                    "use the download button below.")

        self.worker.run("ollama", _work, _done)

    def _refresh_models(self) -> None:
        def _work() -> Any:
            return self.runtime.ollama.list_models(force=True)

        def _done(value: Any) -> None:
            models = value if isinstance(value, list) else []
            self._models = []
            current_main = str(self.runtime.settings.ollama.main_model or "")
            for combo in (self.model_combo, self.vision_combo, self.fast_combo):
                combo.clear()
                combo.addItem("(not set)", "")
            for model in models:
                info = model.as_dict() if hasattr(model, "as_dict") else dict(model)
                self._models.append(info)
                name = str(info.get("name", ""))
                if not name:
                    continue
                caps = []
                if info.get("supports_tools"):
                    caps.append("tools")
                if info.get("supports_vision"):
                    caps.append("vision")
                label = f"{name}  ·  {info.get('size_gb', 0):.1f} GB"
                if caps:
                    label += "  ·  " + "/".join(caps)
                self.model_combo.addItem(label, name)
                self.vision_combo.addItem(label, name)
                self.fast_combo.addItem(label, name)
            if current_main:
                index = self.model_combo.findData(current_main)
                if index >= 0:
                    self.model_combo.setCurrentIndex(index)
            self.check_status.setText(
                f"{len(self._models)} model(s) installed." if self._models
                else "No models installed yet - pull one, then refresh.")

        self.worker.run("models", _work, _done)

    def _auto_pick(self) -> None:
        def _work() -> Any:
            return self.runtime.router.auto_select_main()

        def _done(value: Any) -> None:
            name = value if isinstance(value, str) else str(value or "")
            if name:
                index = self.model_combo.findData(name)
                if index >= 0:
                    self.model_combo.setCurrentIndex(index)
                QMessageBox.information(self, "Model chosen",
                                        f"JARVIS picked {name}.\n\nIt prefers a model "
                                        "that supports tool calling, then the largest "
                                        "one that fits your memory budget.")
            else:
                QMessageBox.information(
                    self, "No model chosen",
                    "No installed model looks suitable yet. Pull one first - the "
                    "Models page can do it for you with your approval.")

        self.worker.run("auto", _work, _done)

    def _test_model(self) -> None:
        model = str(self.model_combo.currentData() or "")
        if not model:
            QMessageBox.information(self, "No model", "Choose a model first.")
            return
        self.detail.setText(f"Testing {model}…")
        self.core.set_state("thinking")

        def _work() -> Any:
            client = self.runtime.ollama
            started = time.time()
            result = client.chat(
                [{"role": "user", "content": "Reply with the single word: ready"}],
                model=model, stream=False, options={"temperature": 0.0}, timeout=90.0)
            return result, started

        def _done(value: Any) -> None:
            result, started = value
            took = time.time() - started
            self.core.set_state("idle")
            if getattr(result, "ok", False):
                rate = float(getattr(result, "tokens_per_second", 0.0) or 0.0)
                self.detail.setText(
                    f"{model} replied in {took:.1f}s"
                    + (f" at {rate:.0f} tokens/s." if rate else ".")
                    + f" It said: {str(result.content)[:80]!r}")
            else:
                self.detail.setText(f"{model} failed: {getattr(result, 'error', '?')}")

        self.worker.run("model-test", _work, _done)

    def _open_ollama_site(self) -> None:
        url = "https://ollama.com/download/windows"
        try:
            url = str(self.runtime.ollama_install_info.get("install_url") or url)
        except Exception:  # noqa: BLE001
            pass
        QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------------
    # voice
    # ------------------------------------------------------------------
    def _scan_devices(self) -> None:
        def _work() -> Any:
            speech = getattr(self.runtime, "speech", None)
            info: Dict[str, Any] = {"devices": [], "voices": []}
            if speech is not None:
                try:
                    found = speech.devices() or {}
                    info["devices"] = list(found.get("devices") or [])
                    info["selected"] = found.get("selected", "")
                    info["backend"] = found.get("backend", "")
                except Exception:  # noqa: BLE001
                    log.debug("device scan failed", exc_info=True)
                try:
                    info["voices"] = list(speech.voices() or [])
                except Exception:  # noqa: BLE001
                    log.debug("voice scan failed", exc_info=True)
            return info

        def _done(value: Any) -> None:
            data = value if isinstance(value, dict) else {}
            devices = data.get("devices") or []
            voices = data.get("voices") or []
            selected = str(data.get("selected") or "")
            self.device_combo.clear()
            self.device_combo.addItem("System default", "")
            for device in devices:
                name = str(device.get("name", "?"))
                ident = str(device.get("index", ""))
                label = name + (f" ({device.get('channels', 1)} ch)"
                                if device.get("channels") else "")
                self.device_combo.addItem(label, ident)
            if selected:
                index = self.device_combo.findData(selected)
                if index >= 0:
                    self.device_combo.setCurrentIndex(index)
            current_voice = str(self.runtime.settings.speech.tts_voice or "")
            self.voice_combo.clear()
            self.voice_combo.addItem("(engine default)", "")
            for voice in voices:
                name = str(voice.get("name") or voice.get("id") or "?")
                label = f"{name}  [{voice.get('engine', '')}]"
                self.voice_combo.addItem(label, str(voice.get("id", "")))
            if current_voice:
                index = self.voice_combo.findData(current_voice)
                if index >= 0:
                    self.voice_combo.setCurrentIndex(index)
            if not devices:
                note = str(data.get("note") or data.get("backend") or "")
                self.detail.setText(
                    "No microphone was found"
                    + (f" ({note})" if note else "")
                    + ". Voice input needs an audio device; you can still type, "
                      "and re-scan after plugging one in.")

        self.worker.run("devices", _work, _done)

    def _test_mic(self) -> None:
        self.detail.setText("Listening for up to 6 seconds - say something…")
        self.core.set_state("listening")

        def _work() -> Any:
            speech = getattr(self.runtime, "speech", None)
            if speech is None:
                return {"error": "no speech subsystem"}
            transcript = speech.listen_once(timeout_s=6.0, source="wizard")
            return transcript.as_dict() if hasattr(transcript, "as_dict") else {}

        def _done(value: Any) -> None:
            self.core.set_state("idle")
            data = value if isinstance(value, dict) else {}
            if data.get("ok"):
                self.detail.setText(f"Heard: “{str(data.get('text', ''))[:120]}”  "
                                    f"({data.get('engine', 'engine')}, "
                                    f"{data.get('took_ms', 0):.0f} ms)")
            else:
                self.detail.setText("Nothing was recognised: "
                                    f"{data.get('error') or data.get('reason') or 'no speech detected'}")

        self.worker.run("mic", _work, _done)

    def _preview_voice(self) -> None:
        self._collect_voice()
        self.detail.setText("Speaking…")
        self.core.set_state("speaking")

        def _work() -> Any:
            speech = getattr(self.runtime, "speech", None)
            if speech is None:
                return {"spoken": False, "error": "the speech subsystem is not loaded"}
            spoken = bool(speech.speak("Systems online. I am JARVIS, and everything "
                                       "I do stays on this computer."))
            note = ""
            if not spoken:
                status = speech.status() if hasattr(speech, "status") else {}
                note = str(((status.get("tts") or {}).get("detail"))
                           or (status.get("notes") or {}).get("tts") or "")
            return {"spoken": spoken, "error": note}

        def _done(value: Any) -> None:
            self.core.set_state("idle")
            data = value if isinstance(value, dict) else {}
            if data.get("spoken"):
                self.detail.setText("If you heard that, speech output is working.")
            else:
                self.detail.setText(
                    "No voice is available"
                    + (f": {data.get('error')}" if data.get("error") else "")
                    + ". JARVIS will reply in text; install a voice later from the "
                      "Voice page.")

        self.worker.run("voice-preview", _work, _done)

    # ------------------------------------------------------------------
    # screen
    # ------------------------------------------------------------------
    def _probe_screen(self) -> None:
        def _work() -> str:
            return self.controller.platform_summary()

        def _done(value: Any) -> None:
            self.cap_label.setText(str(value or "No capability report available."))

        self.worker.run("caps", _work, _done)

    def _observe_now(self) -> None:
        self.detail.setText("Looking at the screen…")
        self.core.set_state("observing")

        def _work() -> Any:
            vision = getattr(self.runtime, "vision", None)
            if vision is None:
                return {"error": "vision is not loaded on this machine"}
            try:
                observation = vision.observe(reason="wizard", include_image=False)
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc)}
            return observation.to_dict(include_elements=False)

        def _done(value: Any) -> None:
            self.core.set_state("idle")
            data = value if isinstance(value, dict) else {}
            if data.get("error"):
                self.observe_label.setText(f"Could not observe: {data.get('error')}")
                return
            window = data.get("active_window") or {}
            title = window.get("title") or "(no focused window)"
            process = window.get("process_name") or ""
            self.observe_label.setText(
                f"Focused window: {title}"
                + (f"  [{process}]" if process else "") + "\n"
                f"Controls found: {data.get('element_count', 0)}    "
                f"OCR lines: {len(data.get('ocr_lines') or [])}    "
                f"OCR engine: {data.get('ocr_engine') or 'none'}    "
                f"Took: {float(data.get('duration_ms', 0) or 0):.0f} ms\n"
                + ("\n".join(str(x)[:90] for x in (data.get("notes") or [])[:3])
                   or "That is what JARVIS sees before it decides what to do."))

        self.worker.run("observe", _work, _done)

    # ------------------------------------------------------------------
    def _fill_summary(self) -> None:
        settings = self.runtime.settings_provider()
        main_model = str(self.model_combo.currentData() or
                         settings.ollama.main_model or "not set")
        lines = [
            f"Assistant name: {settings.general.assistant_name}",
            f"Ollama: {settings.ollama.host}:{settings.ollama.port}",
            f"Main model: {main_model}",
            f"Voice: {'on' if self.speech_enabled.isChecked() else 'off'}"
            f"  ({self.mode_combo.currentText().lower()})",
            f"Automation: {'allowed' if self.master_switch.isChecked() else 'off'}"
            f"  ({'strict' if self.strict_mode.isChecked() else 'balanced'} mode)",
            f"Data folder: {paths.data_dir()}",
        ]
        self.summary_label.setText("\n".join(lines))

    def done(self, result: int) -> None:  # noqa: D102 - Qt override
        try:
            self.worker.stop()
        except Exception:  # noqa: BLE001
            pass
        super().done(result)


__all__ = ["SetupWizard", "WIZARD_STEPS", "WHISPER_RATE"]
