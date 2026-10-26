"""Dashboard: the core visualiser, live vitals and quick actions."""

from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..controller import (
    K_METRICS,
    K_OLLAMA,
    K_SPEECH,
    K_STATE,
    K_TASK,
)
from ..theme import Theme
from ..widgets.core_visual import CorePanel
from .base import Page, card, hint


class _StatCard(QWidget):
    def __init__(self, theme: Theme, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.theme = theme
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("hint")
        self.value_label = QLabel("—")
        self.value_label.setObjectName("valueBig")
        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        self.setStyleSheet(
            f"background-color: {theme.colour('bg_raised')};"
            f" border: 1px solid {theme.colour('border')};"
            f" border-radius: {int(theme.metrics['radius'])}px;")

    def set_value(self, text: str, role: str = "") -> None:
        self.value_label.setText(text)
        colour = {"ok": self.theme.colour("success"),
                  "warn": self.theme.colour("warning"),
                  "danger": self.theme.colour("danger"),
                  "dim": self.theme.colour("text_dim")}.get(role,
                                                            self.theme.colour("text"))
        self.value_label.setStyleSheet(f"color: {colour};")


class DashboardPage(Page):
    """The landing page: what JARVIS is, what it is doing, and how healthy it is."""

    def __init__(self, theme: Theme, controller: Any,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(theme, controller, "Dashboard",
                         "JARVIS runs entirely on this computer. Nothing is uploaded.",
                         parent)

        top = QHBoxLayout()
        top.setSpacing(14)

        left_frame, left = card("Core")
        self.core_panel = CorePanel(theme)
        self.core_panel.interrupted.connect(self._interrupt)
        left.addWidget(self.core_panel)
        self.core_detail = hint("Standing by.")
        left.addWidget(self.core_detail)
        top.addWidget(left_frame, 2)

        right_frame, right = card("Vitals")
        grid = QGridLayout()
        grid.setSpacing(10)
        self.model_card = _StatCard(theme, "Model")
        self.tools_card = _StatCard(theme, "Tools ready")
        self.ram_card = _StatCard(theme, "RAM (ceiling 12 GB)")
        self.vram_card = _StatCard(theme, "VRAM (ceiling 8 GB)")
        self.memory_card = _StatCard(theme, "Memories stored")
        self.voice_card = _StatCard(theme, "Voice")
        grid.addWidget(self.model_card, 0, 0)
        grid.addWidget(self.tools_card, 0, 1)
        grid.addWidget(self.ram_card, 1, 0)
        grid.addWidget(self.vram_card, 1, 1)
        grid.addWidget(self.memory_card, 2, 0)
        grid.addWidget(self.voice_card, 2, 1)
        right.addLayout(grid)

        quick_frame, quick = card("Quick actions")
        quick_row = QHBoxLayout()
        quick_row.setSpacing(8)
        self.observe_button = QPushButton("Look at the screen")
        self.observe_button.clicked.connect(self._observe)
        self.screenshot_button = QPushButton("Take a screenshot")
        self.screenshot_button.clicked.connect(self._screenshot)
        self.listen_button = QPushButton("Speak a command")
        self.listen_button.clicked.connect(self._listen)
        for button in (self.observe_button, self.screenshot_button, self.listen_button):
            quick_row.addWidget(button)
        quick.addLayout(quick_row)
        self.quick_note = hint("Quick actions run the same tools, with the same "
                               "security checks, as a spoken or typed request.")
        quick.addWidget(self.quick_note)

        right.addWidget(quick_frame)
        top.addWidget(right_frame, 3)
        self.body.addLayout(top)
        self.body.addStretch(1)

        self._last_state = "idle"

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        status = self.controller.status()
        self._apply_status(status)
        runtime = self.controller.runtime
        try:
            stats = runtime.memory.stats()
            self.memory_card.set_value(
                f"{stats.get('facts', 0)} facts · {stats.get('tasks', 0)} tasks")
        except Exception:  # noqa: BLE001
            pass

    def _apply_status(self, status: Dict[str, Any]) -> None:
        ollama = status.get("ollama") or {}
        if not ollama.get("reachable"):
            self.model_card.set_value("Ollama offline", "danger")
        else:
            model = ollama.get("main_model") or "none selected"
            self.model_card.set_value(str(model)[:22],
                                      "ok" if ollama.get("main_model") else "warn")
        tools = status.get("tools") or {}
        total = int(tools.get("total", 0))
        available = int(tools.get("available", 0))
        self.tools_card.set_value(f"{available}/{total}",
                                  "ok" if available == total else "warn")
        resources = status.get("resources") or {}
        ram = float(resources.get("ram_gb", 0.0))
        ram_budget = float(resources.get("ram_budget_gb", 12.0)) or 12.0
        vram = float(resources.get("vram_gb", 0.0))
        vram_budget = float(resources.get("vram_budget_gb", 8.0)) or 8.0
        self.ram_card.set_value(f"{ram:.1f} / {ram_budget:.0f} GB",
                                _usage_role(ram, ram_budget))
        self.vram_card.set_value(f"{vram:.1f} / {vram_budget:.0f} GB",
                                 _usage_role(vram, vram_budget))
        speech = status.get("speech") or {}
        stt_ok = bool((speech.get("stt") or {}).get("available"))
        tts_ok = bool((speech.get("tts") or {}).get("available"))
        self.voice_card.set_value(
            f"{'STT ✓' if stt_ok else 'STT ✗'}  {'TTS ✓' if tts_ok else 'TTS ✗'}",
            "ok" if (stt_ok and tts_ok) else "warn")
        platform = status.get("platform") or {}
        if platform.get("simulated"):
            reason = (platform.get("notes") or {}).get("fallback_reason", "")
            self.core_detail.setText(
                "DEMO MODE - actions are applied to a simulated desktop, not your "
                "real one." + (f" Reason: {reason}" if reason else ""))
        else:
            self.core_detail.setText("Controlling this computer directly.")
        self.listen_button.setEnabled(stt_ok)

    # ------------------------------------------------------------------
    def on_event(self, kind: str, data: Any) -> None:
        if kind == K_STATE:
            state = str((data or {}).get("state", "idle"))
            self._last_state = state
            self.core_panel.set_state(state)
            self.core_panel.set_detail(_state_detail(state))
        elif kind == K_METRICS:
            self._apply_status(self.controller.status())
        elif kind == K_OLLAMA:
            self._apply_status(self.controller.status())
        elif kind == K_SPEECH:
            self._apply_status(self.controller.status())
        elif kind == K_TASK:
            payload = dict(data or {})
            stage = payload.get("stage")
            if stage == "started":
                self.core_panel.set_detail(f"Working on: {payload.get('request', '')}")
            elif stage == "finished":
                summary = str(payload.get("summary", ""))
                if summary:
                    self.core_panel.set_detail(summary[:160])
        elif kind == "level":
            self.core_panel.set_levels(mic=float((data or {}).get("level", 0.0)))
        elif kind == "tts_level":
            self.core_panel.set_levels(tts=float((data or {}).get("level", 0.0)))

    # ------------------------------------------------------------------
    def _interrupt(self) -> None:
        self.controller.stop()
        self.core_panel.set_detail("Stopped.")

    def _observe(self) -> None:
        runtime = self.controller.runtime
        if runtime.vision is None:
            self.set_status("Vision is not available.", "warn")
            return
        observation = runtime.vision.observe(reason="dashboard")
        window = observation.active_window or {}
        self.set_status(f"Observed: {window.get('title') or 'the desktop'}"
                        f" · {len(observation.elements)} controls"
                        f" · OCR {observation.ocr.engine}", "ok")
        self.conversation.append_notice(
            "Screen read",
            observation.prompt_text(max_chars=600) or "Nothing recognisable on screen.")

    def _screenshot(self) -> None:
        runtime = self.controller.runtime
        result = runtime.registry.execute("take_screenshot", {})
        self.set_status(result.message or result.error or "", "ok" if result.ok else "danger")
        path = str((result.data or {}).get("path", ""))
        if path:
            self.conversation.append_notice("Screenshot saved", path, "ok")

    def _listen(self) -> None:
        self.controller.listen_once()
        self.set_status("Listening…", "ok")


def _usage_role(value: float, budget: float) -> str:
    if budget <= 0:
        return "dim"
    ratio = value / budget
    if ratio >= 0.9:
        return "danger"
    if ratio >= 0.75:
        return "warn"
    return "ok"


def _state_detail(state: str) -> str:
    return {
        "idle": "Standing by.",
        "listening": "Listening to the microphone…",
        "thinking": "Working out what to do…",
        "executing": "Controlling the computer…",
        "observing": "Looking at the screen…",
        "speaking": "Speaking…",
        "waiting_for_approval": "Waiting for your approval.",
        "error": "Something went wrong.",
    }.get(state, "")
