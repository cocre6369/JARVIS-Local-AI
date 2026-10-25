"""The animated circular AI core.

A thin Qt shell over :mod:`jarvis.ui.visual_core`, which does all the maths.
The widget only:

* keeps a clock,
* asks for a frame for the current agent state and audio level,
* paints it,
* repaints at a rate that depends on how busy JARVIS is (idle = cheap).

With ``ui.animation_speed`` set to 0 the timer is stopped entirely and the core
is drawn once, for machines where the animation is not worth the CPU.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..theme import Theme
from ..visual_core import build_frame, frame_rate_for


def _qcolour(hex_value: str, alpha: float = 1.0) -> QColor:
    colour = QColor(hex_value)
    colour.setAlphaF(max(0.0, min(1.0, alpha)))
    return colour


class CoreVisualiser(QWidget):
    """Reactor-style state indicator."""

    clicked = Signal()

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self._state = "idle"
        self._label = "Idle"
        self._level = 0.0
        self._tts_level = 0.0
        self._t0 = time.time()
        self._last_frame: Any = None
        self.setMinimumSize(120, 120)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("JARVIS core - click to interrupt")

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._apply_state()

    # ------------------------------------------------------------------
    def set_state(self, state: str, label: str = "") -> None:
        state = (state or "idle").lower()
        if state == self._state and label == self._label:
            return
        self._state = state
        self._label = label or self.theme.state_label(state)
        self._apply_state()
        self.update()

    def set_level(self, level: float) -> None:
        """Microphone level (0..1)."""
        self._level = max(0.0, min(1.0, float(level)))
        self.update()

    def set_tts_level(self, level: float) -> None:
        self._tts_level = max(0.0, min(1.0, float(level)))
        self.update()

    def _apply_state(self) -> None:
        interval = frame_rate_for(self._state, self.theme.animation_speed)
        if interval <= 0:
            self._timer.stop()
            self.update()
        else:
            self._timer.setInterval(interval)
            if not self._timer.isActive():
                self._timer.start()

    def set_animation_speed(self, speed: float) -> None:
        self.theme.animation_speed = max(0.0, float(speed))
        self._apply_state()

    def _tick(self) -> None:
        # Decay the audio level so the halo relaxes between updates.
        self._level *= 0.86
        self._tts_level *= 0.88
        self.update()

    # ------------------------------------------------------------------
    def sizeHint(self):  # noqa: N802 - Qt naming
        side = int(self.theme.metrics["core_size"])
        return QSize(side, side)

    def minimumSizeHint(self):  # noqa: N802 - Qt naming
        return QSize(120, 120)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            side = float(min(self.width(), self.height()))
            if side <= 8:
                return
            speed = self.theme.animation_speed
            level = max(self._level, self._tts_level * 0.8)
            frame = build_frame(
                state=self._state,
                t=time.time() - self._t0,
                size=side,
                colour=self.theme.state_colour(self._state),
                level=level,
                accent_secondary=self.theme.accent_secondary,
                label=self._label,
                animation_speed=speed,
            )
            self._last_frame = frame
            cx, cy = frame.centre
            centre = QPointF(cx, cy)

            # --- outer halo ------------------------------------------------
            if frame.halo_alpha > 0.01:
                halo = QRadialGradient(centre, frame.halo_radius)
                halo.setColorAt(0.62, _qcolour(frame.colour, 0.0))
                halo.setColorAt(0.86, _qcolour(frame.colour, frame.halo_alpha * 0.55))
                halo.setColorAt(1.0, _qcolour(frame.colour, 0.0))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(halo))
                painter.drawEllipse(centre, frame.halo_radius, frame.halo_radius)

            # --- faint full circle guides ---------------------------------
            for ring in frame.rings:
                painter.setPen(QPen(_qcolour(self.theme.colour("border"), 0.55),
                                    1.0, Qt.PenStyle.SolidLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(centre, ring.radius, ring.radius)

            # --- segmented rings ------------------------------------------
            for ring in frame.rings:
                pen = QPen(_qcolour(ring.colour, ring.alpha), ring.width,
                           Qt.PenStyle.DashLine if ring.dash else Qt.PenStyle.SolidLine,
                           Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                rect = QRectF(cx - ring.radius, cy - ring.radius,
                              ring.radius * 2, ring.radius * 2)
                for start, end in ring.arcs():
                    span = end - start
                    if span < 0:
                        span += 360.0
                    painter.drawArc(rect, int(-start * 16), int(-span * 16))

            # --- radar sweep ----------------------------------------------
            sweep = QConicalGradient(centre, -frame.sweep_start)
            sweep_colour = _qcolour(frame.colour, 0.30 * frame.brightness)
            sweep.setColorAt(0.0, _qcolour(frame.colour, 0.0))
            sweep.setColorAt(max(0.01, min(0.99, frame.sweep_span / 360.0)),
                             sweep_colour)
            sweep.setColorAt(1.0, _qcolour(frame.colour, 0.0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(sweep))
            path = QPainterPath()
            path.moveTo(centre)
            path.arcTo(QRectF(cx - frame.glow_radius * 1.9, cy - frame.glow_radius * 1.9,
                              frame.glow_radius * 3.8, frame.glow_radius * 3.8),
                       -frame.sweep_start, frame.sweep_span)
            path.closeSubpath()
            painter.drawPath(path)

            # --- sparks ----------------------------------------------------
            painter.setPen(Qt.PenStyle.NoPen)
            for spark in frame.sparks:
                sx = cx + spark.radius * math.cos(spark.angle)
                sy = cy + spark.radius * math.sin(spark.angle)
                painter.setBrush(_qcolour(frame.colour, spark.alpha * frame.brightness))
                painter.drawEllipse(QPointF(sx, sy), spark.size, spark.size)

            # --- reactor core ---------------------------------------------
            glow = QRadialGradient(centre, max(1.0, frame.glow_radius))
            glow.setColorAt(0.0, _qcolour(self.theme.colour("reactor_core"),
                                          0.85 * frame.brightness))
            glow.setColorAt(0.45, _qcolour(frame.colour, 0.42 * frame.brightness))
            glow.setColorAt(1.0, _qcolour(frame.colour, 0.0))
            painter.setBrush(QBrush(glow))
            painter.drawEllipse(centre, frame.glow_radius, frame.glow_radius)

            painter.setBrush(_qcolour(self.theme.colour("reactor_core"),
                                      0.70 + 0.30 * frame.brightness))
            painter.drawEllipse(centre, frame.core_radius, frame.core_radius)

            painter.setPen(QPen(_qcolour(frame.colour, 0.9), 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, frame.core_radius * 1.45,
                                frame.core_radius * 1.45)
        finally:
            painter.end()

    # ------------------------------------------------------------------
    def frame_snapshot(self) -> Dict[str, Any]:
        """Last computed frame (used by tests and the diagnostics dump)."""
        if self._last_frame is None:
            return {}
        return self._last_frame.as_dict()


class CorePanel(QWidget):
    """The core plus its caption, as used on the dashboard."""

    interrupted = Signal()

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.theme = theme
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.core = CoreVisualiser(theme)
        self.core.clicked.connect(self.interrupted.emit)
        self.state_label = QLabel(theme.state_label("idle"))
        self.state_label.setObjectName("cardTitle")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label = QLabel("Standing by")
        self.detail_label.setObjectName("hint")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.core, 1)
        layout.addWidget(self.state_label)
        layout.addWidget(self.detail_label)

    def set_state(self, state: str) -> None:
        self.core.set_state(state)
        self.state_label.setText(self.theme.state_label(state))

    def set_detail(self, text: str) -> None:
        self.detail_label.setText(text or "")

    def set_levels(self, mic: float = 0.0, tts: float = 0.0) -> None:
        if mic:
            self.core.set_level(mic)
        if tts:
            self.core.set_tts_level(tts)
