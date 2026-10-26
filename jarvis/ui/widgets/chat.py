"""Conversation panel and tool trace."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QHeaderView,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..render import (
    inline_format,
    render_line,
    render_notice,
    render_tool_line,
    timestamp_label,
)
from ..theme import Theme

#: Only the newest fragments are kept in the HTML document; older ones stay in
#: the History page.  Re-rendering thousands of bubbles would make streaming
#: visibly laggy.
MAX_FRAGMENTS = 140


class ConversationView(QTextEdit):
    """Read-only conversation feed with streaming support."""

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.setObjectName("conversation")
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setOpenExternalLinks(False)
        self._fragments: List[str] = []
        self._streaming = False
        self._html_head = (
            "<html><head><style>"
            f"body {{ background-color: {theme.colour('bg_alt')};"
            f" color: {theme.colour('text')};"
            f" font-family: '{theme.font_family}';"
            f" font-size: {theme.font_size + 1}pt; }}"
            "div { line-height: 1.35; }"
            "</style></head><body>"
        )
        self.setHtml(self._html_head + "</body></html>")

    # ------------------------------------------------------------------
    def _repaint(self, autoscroll: bool = True) -> None:
        tail = self._fragments[-MAX_FRAGMENTS:]
        self.setHtml(self._html_head + "".join(tail) + "</body></html>")
        if autoscroll:
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.setTextCursor(cursor)
            self.ensureCursorVisible()

    def append_line(self, role: str, text: str, ts: Optional[float] = None,
                    streaming: bool = False) -> None:
        if streaming:
            self.begin_stream(role, text, ts)
            return
        self._streaming = False
        self._fragments.append(render_line(role, text, self.theme.palette, ts=ts))
        self._repaint()

    def begin_stream(self, role: str, text: str, ts: Optional[float] = None) -> None:
        self._streaming = True
        self._stream_fragment = render_line(role, text, self.theme.palette, ts=ts,
                                            streaming=True)
        self._repaint()

    def update_stream(self, text: str) -> None:
        if not self._streaming:
            self.begin_stream("assistant", text)
            return
        self._stream_fragment = render_line("assistant", text, self.theme.palette,
                                            streaming=True)
        self._fragments = [f for f in self._fragments if "▌" not in f]
        self._fragments.append(self._stream_fragment)
        self._repaint()

    def end_stream(self, text: str) -> None:
        self._streaming = False
        self._fragments = [f for f in self._fragments if "▌" not in f]
        if text.strip():
            self._fragments.append(render_line("assistant", text, self.theme.palette))
        self._repaint()

    def append_tool(self, tool: str, ok: Optional[bool], message: str,
                    verified: Optional[bool] = None, detail: str = "",
                    duration_ms: float = 0.0) -> None:
        self._fragments.append(render_tool_line(tool, ok, message, verified, detail,
                                                self.theme.palette, duration_ms))
        self._repaint()

    def append_notice(self, title: str, message: str, kind: str = "info") -> None:
        self._fragments.append(render_notice(title, message, self.theme.palette, kind))
        self._repaint()

    def append_thought(self, text: str) -> None:
        if not text.strip():
            return
        self._fragments.append(render_line("thought", text, self.theme.palette))
        self._repaint()

    def clear_all(self) -> None:
        self._fragments.clear()
        self._streaming = False
        self._repaint(autoscroll=False)

    def fragment_count(self) -> int:
        return len(self._fragments)


class ToolTracePanel(QWidget):
    """Step-by-step record of what JARVIS actually did."""

    COLUMNS = ("#", "Tool", "Result", "Verification", "ms")

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.theme = theme
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.COLUMNS))
        self.tree.setHeaderLabels(list(self.COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree)

    def clear(self) -> None:
        self.tree.clear()

    def add_call(self, index: int, tool: str, args: Dict[str, Any]) -> QTreeWidgetItem:
        rendered = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:4])
        item = QTreeWidgetItem([str(index), tool, rendered or "-", "running…", ""])
        self.tree.addTopLevelItem(item)
        self.tree.scrollToBottom()
        return item

    def add_result(self, index: int, tool: str, ok: bool, message: str,
                   verified: Optional[bool], detail: str = "",
                   duration_ms: float = 0.0) -> None:
        state = "ok" if ok else "FAILED"
        if ok and verified is False:
            state = "ok (unverified)"
        verification = detail or ("verified" if verified else
                                  ("not verified" if verified is False else ""))
        item = QTreeWidgetItem([str(index), tool, f"{state}: {message[:120]}",
                                verification[:160], f"{duration_ms:.0f}"])
        colour = self.theme.colour("success" if ok else "danger")
        for column in range(len(self.COLUMNS)):
            item.setForeground(column, self.theme.colour("text_dim"))
        item.setForeground(2, colour)
        self.tree.addTopLevelItem(item)
        self.tree.scrollToBottom()

    def set_status_text(self, text: str) -> None:
        self.tree.setToolTip(text)

    def row_count(self) -> int:
        return self.tree.topLevelItemCount()
