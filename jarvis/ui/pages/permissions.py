"""Permissions: the security layer, shown honestly.

This page can only ever *tighten* JARVIS.  The locked switches are drawn
disabled with an explanation, the prohibited list is read-only, and per-tool
overrides can move an action from "allow" to "ask" to "block" but never the
other way past what the code enforces.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...security.policy import (
    CREDENTIAL_STORE_MARKERS,
    CRITICAL_PATH_PREFIXES,
    ELEVATION_REQUIRED_PREFIXES,
    FINANCIAL_URL_MARKERS,
)
from ..theme import Theme
from .base import Page, card, hint

OVERRIDE_CHOICES = [("", "Default (code decides)"), ("allow", "Allow without asking"),
                    ("ask", "Always ask"), ("block", "Always refuse")]

#: Actions that always need approval, whatever the settings say.  Shown so the
#: user knows what to expect; they are not switches.
ALWAYS_APPROVAL = (
    "Downloading anything",
    "Sending a message, email or post",
    "Deleting a file permanently",
    "Overwriting an existing file",
    "Moving or renaming many files at once",
    "Running a shell command",
    "Opening a banking or payment site",
    "Erasing all stored memories",
)

#: Actions JARVIS refuses outright.  These cannot be re-enabled anywhere.
ALWAYS_PROHIBITED = (
    "Uninstalling or installing system software",
    "Anything that needs administrator rights or a UAC prompt",
    "Reading or writing the Windows registry",
    "Changing Windows Defender, firewall or other security software",
    "Touching system files (C:\\Windows, Program Files, boot files)",
    "Creating, changing or removing user accounts",
    "Reading password fields, saved credentials, cookies or tokens",
    "Uploading anything anywhere - JARVIS has no cloud AI and no telemetry",
)


class PermissionsPage(Page):
    """Security mode, per-tool overrides, session grants and the rules."""

    def __init__(self, theme: Theme, controller: Any,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(theme, controller, "Permissions",
                         "Security is enforced in JARVIS's code, not by asking the "
                         "model nicely. Nothing here can be loosened by a prompt.",
                         parent)

        # --- mode ------------------------------------------------------------
        mode_frame, mode_layout = card("Security mode")
        self.strict_box = QCheckBox("Strict mode: only reading and looking is allowed "
                                    "without asking")
        self.strict_box.stateChanged.connect(self._save_mode)
        mode_layout.addWidget(self.strict_box)
        self.master_box = QCheckBox("Allow JARVIS to control this computer at all "
                                    "(master switch)")
        self.master_box.stateChanged.connect(self._save_mode)
        mode_layout.addWidget(self.master_box)
        mode_layout.addWidget(hint(
            "With the master switch off, JARVIS can still answer questions and "
            "read the screen, but every tool that touches the mouse, keyboard, "
            "windows or files is refused."))
        self.body.addWidget(mode_frame)

        # --- locked switches -------------------------------------------------
        locked_frame, locked_layout = card("Always required (locked)")
        locked_layout.addWidget(hint(
            "These approval requirements are compiled into JARVIS. They are shown "
            "here so you know what will happen; they cannot be switched off from "
            "the interface, the config file, or by the AI."))
        for text in ALWAYS_APPROVAL:
            box = QCheckBox(text)
            box.setChecked(True)
            box.setEnabled(False)
            locked_layout.addWidget(box)
        self.body.addWidget(locked_frame)

        prohibited_frame, prohibited_layout = card("Always refused (locked)")
        for text in ALWAYS_PROHIBITED:
            box = QCheckBox(text)
            box.setChecked(True)
            box.setEnabled(False)
            prohibited_layout.addWidget(box)
        prohibited_layout.addWidget(hint(
            "Protected locations: " + ", ".join(list(CRITICAL_PATH_PREFIXES)[:5]) + " …\n"
            "Needs administrator rights (refused): "
            + ", ".join(list(ELEVATION_REQUIRED_PREFIXES)[:4]) + " …\n"
            "Credential stores are never read: "
            + ", ".join(list(CREDENTIAL_STORE_MARKERS)[:4]) + " …\n"
            "Financial sites that always need approval: "
            + ", ".join(list(FINANCIAL_URL_MARKERS)[:4]) + " …"))
        self.body.addWidget(prohibited_frame)

        # --- per-tool overrides ---------------------------------------------
        tools_frame, tools_layout = card("Per-tool rules")
        tools_layout.addWidget(hint(
            "You can make any tool stricter. 'Always ask' adds a confirmation to a "
            "tool that would otherwise just run; 'Always refuse' disables it "
            "completely. You cannot make a tool weaker than the code allows."))
        self.filter_edit = QComboBox()
        self.filter_edit.addItem("All categories", "")
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Tool", "Category", "Code risk", "Your override", "Available"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tools_layout.addWidget(self.table, 1)
        tools_buttons = QHBoxLayout()
        self.reset_button = QPushButton("Reset all overrides")
        self.reset_button.setObjectName("danger")
        self.reset_button.clicked.connect(self._reset_overrides)
        tools_buttons.addWidget(self.reset_button)
        tools_buttons.addStretch(1)
        tools_layout.addLayout(tools_buttons)
        self.body.addWidget(tools_frame, 1)

        # --- session grants --------------------------------------------------
        grants_frame, grants_layout = card("Allowed for this session")
        grants_layout.addWidget(hint(
            "When you approve an action 'for this session', JARVIS may repeat that "
            "exact tool and target without asking again. These grants disappear "
            "when JARVIS closes."))
        self.grants_table = QTableWidget(0, 1)
        self.grants_table.setHorizontalHeaderLabels(["Granted"])
        self.grants_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.grants_table.setMaximumHeight(140)
        grants_layout.addWidget(self.grants_table)
        revoke = QPushButton("Revoke all session grants")
        revoke.setObjectName("danger")
        revoke.clicked.connect(self._revoke_grants)
        grants_layout.addWidget(revoke)
        self.body.addWidget(grants_frame)

        self.add_footer_button("Refresh", self.refresh, "ghost")

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        security = self.settings().security
        self.strict_box.blockSignals(True)
        self.strict_box.setChecked(str(security.mode) == "strict")
        self.strict_box.blockSignals(False)
        self.master_box.blockSignals(True)
        self.master_box.setChecked(bool(security.automation_master_switch))
        self.master_box.blockSignals(False)
        self._fill_tools()
        self._fill_grants()

    def _fill_tools(self) -> None:
        runtime = self.controller.runtime
        overrides = dict(self.settings().security.tool_overrides or {})
        availability = runtime.registry.availability()
        categories = runtime.registry.by_category()
        category_of: Dict[str, str] = {}
        for category, names in categories.items():
            for name in names:
                category_of[name] = category
        self.table.setRowCount(0)
        for tool in runtime.registry.all():
            spec = tool.spec
            if spec.hidden:
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            risk = spec.risk.value if hasattr(spec.risk, "value") else str(spec.risk)
            ok, why = availability.get(spec.name, (True, ""))
            name_item = QTableWidgetItem(spec.name)
            name_item.setToolTip(spec.description[:400])
            cat_item = QTableWidgetItem(category_of.get(spec.name, spec.category))
            risk_item = QTableWidgetItem(risk)
            risk_item.setForeground(QColor(self.theme.colour(
                {"allowed": "success", "approval": "warning",
                 "prohibited": "danger"}.get(risk, "text_dim"))))
            combo = QComboBox()
            for value, label in OVERRIDE_CHOICES:
                combo.addItem(label, value)
            index = combo.findData(overrides.get(spec.name, ""))
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.currentIndexChanged.connect(
                lambda _i, name=spec.name, widget=combo: self._save_override(name, widget))
            self.table.setCellWidget(row, 3, combo)
            state_item = QTableWidgetItem("yes" if ok else "no")
            state_item.setForeground(QColor(self.theme.colour(
                "success" if ok else "warning")))
            if not ok:
                state_item.setToolTip(str(why))
            for item in (name_item, cat_item, risk_item, state_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, cat_item)
            self.table.setItem(row, 2, risk_item)
            self.table.setItem(row, 4, state_item)

    def _fill_grants(self) -> None:
        snapshot = self.controller.runtime.grants.snapshot()
        granted = snapshot.get("granted") if isinstance(snapshot, dict) else snapshot
        self.grants_table.setRowCount(0)
        for key in list(granted or []):
            row = self.grants_table.rowCount()
            self.grants_table.insertRow(row)
            item = QTableWidgetItem(str(key))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.grants_table.setItem(row, 0, item)
        if not granted:
            self.grants_table.setRowCount(1)
            item = QTableWidgetItem("(nothing granted this session)")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.grants_table.setItem(0, 0, item)

    # ------------------------------------------------------------------
    def _save_mode(self, *_args: Any) -> None:
        result = self.controller.save_settings({"security": {
            "mode": "strict" if self.strict_box.isChecked() else "balanced",
            "automation_master_switch": bool(self.master_box.isChecked()),
        }})
        if result.get("ok"):
            self.controller.runtime.registry.invalidate_availability()
            self.set_status("Security mode saved.", "ok")
            self.refresh()
        else:
            self.set_status(f"Could not save: {result.get('error')}", "danger")

    def _save_override(self, tool: str, combo: QComboBox) -> None:
        overrides = dict(self.settings().security.tool_overrides or {})
        value = str(combo.currentData() or "")
        if value:
            overrides[tool] = value
        else:
            overrides.pop(tool, None)
        result = self.controller.save_settings({"security": {"tool_overrides": overrides}})
        if result.get("ok"):
            self.controller.runtime.registry.invalidate_availability()
            self.set_status(f"'{tool}' override saved"
                            + (f": {value}" if value else " (reset to default)"), "ok")
        else:
            self.set_status(f"Could not save: {result.get('error')}", "danger")

    def _reset_overrides(self) -> None:
        answer = QMessageBox.question(
            self, "Reset overrides",
            "Remove every per-tool override and go back to JARVIS's built-in rules?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.controller.save_settings({"security": {"tool_overrides": {}}})
        self.set_status("All overrides removed.", "ok")
        self.refresh()

    def _revoke_grants(self) -> None:
        self.controller.runtime.grants.clear()
        self.set_status("Session grants revoked. JARVIS will ask again.", "ok")
        self._fill_grants()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.refresh()
