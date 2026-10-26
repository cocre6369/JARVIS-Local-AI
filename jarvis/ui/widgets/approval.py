"""Approval and question dialogs.

The security requirement is explicit: every confirmation must say *what* will
happen, *what* it applies to, *what the consequences are* and *whether it can be
undone* - and there is no "always allow everything" button.  The three choices
are Approve once, Approve for this session (same tool, same target only) and
Deny.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..render import approval_text, question_text
from ..theme import Theme


class _BaseDialog(QDialog):
    def __init__(self, theme: Theme, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setStyleSheet(
            f"QDialog {{ background-color: {theme.colour('bg_alt')};"
            f" border: 1px solid {theme.colour('border_bright')};"
            f" border-radius: {int(theme.metrics['radius'])}px; }}")


class ApprovalDialog(_BaseDialog):
    """Modal confirmation for one specific action."""

    def __init__(self, theme: Theme, request: Dict[str, Any],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(theme, "JARVIS needs your approval", parent)
        self.request = request
        self.outcome = "denied"
        self.note = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        header = QLabel("Approval required")
        header.setObjectName("sectionTitle")
        layout.addWidget(header)

        tool = str(request.get("tool", ""))
        rule = str((request.get("decision") or {}).get("rule", ""))
        subtitle = QLabel(f"{tool}" + (f"  ·  rule {rule}" if rule else ""))
        subtitle.setObjectName("hint")
        layout.addWidget(subtitle)

        body = QFrame()
        body.setObjectName("card")
        body_layout = QVBoxLayout(body)
        details = QLabel(approval_text(request))
        details.setWordWrap(True)
        details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body_layout.addWidget(details)
        layout.addWidget(body)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Optional note back to JARVIS (e.g. 'not "
                                          "that folder')")
        layout.addWidget(self.note_edit)

        warning = QLabel("Approval applies to this action only. JARVIS will ask "
                         "again for anything else.")
        warning.setObjectName("hint")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        buttons.addWidget(spacer)

        self.deny_button = QPushButton("Deny")
        self.deny_button.setObjectName("danger")
        self.deny_button.clicked.connect(self._deny)
        buttons.addWidget(self.deny_button)

        self.session_button = QPushButton("Allow for this session")
        self.session_button.setToolTip("Allows this exact tool and target again "
                                       "without asking, until JARVIS restarts.")
        self.session_button.clicked.connect(self._allow_session)
        buttons.addWidget(self.session_button)

        self.approve_button = QPushButton("Approve once")
        self.approve_button.setObjectName("primary")
        self.approve_button.setDefault(True)
        self.approve_button.clicked.connect(self._allow_once)
        buttons.addWidget(self.approve_button)

        layout.addLayout(buttons)

    # ------------------------------------------------------------------
    def _deny(self) -> None:
        self.outcome = "denied"
        self.note = self.note_edit.text().strip()
        self.accept()

    def _allow_once(self) -> None:
        self.outcome = "approved"
        self.note = self.note_edit.text().strip()
        self.accept()

    def _allow_session(self) -> None:
        self.outcome = "approved_for_session"
        self.note = self.note_edit.text().strip()
        self.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Escape must deny, never approve.
        if event.key() == Qt.Key.Key_Escape:
            self._deny()
            return
        super().keyPressEvent(event)


class QuestionDialog(_BaseDialog):
    """Free-form question from the agent (the ``ask_user`` tool)."""

    def __init__(self, theme: Theme, request: Dict[str, Any],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(theme, "JARVIS has a question", parent)
        self.request = request
        self.answer = ""
        self.option_buttons: List[QPushButton] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        header = QLabel("Question")
        header.setObjectName("sectionTitle")
        layout.addWidget(header)

        body = QLabel(question_text(request))
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(body)

        self.answer_edit = QLineEdit()
        self.answer_edit.setPlaceholderText("Type your answer…")
        self.answer_edit.returnPressed.connect(self._submit)
        layout.addWidget(self.answer_edit)

        options = request.get("options") or []
        if options:
            row = QHBoxLayout()
            row.setSpacing(8)
            for option in list(options)[:5]:
                button = QPushButton(str(option))
                button.clicked.connect(lambda _checked=False, text=str(option):
                                       self._pick(text))
                row.addWidget(button)
                self.option_buttons.append(button)
            layout.addLayout(row)

        buttons = QHBoxLayout()
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        buttons.addWidget(spacer)
        skip = QPushButton("Skip")
        skip.setObjectName("ghost")
        skip.clicked.connect(self.reject)
        buttons.addWidget(skip)
        send = QPushButton("Send answer")
        send.setObjectName("primary")
        send.setDefault(True)
        send.clicked.connect(self._submit)
        buttons.addWidget(send)
        layout.addLayout(buttons)

    def _pick(self, text: str) -> None:
        self.answer_edit.setText(text)
        self._submit()

    def _submit(self) -> None:
        self.answer = self.answer_edit.text().strip()
        self.accept()
