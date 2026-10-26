"""Shared scaffolding for the eight settings/status pages.

Two base classes:

* :class:`Page` - a titled scroll area with a header and a body layout.  Pages
  that are mostly live data (Dashboard, History, Memory, Models, Permissions,
  Automation) build their own widgets on top of it.
* :class:`SettingsPage` - declares its fields as data and gets load/save,
  validation and a "Saved" confirmation for free.  Voice and General are pure
  SettingsPages; the others mix both.

Everything writes through ``controller.save_settings`` so the settings are
re-sanitised on the way to disk: the UI cannot weaken a locked security value
even by accident, because :func:`jarvis.config.Settings.sanitize` re-asserts it.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..theme import Theme

log = logging.getLogger(__name__)


def card(title: str = "", theme: Optional[Theme] = None) -> tuple:
    """Return ``(frame, layout)`` for a titled card."""
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 14)
    layout.setSpacing(8)
    if title:
        label = QLabel(title)
        label.setObjectName("cardTitle")
        layout.addWidget(label)
    return frame, layout


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def row(widgets: Sequence[QWidget], stretch_first: bool = True) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for index, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if (stretch_first and index == 0) else 0)
    return holder


class Page(QFrame):
    """Titled, scrollable page."""

    def __init__(self, theme: Theme, controller: Any, title: str,
                 subtitle: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.controller = controller
        self.title = title
        self.setObjectName("page")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)

        header = QVBoxLayout()
        header.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("sectionTitle")
        header.addWidget(title_label)
        if subtitle:
            header.addWidget(hint(subtitle))
        outer.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_widget = QWidget()
        self.body = QVBoxLayout(self.body_widget)
        self.body.setContentsMargins(0, 0, 6, 0)
        self.body.setSpacing(12)
        self.scroll.setWidget(self.body_widget)
        outer.addWidget(self.scroll, 1)

        self.footer = QHBoxLayout()
        self.footer.setSpacing(8)
        self.status_label = QLabel("")
        self.status_label.setObjectName("hint")
        self.footer.addWidget(self.status_label, 1)
        outer.addLayout(self.footer)

    # ------------------------------------------------------------------
    def set_status(self, text: str, role: str = "") -> None:
        self.status_label.setText(text or "")
        colour = {"ok": self.theme.colour("success"),
                  "warn": self.theme.colour("warning"),
                  "danger": self.theme.colour("danger")}.get(role,
                                                             self.theme.colour("text_muted"))
        self.status_label.setStyleSheet(f"color: {colour};")

    def add_footer_button(self, text: str, handler: Callable[[], None],
                          object_name: str = "") -> QPushButton:
        button = QPushButton(text)
        if object_name:
            button.setObjectName(object_name)
        button.clicked.connect(handler)
        self.footer.addWidget(button)
        return button

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Called when the page becomes visible.  Override in subclasses."""
        return None

    def on_event(self, kind: str, data: Any) -> None:
        """Called for every UI update while the page is visible."""
        return None

    def settings(self) -> Any:
        return self.controller.runtime.settings_provider()


# ---------------------------------------------------------------------------
class Field:
    """One declarative settings field."""

    def __init__(self, section: str, key: str, label: str, kind: str = "str",
                 options: Optional[Sequence[Any]] = None, minimum: float = 0.0,
                 maximum: float = 100.0, step: float = 1.0, help_text: str = "",
                 locked: bool = False, suffix: str = "",
                 editor: Optional[str] = None) -> None:
        self.section = section
        self.key = key
        self.label = label
        self.kind = kind                  # str | int | float | bool | choice | list
        self.options = list(options or [])
        self.minimum = minimum
        self.maximum = maximum
        self.step = step
        self.help_text = help_text
        self.locked = locked
        self.suffix = suffix
        self.editor = editor or kind
        self.widget: Optional[QWidget] = None

    # ------------------------------------------------------------------
    def build(self, theme: Theme) -> QWidget:
        kind = self.editor
        widget: QWidget
        if kind == "bool":
            box = QCheckBox()
            box.setText("Enabled" if not self.label.lower().endswith("?") else "")
            widget = box
        elif kind == "choice":
            combo = QComboBox()
            for option in self.options:
                if isinstance(option, tuple):
                    combo.addItem(str(option[1]), option[0])
                else:
                    combo.addItem(str(option), option)
            widget = combo
        elif kind == "int":
            spin = QSpinBox()
            spin.setRange(int(self.minimum), int(self.maximum))
            spin.setSingleStep(int(self.step) or 1)
            if self.suffix:
                spin.setSuffix(self.suffix)
            widget = spin
        elif kind == "float":
            spin = QDoubleSpinBox()
            spin.setRange(float(self.minimum), float(self.maximum))
            spin.setSingleStep(float(self.step))
            spin.setDecimals(3 if float(self.step) < 0.01 else 2)
            if self.suffix:
                spin.setSuffix(self.suffix)
            widget = spin
        elif kind == "slider":
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(int(self.minimum * 100), int(self.maximum * 100))
            slider.setSingleStep(int(self.step * 100) or 1)
            widget = slider
        elif kind == "list":
            edit = QLineEdit()
            edit.setPlaceholderText("Comma separated")
            widget = edit
        else:
            widget = QLineEdit()
        if self.locked:
            widget.setEnabled(False)
            widget.setToolTip("Locked: JARVIS enforces this in code and it cannot "
                              "be weakened from the interface.")
        self.widget = widget
        return widget

    def set_value(self, value: Any) -> None:
        widget = self.widget
        if widget is None:
            return
        editor = self.editor
        if editor == "bool":
            widget.setChecked(bool(value))  # type: ignore[attr-defined]
        elif editor == "choice":
            combo: QComboBox = widget  # type: ignore[assignment]
            index = combo.findData(value)
            combo.setCurrentIndex(index if index >= 0 else 0)
        elif editor == "int":
            widget.setValue(int(value or 0))  # type: ignore[attr-defined]
        elif editor == "float":
            widget.setValue(float(value or 0.0))  # type: ignore[attr-defined]
        elif editor == "slider":
            widget.setValue(int(float(value or 0.0) * 100))  # type: ignore[attr-defined]
        elif editor == "list":
            items = value if isinstance(value, (list, tuple)) else []
            widget.setText(", ".join(str(i) for i in items))  # type: ignore[attr-defined]
        else:
            widget.setText(str(value if value is not None else ""))  # type: ignore[attr-defined]

    def value(self) -> Any:
        widget = self.widget
        if widget is None:
            return None
        editor = self.editor
        if editor == "bool":
            return bool(widget.isChecked())  # type: ignore[attr-defined]
        if editor == "choice":
            return widget.currentData()  # type: ignore[attr-defined]
        if editor == "int":
            return int(widget.value())  # type: ignore[attr-defined]
        if editor == "float":
            return float(widget.value())  # type: ignore[attr-defined]
        if editor == "slider":
            return round(float(widget.value()) / 100.0, 3)  # type: ignore[attr-defined]
        if editor == "list":
            text = str(widget.text())  # type: ignore[attr-defined]
            return [part.strip() for part in text.split(",") if part.strip()]
        return str(widget.text()).strip()  # type: ignore[attr-defined]


class SettingsPage(Page):
    """A page built from a list of :class:`Field` objects."""

    def __init__(self, theme: Theme, controller: Any, title: str, subtitle: str,
                 groups: Sequence[tuple], parent: Optional[QWidget] = None) -> None:
        """``groups`` is a sequence of ``(group_title, help_text, [Field, ...])``."""
        super().__init__(theme, controller, title, subtitle, parent)
        self.groups: List[tuple] = list(groups)
        self.fields: List[Field] = []
        for group_title, group_help, fields in self.groups:
            frame, layout = card(group_title, theme)
            if group_help:
                layout.addWidget(hint(group_help))
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
            form.setSpacing(8)
            for field in fields:
                widget = field.build(theme)
                self.fields.append(field)
                label_text = field.label + ("  🔒" if field.locked else "")
                form.addRow(label_text, widget)
                if field.help_text:
                    form.addRow("", hint(field.help_text))
            layout.addLayout(form)
            self.body.addWidget(frame)
        self.body.addStretch(1)
        self.add_footer_button("Save changes", self.save, "primary")
        self.add_footer_button("Reload", self.refresh, "ghost")

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        settings = self.settings()
        for field in self.fields:
            section = getattr(settings, field.section, None)
            if section is None:
                continue
            field.set_value(getattr(section, field.key, None))
        self.set_status("Loaded from disk.")

    def save(self) -> None:
        changes: Dict[str, Dict[str, Any]] = {}
        for field in self.fields:
            if field.locked:
                continue          # never write a locked value back
            changes.setdefault(field.section, {})[field.key] = field.value()
        result = self.controller.save_settings(changes)
        if result.get("ok"):
            self.set_status("Saved. Changes apply immediately.", "ok")
        else:
            self.set_status(f"Could not save: {result.get('error')}", "danger")
        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.refresh()
