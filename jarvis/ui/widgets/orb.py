"""The floating desktop orb: JARVIS's face when the main window is away.

A small always-on-top reactor disc pinned to the bottom-right of the screen
that mirrors the agent state in colour, plus a compact chat box that folds
out underneath it.  It stays visible (and JARVIS stays fully working - tasks,
wake word, approvals queue) when the main window is minimised or closed to
the tray.

States and colours:
    idle                 steady cyan
    listening            green, pulsing with the microphone
    thinking             violet, slow sweep
    executing/observing  amber/orange, quick pulse
    speaking             aqua, ripple
    waiting_for_approval gold, attention blink
    error                red
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QConicalGradient, QBrush, QFont, QIcon,
                           QPainter, QPen, QRadialGradient)
from PySide6.QtWidgets import (QHBoxLayout, QLineEdit, QMenu, QPushButton,
                               QTextBrowser, QVBoxLayout, QWidget)

log = logging.getLogger(__name__)

ORB_PX = 64
PANEL_W = 320
PANEL_H = 240

#: state -> (core colour, glow colour, pulse period seconds; 0 = steady)
STATE_COLOURS: Dict[str, tuple] = {
    "idle":                 ("#38b6ff", "#0e7bb8", 0.0),
    "listening":            ("#2ecc71", "#148f4a", 1.1),
    "thinking":             ("#9b6bff", "#5d35cc", 2.4),
    "executing":            ("#ff9f43", "#cc6a10", 0.7),
    "observing":            ("#ff9f43", "#cc6a10", 0.9),
    "speaking":             ("#00e5ff", "#0090a8", 0.8),
    "waiting_for_approval": ("#ffd166", "#cc9a20", 0.5),
    "error":                ("#ff4d4d", "#b32020", 0.4),
}
_FALLBACK = ("#38b6ff", "#0e7bb8", 0.0)


class _OrbDisc(QPushButton):
    """The painted reactor disc. Click = toggle the chat panel."""

    def __init__(self, parent: "DesktopOrb") -> None:
        super().__init__(parent)
        self.setFixedSize(ORB_PX, ORB_PX)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("JARVIS - click to chat, drag to move, right-click for menu")
        self._state = "idle"
        self._phase = 0.0
        self._level = 0.0
        self._anim = QTimer(self)
        self._anim.setInterval(40)
        self._anim.timeout.connect(self._tick)
        self._anim.start()

    # ------------------------------------------------------------------
    def set_state(self, state: str) -> None:
        self._state = str(state or "idle")

    def set_level(self, level: float) -> None:
        self._level = max(0.0, min(1.0, float(level or 0.0)))

    def _tick(self) -> None:
        self._phase += 0.04
        self.update()

    # ------------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        core_hex, glow_hex, period = STATE_COLOURS.get(self._state, _FALLBACK)
        core = QColor(core_hex)
        glow = QColor(glow_hex)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)

        pulse = 0.5
        if period > 0:
            pulse = 0.5 + 0.5 * math.sin(2 * math.pi * (time.time() % period) / period)
        if self._state == "listening" and self._level > 0:
            pulse = max(pulse, self._level)

        # outer glow
        glow_color = QColor(core)
        glow_color.setAlphaF(0.25 + 0.35 * pulse)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow_color))
        p.drawEllipse(rect.adjusted(-2, -2, 2, 2))

        # rotating energy ring
        ring = QRectF(rect).adjusted(4, 4, -4, -4)
        gradient = QConicalGradient(ring.center(), (self._phase * 90) % 360)
        gradient.setColorAt(0.0, core)
        gradient.setColorAt(0.5, glow)
        gradient.setColorAt(1.0, core)
        pen = QPen(QBrush(gradient), 2.4)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(ring)

        # reactor core
        inner = QRectF(rect).adjusted(10, 10, -10, -10)
        core_grad = QRadialGradient(inner.center(), inner.width() / 2)
        bright = QColor(core)
        bright.setAlphaF(0.85 + 0.15 * pulse)
        core_grad.setColorAt(0.0, QColor("#ffffff"))
        core_grad.setColorAt(0.35, bright)
        core_grad.setColorAt(1.0, glow)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(core_grad))
        p.drawEllipse(inner)

        # tiny JARVIS monogram
        p.setPen(QPen(QColor("#06121c")))
        font = QFont("Segoe UI", 9, QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(inner, Qt.AlignmentFlag.AlignCenter, "J")
        p.end()


class DesktopOrb(QWidget):
    """Frameless always-on-top orb + fold-out mini chat, bottom-right."""

    def __init__(self, controller: Any, theme: Any = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.theme = theme
        self.setObjectName("desktopOrb")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._drag_pos: Optional[QPointF] = None
        self._lines: List[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        root.setAlignment(Qt.AlignmentFlag.AlignRight)

        self.disc = _OrbDisc(self)
        self.disc.clicked.connect(self.toggle_panel)
        self.disc.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.disc.customContextMenuRequested.connect(self._menu)
        orb_row = QHBoxLayout()
        orb_row.addStretch(1)
        orb_row.addWidget(self.disc)
        root.addLayout(orb_row)

        # --- mini chat panel (hidden until the orb is clicked) ----------
        self.panel = QWidget(self)
        self.panel.setObjectName("orbPanel")
        self.panel.setFixedSize(PANEL_W, PANEL_H)
        self.panel.setStyleSheet(
            "QWidget#orbPanel { background-color: #0d1a26;"
            " border: 1px solid #1e3a52; border-radius: 10px; }"
            "QTextBrowser { background-color: #0a141e; color: #cfe8ff;"
            " border: none; font-size: 9pt; }"
            "QLineEdit { background-color: #10222f; color: #e8f6ff;"
            " border: 1px solid #234b68; border-radius: 6px; padding: 4px; }"
            "QPushButton { background-color: #14324a; color: #bfe6ff;"
            " border: 1px solid #234b68; border-radius: 6px; padding: 3px 10px; }"
            "QPushButton:hover { background-color: #1b4260; }")
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(8, 8, 8, 8)
        pl.setSpacing(6)
        self.feed = QTextBrowser()
        self.feed.setOpenExternalLinks(False)
        pl.addWidget(self.feed, 1)
        row = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Tell JARVIS…")
        self.entry.returnPressed.connect(self._send)
        row.addWidget(self.entry, 1)
        send = QPushButton("Send")
        send.clicked.connect(self._send)
        row.addWidget(send)
        pl.addLayout(row)
        self.panel.hide()
        root.addWidget(self.panel, 0, Qt.AlignmentFlag.AlignRight)

        self._reposition()
        self.set_state("idle")
        self.append_line("system", "JARVIS is here. Click the orb to chat - the "
                                   "main window can stay closed.")

    # ------------------------------------------------------------------
    def _reposition(self) -> None:
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else self.screen().availableGeometry()
        self.adjustSize()
        self.move(geo.right() - self.width() - 14, geo.bottom() - self.height() - 14)

    def set_state(self, state: str) -> None:
        self.disc.set_state(state)
        if state == "error":
            self.disc.setToolTip("JARVIS - something went wrong. Click for details.")
        else:
            self.disc.setToolTip("JARVIS - click to chat, drag to move, "
                                 "right-click for menu")

    def set_level(self, level: float) -> None:
        self.disc.set_level(level)

    def toggle_panel(self) -> None:
        self.panel.setVisible(not self.panel.isVisible())
        self.adjustSize()
        if self.panel.isVisible():
            self.entry.setFocus()

    def append_line(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        stamp = time.strftime("%H:%M")
        colour = {"user": "#7ee787", "assistant": "#8ecbff",
                  "system": "#9aa7b3"}.get(role, "#cfe8ff")
        who = {"user": "You", "assistant": "JARVIS", "system": "•"}.get(role, role)
        safe = (text.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;"))
        self._lines.append(
            f'<div><span style="color:#5d7488">[{stamp}]</span> '
            f'<span style="color:{colour}"><b>{who}:</b></span> '
            f'<span style="color:{colour}">{safe}</span></div>')
        self._lines = self._lines[-60:]
        self.feed.setHtml("".join(self._lines))
        bar = self.feed.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ------------------------------------------------------------------
    def _send(self) -> None:
        text = self.entry.text().strip()
        if not text:
            return
        self.entry.clear()
        self.append_line("user", text)
        try:
            self.controller.submit(text, source="orb")
        except Exception:  # noqa: BLE001
            log.exception("orb submit failed")

    def _menu(self, _pos) -> None:
        menu = QMenu(self)
        toggle = menu.addAction("Hide chat" if self.panel.isVisible() else "Open chat")
        toggle.triggered.connect(self.toggle_panel)
        show = menu.addAction("Show main window")
        show.triggered.connect(self._show_main)
        menu.addSeparator()
        close = menu.addAction("Hide orb")
        close.triggered.connect(self.hide)
        menu.exec(self.disc.mapToGlobal(_pos))

    def _show_main(self) -> None:
        # Walk up to the MainWindow (sibling top-level) and restore it.
        from PySide6.QtWidgets import QApplication
        for top in QApplication.topLevelWidgets():
            if top is self or top.parent() is self:
                continue
            if type(top).__name__ == "MainWindow":
                top.showNormal()
                top.raise_()
                top.activateWindow()
                return

    # ------------------------------------------------------------------
    # dragging the orb (panel stays anchored bottom-right of the widget)
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_pos is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move((event.globalPosition() - self._drag_pos).toPoint())
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_pos = None
        event.accept()


__all__ = ["DesktopOrb", "STATE_COLOURS"]
