"""History: past conversations, tasks and the security audit log."""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..theme import Theme
from .base import Page, card, hint


def _item(text: Any, colour: str = "", selectable: bool = True) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text if text is not None else ""))
    if not selectable:
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if colour:
        from PySide6.QtGui import QColor

        item.setForeground(QColor(colour))
    return item


class HistoryPage(Page):
    """Everything JARVIS has done, stored locally and deletable by the user."""

    def __init__(self, theme: Theme, controller: Any,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(theme, controller, "History",
                         "Conversations, tasks and the security audit log. Stored "
                         "only in JARVIS's own folder on this PC.", parent)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_sessions_tab(), "Conversations")
        self.tabs.addTab(self._build_tasks_tab(), "Tasks")
        self.tabs.addTab(self._build_audit_tab(), "Security audit")
        self.body.addWidget(self.tabs, 1)

        self.add_footer_button("Refresh", self.refresh, "ghost")
        self.add_footer_button("Export diagnostics", self._export, "ghost")
        self.add_footer_button("Clear conversation history", self._clear_history, "danger")

    # ------------------------------------------------------------------
    def _build_sessions_tab(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.sessions_table = QTableWidget(0, 4)
        self.sessions_table.setHorizontalHeaderLabels(
            ["Started", "Session", "Messages", "Last message"])
        self.sessions_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        self.sessions_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.sessions_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.sessions_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sessions_table.itemSelectionChanged.connect(self._show_session)
        splitter.addWidget(self.sessions_table)

        self.session_view = QTextEdit()
        self.session_view.setObjectName("conversation")
        self.session_view.setReadOnly(True)
        splitter.addWidget(self.session_view)
        splitter.setSizes([240, 320])
        layout.addWidget(splitter)
        return holder

    def _build_tasks_tab(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.tasks_table = QTableWidget(0, 6)
        self.tasks_table.setHorizontalHeaderLabels(
            ["When", "Request", "Status", "Steps", "Took", "Summary"])
        self.tasks_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.tasks_table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch)
        self.tasks_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.tasks_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tasks_table.itemSelectionChanged.connect(self._show_task)
        splitter.addWidget(self.tasks_table)

        self.task_view = QTextEdit()
        self.task_view.setObjectName("conversation")
        self.task_view.setReadOnly(True)
        splitter.addWidget(self.task_view)
        splitter.setSizes([240, 320])
        layout.addWidget(splitter)
        return holder

    def _build_audit_tab(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(hint(
            "Every security decision is recorded here: what was asked for, which "
            "rule applied, and what you decided. This is how you check that "
            "JARVIS never did something you did not approve."))
        self.audit_table = QTableWidget(0, 6)
        self.audit_table.setHorizontalHeaderLabels(
            ["When", "Tool", "Risk", "Rule", "Outcome", "Target"])
        self.audit_table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch)
        self.audit_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.audit_table, 1)
        return holder

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        threading.Thread(target=self._load, name="jarvis-history", daemon=True).start()

    def _load(self) -> None:
        """Read the databases off the GUI thread, then hand rows to the tables."""
        runtime = self.controller.runtime
        try:
            sessions = runtime.memory.sessions(limit=80)
        except Exception:  # noqa: BLE001
            sessions = []
        try:
            tasks = runtime.memory.list_tasks(limit=120)
        except Exception:  # noqa: BLE001
            tasks = []
        try:
            audit = runtime.audit.recent(limit=300)
        except Exception:  # noqa: BLE001
            audit = []
        self._sessions = sessions
        self._tasks = tasks
        self._fill_sessions(sessions)
        self._fill_tasks(tasks)
        self._fill_audit(audit)

    def _fill_sessions(self, sessions: List[Dict[str, Any]]) -> None:
        import time as _time

        table = self.sessions_table
        table.setRowCount(0)
        for session in sessions:
            row = table.rowCount()
            table.insertRow(row)
            ts = float(session.get("ended") or session.get("started") or 0)
            when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(ts)) if ts else "?"
            table.setItem(row, 0, _item(when))
            table.setItem(row, 1, _item(session.get("session_id", "")))
            table.setItem(row, 2, _item(session.get("messages", 0)))
            table.setItem(row, 3, _item(str(session.get("first_user_message", ""))[:160]))

    def _fill_tasks(self, tasks: List[Any]) -> None:
        import json as _json
        import time as _time

        table = self.tasks_table
        table.setRowCount(0)
        for task in tasks:
            row = table.rowCount()
            table.insertRow(row)
            when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(task.ts))
            status = str(getattr(task, "status", ""))
            colour = self.theme.colour("success" if status == "completed" else
                                       ("danger" if status == "failed" else "text_dim"))
            try:
                steps = len(_json.loads(getattr(task, "steps", "[]") or "[]"))
            except Exception:  # noqa: BLE001
                steps = 0
            table.setItem(row, 0, _item(when))
            table.setItem(row, 1, _item(getattr(task, "request", "")))
            table.setItem(row, 2, _item(status, colour))
            table.setItem(row, 3, _item(steps))
            table.setItem(row, 4, _item(f"{float(getattr(task, 'duration_ms', 0) or 0):.0f} ms"))
            table.setItem(row, 5, _item(getattr(task, "summary", "")))

    def _fill_audit(self, entries: List[Any]) -> None:
        import time as _time

        table = self.audit_table
        table.setRowCount(0)
        for entry in entries:
            row = table.rowCount()
            table.insertRow(row)
            data = entry.to_row() if hasattr(entry, "to_row") else dict(entry)
            ts = float(data.get("ts", 0) or 0)
            when = _time.strftime("%m-%d %H:%M:%S", _time.localtime(ts)) if ts else "?"
            risk = str(data.get("risk", ""))
            decision = str(data.get("decision", ""))
            colour = self.theme.colour(
                "danger" if decision in ("blocked", "denied") or
                risk == "prohibited" else
                ("warning" if risk == "approval" else "text_dim"))
            table.setItem(row, 0, _item(when))
            table.setItem(row, 1, _item(data.get("tool", "")))
            table.setItem(row, 2, _item(risk, colour))
            table.setItem(row, 3, _item(data.get("rule", "")))
            table.setItem(row, 4, _item(decision, colour))
            table.setItem(row, 5, _item(str(data.get("target", ""))[:200]))

    # ------------------------------------------------------------------
    def _show_session(self) -> None:
        rows = self.sessions_table.selectionModel().selectedRows()
        if not rows:
            return
        session_id = self.sessions_table.item(rows[0].row(), 1).text()
        messages = self.controller.runtime.memory.history(session_id=session_id,
                                                          limit=400)
        html = []
        for message in messages:
            role = str(message.get("role", ""))
            text = str(message.get("content", ""))
            html.append(f"<b>{role}</b>: {text}")
        self.session_view.setHtml("<br/>".join(html) or "(empty)")

    def _show_task(self) -> None:
        rows = self.tasks_table.selectionModel().selectedRows()
        if not rows:
            return
        index = rows[0].row()
        if index >= len(getattr(self, "_tasks", [])):
            return
        task = self._tasks[index]
        import json as _json

        try:
            steps = _json.loads(getattr(task, "steps", "[]") or "[]")
        except Exception:  # noqa: BLE001
            steps = []
        lines = [f"Request: {getattr(task, 'request', '')}",
                 f"Status: {getattr(task, 'status', '')}",
                 f"Model: {getattr(task, 'model', '')}",
                 f"Summary: {getattr(task, 'summary', '')}", "", "Steps:"]
        for step in steps:
            mark = "✓" if step.get("ok") else ("✗" if step.get("ok") is False else "○")
            lines.append(f"  {mark} {step.get('tool', '')} "
                         f"{str(step.get('message', ''))[:120]}")
            if step.get("verify_detail"):
                lines.append(f"      verified: {step['verify_detail'][:120]}")
        self.task_view.setPlainText("\n".join(lines))

    # ------------------------------------------------------------------
    def _clear_history(self) -> None:
        answer = QMessageBox.question(
            self, "Clear conversation history",
            "Delete every stored conversation message? Tasks, memories and the "
            "security audit log are kept.\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self.controller.runtime.memory.clear_history()
        self.set_status(f"Deleted {removed} stored message(s).", "ok")
        self.refresh()

    def _export(self) -> None:
        from ...logging_setup import dump_diagnostics

        path = dump_diagnostics()
        self.set_status(f"Diagnostics written to {path}", "ok")
        QMessageBox.information(self, "Diagnostics",
                                f"A diagnostics file (no personal content) was "
                                f"written to:\n{path}")
