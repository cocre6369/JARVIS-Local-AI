"""Live system status strip.

Shows the things a user needs at a glance and nothing else: what JARVIS is doing,
which model is answering, how close the resource ceilings are, whether the
desktop is real or simulated, and whether the voice path works.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import Theme


def _bar(theme: Theme, colour: str) -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, 100)
    bar.setValue(0)
    bar.setTextVisible(False)
    bar.setFixedHeight(6)
    bar.setFixedWidth(84)
    bar.setStyleSheet(
        f"QProgressBar {{ background-color: {theme.colour('bg_raised')};"
        f" border: 1px solid {theme.colour('border')}; border-radius: 3px; }}"
        f"QProgressBar::chunk {{ background-color: {colour}; border-radius: 2px; }}")
    return bar


class StatusStrip(QFrame):
    """The always-visible status row at the bottom of the window."""

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.setObjectName("statusBar")
        self.setFixedHeight(34)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 3, 12, 3)
        layout.setSpacing(14)

        self.state_label = QLabel("Idle")
        self.state_label.setObjectName("statusBarLabel")
        layout.addWidget(self.state_label)

        self.demo_label = QLabel("DEMO MODE")
        self.demo_label.setStyleSheet(
            f"color: {theme.colour('warning')}; font-weight: 700;"
            f" border: 1px solid {theme.colour('warning')};"
            f" border-radius: 4px; padding: 1px 6px;")
        self.demo_label.setVisible(False)
        layout.addWidget(self.demo_label)

        self.model_label = QLabel("model: -")
        self.model_label.setObjectName("statusBarLabel")
        layout.addWidget(self.model_label)

        self.tools_label = QLabel("tools: -")
        self.tools_label.setObjectName("statusBarLabel")
        layout.addWidget(self.tools_label)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(spacer)

        self.ram_label = QLabel("RAM")
        self.ram_label.setObjectName("statusBarLabel")
        layout.addWidget(self.ram_label)
        self.ram_bar = _bar(theme, theme.colour("accent"))
        layout.addWidget(self.ram_bar)

        self.vram_label = QLabel("VRAM")
        self.vram_label.setObjectName("statusBarLabel")
        layout.addWidget(self.vram_label)
        self.vram_bar = _bar(theme, theme.colour("accent_secondary"))
        layout.addWidget(self.vram_bar)

        self.pressure_label = QLabel("●")
        self.pressure_label.setToolTip("Resource pressure")
        layout.addWidget(self.pressure_label)

        self.voice_label = QLabel("voice: -")
        self.voice_label.setObjectName("statusBarLabel")
        layout.addWidget(self.voice_label)

    # ------------------------------------------------------------------
    def set_state(self, state: str) -> None:
        self.state_label.setText(self.theme.state_label(state))
        self.state_label.setStyleSheet(
            f"color: {self.theme.state_colour(state)}; font-weight: 600;")

    def set_demo_mode(self, simulated: bool, reason: str = "") -> None:
        self.demo_label.setVisible(bool(simulated))
        if simulated:
            self.demo_label.setToolTip(
                "Demo Mode: actions are applied to a simulated desktop, not your "
                "real one." + (f"\nWhy: {reason}" if reason else ""))

    def set_models(self, main_model: str, available: bool) -> None:
        if not available:
            self.model_label.setText("model: Ollama not reachable")
            self.model_label.setStyleSheet(f"color: {self.theme.colour('danger')};")
            return
        self.model_label.setText(f"model: {main_model or 'none selected'}")
        self.model_label.setStyleSheet(
            f"color: {self.theme.colour('text_dim') if main_model else self.theme.colour('warning')};")

    def set_tools(self, total: int, available: int) -> None:
        self.tools_label.setText(f"tools: {available}/{total}")

    def set_resources(self, ram_gb: float, ram_budget: float, vram_gb: float,
                      vram_budget: float, pressure: str) -> None:
        ram_pct = int(min(100, (ram_gb / ram_budget) * 100)) if ram_budget else 0
        vram_pct = int(min(100, (vram_gb / vram_budget) * 100)) if vram_budget else 0
        self.ram_bar.setValue(ram_pct)
        self.vram_bar.setValue(vram_pct)
        self.ram_label.setText(f"RAM {ram_gb:.1f}/{ram_budget:.0f} GB")
        self.vram_label.setText(f"VRAM {vram_gb:.1f}/{vram_budget:.0f} GB")
        colour = self.theme.pressure_colour(pressure)
        self.pressure_label.setText("●")
        self.pressure_label.setStyleSheet(f"color: {colour}; font-size: 13pt;")
        self.pressure_label.setToolTip(
            f"Resource pressure: {pressure}\nCeilings: {ram_budget:.0f} GB RAM, "
            f"{vram_budget:.0f} GB VRAM")
        for bar, pct in ((self.ram_bar, ram_pct), (self.vram_bar, vram_pct)):
            chunk = self.theme.colour("danger") if pct >= 90 else (
                self.theme.colour("warning") if pct >= 75 else
                self.theme.colour("accent"))
            bar.setStyleSheet(bar.styleSheet().split("QProgressBar::chunk")[0] +
                              f"QProgressBar::chunk {{ background-color: {chunk};"
                              f" border-radius: 2px; }}")

    def set_voice(self, status: Dict[str, Any]) -> None:
        if not status:
            self.voice_label.setText("voice: off")
            return
        mode = str(status.get("mode", ""))
        stt_ok = bool((status.get("stt") or {}).get("available"))
        tts_ok = bool((status.get("tts") or {}).get("available"))
        marks = []
        marks.append("STT ✓" if stt_ok else "STT ✗")
        marks.append("TTS ✓" if tts_ok else "TTS ✗")
        self.voice_label.setText(f"voice: {mode} · " + " · ".join(marks))
        colour = self.theme.colour("success") if (stt_ok and tts_ok) else \
            self.theme.colour("warning")
        self.voice_label.setStyleSheet(f"color: {colour};")
        self.voice_label.setToolTip(
            f"Speech-to-text: {(status.get('stt') or {}).get('detail', '')}\n"
            f"Text-to-speech: {(status.get('tts') or {}).get('detail', '')}")

    def update_from(self, status: Dict[str, Any]) -> None:
        self.set_state(str(status.get("state", "idle")))
        platform = status.get("platform") or {}
        self.set_demo_mode(bool(platform.get("simulated")),
                           str((platform.get("notes") or {}).get("fallback_reason", "")))
        ollama = status.get("ollama") or {}
        self.set_models(str(ollama.get("main_model", "")),
                        bool(ollama.get("reachable")))
        tools = status.get("tools") or {}
        self.set_tools(int(tools.get("total", 0)), int(tools.get("available", 0)))
        resources = status.get("resources") or {}
        self.set_resources(float(resources.get("ram_gb", 0.0)),
                           float(resources.get("ram_budget_gb", 0.0)),
                           float(resources.get("vram_gb", 0.0)),
                           float(resources.get("vram_budget_gb", 0.0)),
                           str(resources.get("pressure", "ok")))
        self.set_voice(status.get("speech") or {})
