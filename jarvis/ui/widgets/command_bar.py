"""The command input row: text field, microphone, send and stop."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import Theme


class CommandInput(QLineEdit):
    """Enter submits; Escape cancels the task in flight."""

    submitted = Signal(str)
    cancelled = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            text = self.text().strip()
            if text:
                self.submitted.emit(text)
                self.clear()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            return
        super().keyPressEvent(event)


class CommandBar(QWidget):
    """Input row plus the hint line under it."""

    submitted = Signal(str)
    mic_requested = Signal()
    mic_released = Signal()
    stop_requested = Signal()

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.theme = theme
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 6, 0, 6)
        outer.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(8)

        self.input = CommandInput()
        self.input.setObjectName("commandInput")
        self.input.setPlaceholderText("Tell JARVIS what to do…  (Enter to send, "
                                      "Esc to stop)")
        self.input.setClearButtonEnabled(True)
        self.input.submitted.connect(self.submitted.emit)
        self.input.cancelled.connect(self.stop_requested.emit)
        row.addWidget(self.input, 1)

        self.mic_button = QPushButton("🎙")
        self.mic_button.setObjectName("micButton")
        self.mic_button.setCheckable(True)
        self.mic_button.setFixedSize(40, 40)
        self.mic_button.setToolTip("Push to talk (hold Ctrl+Space anywhere)")
        self.mic_button.clicked.connect(self._mic_clicked)
        row.addWidget(self.mic_button)

        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("primary")
        self.send_button.setFixedHeight(40)
        self.send_button.clicked.connect(self._send_clicked)
        row.addWidget(self.send_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("danger")
        self.stop_button.setFixedHeight(40)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        row.addWidget(self.stop_button)

        outer.addLayout(row)

        self.hint = QLabel("Voice and automation are ready.")
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        outer.addWidget(self.hint)

    # ------------------------------------------------------------------
    def _send_clicked(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.submitted.emit(text)

    def _mic_clicked(self) -> None:
        if self.mic_button.isChecked():
            self.mic_requested.emit()
        else:
            self.mic_released.emit()

    # ------------------------------------------------------------------
    def set_busy(self, busy: bool) -> None:
        self.stop_button.setEnabled(bool(busy))
        self.send_button.setEnabled(not busy)
        self.input.setEnabled(not busy)

    def set_mic_listening(self, listening: bool) -> None:
        self.mic_button.setChecked(bool(listening))
        self.mic_button.setToolTip("Listening… click to stop" if listening
                                   else "Push to talk (hold Ctrl+Space anywhere)")

    def set_hint(self, text: str) -> None:
        self.hint.setText(text or "")

    def set_mic_available(self, available: bool, reason: str = "") -> None:
        self.mic_button.setEnabled(available)
        self.mic_button.setToolTip(reason if not available else
                                   "Push to talk (hold Ctrl+Space anywhere)")

    def focus_input(self) -> None:
        self.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def text(self) -> str:
        return self.input.text()
