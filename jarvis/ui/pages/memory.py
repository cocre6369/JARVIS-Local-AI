"""Memory: what JARVIS remembers, with full user control over it.

The design requires memories to be viewable, editable, deletable and
*disableable*, and to never leave the machine.  Everything on this page is
backed by the local SQLite database in JARVIS's data folder.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..theme import Theme
from .base import Page, card, hint

FACT_CATEGORIES = ("preference", "app", "location", "music", "person", "routine",
                   "device", "workflow", "other")


class _FactDialog(QDialog):
    """Add or edit one memory."""

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None,
                 fact: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.setWindowTitle("Edit memory" if fact else "Add a memory")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.fact = fact or {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.category = QComboBox()
        self.category.addItems(list(FACT_CATEGORIES))
        if self.fact.get("category") in FACT_CATEGORIES:
            self.category.setCurrentText(str(self.fact["category"]))
        self.key = QLineEdit(str(self.fact.get("key", "")))
        self.key.setPlaceholderText("e.g. favourite editor")
        self.value = QLineEdit(str(self.fact.get("value", "")))
        self.value.setPlaceholderText("e.g. Visual Studio Code")
        self.pinned = QCheckBox("Keep this memory even when old ones are pruned")
        self.pinned.setChecked(bool(self.fact.get("pinned")))
        form.addRow("Category", self.category)
        form.addRow("Name", self.key)
        form.addRow("Value", self.value)
        form.addRow("", self.pinned)
        layout.addLayout(form)
        layout.addWidget(hint("Credentials are refused: JARVIS will not store "
                              "passwords, tokens or card numbers."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> Dict[str, Any]:
        return {"category": self.category.currentText(),
                "key": self.key.text().strip(),
                "value": self.value.text().strip(),
                "pinned": self.pinned.isChecked()}


class MemoryPage(Page):
    """View / edit / delete / disable every stored memory."""

    def __init__(self, theme: Theme, controller: Any,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(theme, controller, "Memory",
                         "Stored in a local database on this PC. Never uploaded, "
                         "never synced.", parent)
        self._facts: List[Any] = []
        self._shortcuts: List[Dict[str, Any]] = []

        # --- global switches -------------------------------------------------
        frame, layout = card("Remembering")
        row1 = QHBoxLayout()
        self.enabled_box = QCheckBox("Remember things between conversations")
        self.conversations_box = QCheckBox("Store conversation text")
        self.auto_box = QCheckBox("Extract facts automatically")
        row1.addWidget(self.enabled_box)
        row1.addWidget(self.conversations_box)
        row1.addWidget(self.auto_box)
        row1.addStretch(1)
        layout.addLayout(row1)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Delete memories older than"))
        self.retention = QSpinBox()
        self.retention.setRange(0, 3650)
        self.retention.setSuffix(" days")
        self.retention.setSpecialValueText("never")
        row2.addWidget(self.retention)
        row2.addStretch(1)
        layout.addLayout(row2)
        self.stats_label = hint("")
        layout.addWidget(self.stats_label)
        save_row = QHBoxLayout()
        self.save_button = QPushButton("Save memory settings")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self._save_settings)
        save_row.addWidget(self.save_button)
        save_row.addStretch(1)
        layout.addLayout(save_row)
        self.body.addWidget(frame)

        # --- facts -----------------------------------------------------------
        facts_frame, facts_layout = card("Memories")
        toolbar = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter memories…")
        self.search_box.textChanged.connect(self._fill_facts)
        self.show_disabled = QCheckBox("Show disabled")
        self.show_disabled.stateChanged.connect(lambda *_: self.refresh())
        toolbar.addWidget(self.search_box, 1)
        toolbar.addWidget(self.show_disabled)
        for text, slot in (("Add", self._add_fact), ("Edit", self._edit_fact),
                           ("Pin", self._toggle_pin), ("Disable", self._toggle_disabled),
                           ("Delete", self._delete_fact)):
            button = QPushButton(text)
            if text == "Delete":
                button.setObjectName("danger")
            button.clicked.connect(slot)
            toolbar.addWidget(button)
        facts_layout.addLayout(toolbar)

        self.facts_table = QTableWidget(0, 7)
        self.facts_table.setHorizontalHeaderLabels(
            ["Category", "Name", "Value", "Uses", "Source", "Pinned", "Active"])
        header = self.facts_table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.facts_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.facts_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.facts_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.facts_table.doubleClicked.connect(lambda *_: self._edit_fact())
        facts_layout.addWidget(self.facts_table, 1)
        self.body.addWidget(facts_frame, 1)

        # --- shortcuts -------------------------------------------------------
        sc_frame, sc_layout = card("Voice shortcuts")
        sc_layout.addWidget(hint(
            "Phrases you taught JARVIS. Say or type the phrase and the saved "
            "actions are replayed - each one is still checked against the "
            "security policy and still verified."))
        self.shortcuts_table = QTableWidget(0, 3)
        self.shortcuts_table.setHorizontalHeaderLabels(["Phrase", "Action", "Uses"])
        self.shortcuts_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.shortcuts_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        sc_layout.addWidget(self.shortcuts_table)
        sc_buttons = QHBoxLayout()
        delete_shortcut = QPushButton("Delete selected shortcut")
        delete_shortcut.setObjectName("danger")
        delete_shortcut.clicked.connect(self._delete_shortcut)
        sc_buttons.addWidget(delete_shortcut)
        sc_buttons.addStretch(1)
        sc_layout.addLayout(sc_buttons)
        self.body.addWidget(sc_frame)

        self.add_footer_button("Export…", self._export, "ghost")
        self.add_footer_button("Erase everything", self._clear_all, "danger")
        self.add_footer_button("Refresh", self.refresh, "ghost")

    # ------------------------------------------------------------------
    @property
    def memory(self) -> Any:
        return self.controller.runtime.memory

    def refresh(self) -> None:
        settings = self.settings()
        self.enabled_box.setChecked(bool(settings.memory.enabled))
        self.conversations_box.setChecked(bool(settings.memory.remember_conversations))
        self.auto_box.setChecked(bool(settings.memory.auto_extract_facts))
        self.retention.setValue(int(settings.memory.retention_days or 0))
        try:
            self._facts = self.memory.list_facts(limit=500, include_disabled=True)
        except Exception as exc:  # noqa: BLE001
            self._facts = []
            self.set_status(f"Could not read memories: {exc}", "danger")
        try:
            self._shortcuts = self.memory.shortcuts()
        except Exception:  # noqa: BLE001
            self._shortcuts = []
        self._fill_facts()
        self._fill_shortcuts()
        try:
            stats = self.memory.stats()
            self.stats_label.setText(
                f"Database: {stats.get('db_path', '')}  ·  "
                f"{stats.get('db_size_kb', 0)} KB  ·  "
                f"{stats.get('facts', 0)} memories"
                + (f" ({stats.get('facts_disabled', 0)} disabled)"
                   if stats.get("facts_disabled") else "")
                + f"  ·  {stats.get('messages', 0)} messages  ·  "
                f"{stats.get('tasks', 0)} tasks  ·  "
                f"{stats.get('shortcuts', 0)} shortcuts")
        except Exception:  # noqa: BLE001
            pass

    def _fill_facts(self, *_args: Any) -> None:
        needle = self.search_box.text().strip().lower()
        table = self.facts_table
        table.setRowCount(0)
        for fact in self._facts:
            if fact.disabled and not self.show_disabled.isChecked():
                continue
            if needle and needle not in f"{fact.key} {fact.value} {fact.category}".lower():
                continue
            row = table.rowCount()
            table.insertRow(row)
            values = (fact.category, fact.key, fact.value, fact.uses, fact.source,
                      "★" if fact.pinned else "", "on" if not fact.disabled else "OFF")
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 6 and fact.disabled:
                    from PySide6.QtGui import QColor

                    item.setForeground(QColor(self.theme.colour("warning")))
                table.setItem(row, column, item)
            # The fact id rides along on the first cell so the buttons can find it.
            table.item(row, 0).setData(Qt.ItemDataRole.UserRole, fact.id)

    def _fill_shortcuts(self) -> None:
        table = self.shortcuts_table
        table.setRowCount(0)
        for shortcut in self._shortcuts:
            row = table.rowCount()
            table.insertRow(row)
            action = shortcut.get("action")
            rendered = json.dumps(action, ensure_ascii=False) if not isinstance(
                action, str) else action
            for column, value in enumerate((shortcut.get("phrase", ""),
                                            rendered[:300], shortcut.get("uses", 0))):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row, column, item)
            table.item(row, 0).setData(Qt.ItemDataRole.UserRole, shortcut.get("id"))

    def _selected_fact(self) -> Optional[Any]:
        rows = self.facts_table.selectionModel().selectedRows()
        if not rows:
            self.set_status("Select a memory first.", "warn")
            return None
        fact_id = self.facts_table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        for fact in self._facts:
            if fact.id == fact_id:
                return fact
        return None

    # ------------------------------------------------------------------
    def _save_settings(self) -> None:
        result = self.controller.save_settings({"memory": {
            "enabled": self.enabled_box.isChecked(),
            "remember_conversations": self.conversations_box.isChecked(),
            "auto_extract_facts": self.auto_box.isChecked(),
            "retention_days": int(self.retention.value()),
        }})
        self.set_status("Memory settings saved." if result.get("ok")
                        else f"Could not save: {result.get('error')}",
                        "ok" if result.get("ok") else "danger")
        self.refresh()

    def _add_fact(self) -> None:
        dialog = _FactDialog(self.theme, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if not values["key"] or not values["value"]:
            self.set_status("A memory needs both a name and a value.", "warn")
            return
        fact = self.memory.add_fact(values["category"], values["key"], values["value"],
                                    source="user", pinned=values["pinned"])
        if fact is None:
            self.set_status("JARVIS refused to store that - it looks like a "
                            "credential.", "danger")
            return
        self.set_status(f"Stored '{fact.key}'.", "ok")
        self.refresh()

    def _edit_fact(self) -> None:
        fact = self._selected_fact()
        if fact is None:
            return
        dialog = _FactDialog(self.theme, self, fact=fact.as_dict())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        ok = self.memory.update_fact(fact.id, value=values["value"],
                                     category=values["category"], key=values["key"],
                                     pinned=values["pinned"])
        self.set_status("Memory updated." if ok else
                        "Update refused (the value looks like a credential).",
                        "ok" if ok else "danger")
        self.refresh()

    def _toggle_pin(self) -> None:
        fact = self._selected_fact()
        if fact is None:
            return
        self.memory.update_fact(fact.id, pinned=not fact.pinned)
        self.set_status("Pinned." if not fact.pinned else "Unpinned.", "ok")
        self.refresh()

    def _toggle_disabled(self) -> None:
        fact = self._selected_fact()
        if fact is None:
            return
        self.memory.set_fact_enabled(fact.id, bool(fact.disabled))
        self.set_status("Memory disabled - JARVIS will not use it." if not fact.disabled
                        else "Memory re-enabled.", "ok")
        self.refresh()

    def _delete_fact(self) -> None:
        fact = self._selected_fact()
        if fact is None:
            return
        answer = QMessageBox.question(
            self, "Delete memory",
            f"Permanently delete this memory?\n\n{fact.key}: {fact.value}\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.memory.delete_fact(fact.id)
        self.set_status("Memory deleted.", "ok")
        self.refresh()

    def _delete_shortcut(self) -> None:
        rows = self.shortcuts_table.selectionModel().selectedRows()
        if not rows:
            self.set_status("Select a shortcut first.", "warn")
            return
        shortcut_id = self.shortcuts_table.item(rows[0].row(), 0).data(
            Qt.ItemDataRole.UserRole)
        if self.memory.delete_shortcut(int(shortcut_id)):
            self.set_status("Shortcut deleted.", "ok")
            self.refresh()

    # ------------------------------------------------------------------
    def _export(self) -> None:
        try:
            path = self.memory.export_json()
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"Export failed: {exc}", "danger")
            return
        self.set_status(f"Exported to {path}", "ok")
        QMessageBox.information(self, "Export",
                                f"Your memories were exported to:\n{path}\n\n"
                                "That file stays on this computer unless you move it.")

    def _clear_all(self) -> None:
        answer = QMessageBox.question(
            self, "Erase all memory",
            "Delete every conversation, memory, task record and shortcut?\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        keep = QMessageBox.question(
            self, "Erase all memory",
            "Keep the facts JARVIS learned about you (preferences, apps, people)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        report = self.memory.clear_all(keep_facts=keep)
        self.set_status("Erased: " + ", ".join(f"{k}={v}" for k, v in report.items()), "ok")
        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.refresh()
