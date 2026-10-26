"""The main application window.

Layout: a navigation rail on the left, the eight pages in the middle, and an
always-visible conversation panel with the command input underneath - so JARVIS
can be talked to from any page.  A live status strip runs along the bottom.

Two rules keep the interface responsive:

* nothing blocking happens on this thread.  Model calls, tool execution,
  microphone capture and model downloads all run on runtime/worker threads.
* engine events are never handled directly.  They arrive as
  :class:`UiUpdate` records that a timer drains, because they are published from
  worker threads and Qt widgets may only be touched here.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..core.broker import Outcome
from ..events import AgentState
from .controller import (
    K_APPROVAL,
    K_APPROVAL_RESOLVED,
    K_LEVEL,
    K_LOG,
    K_MESSAGE,
    K_METRICS,
    K_NOTIFICATION,
    K_OLLAMA,
    K_QUESTION,
    K_SPEECH,
    K_STATE,
    K_STREAM,
    K_TASK,
    K_THOUGHT,
    K_TTS_LEVEL,
    K_TOOL,
    UiController,
)
from .pages import PAGE_ORDER, build_pages
from .theme import Theme, nav_items, stylesheet
from .widgets.approval import ApprovalDialog, QuestionDialog
from .widgets.chat import ConversationView, ToolTracePanel
from .widgets.command_bar import CommandBar
from .widgets.status_bar import StatusStrip

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """One window, one runtime."""

    def __init__(self, runtime: Any, controller: Optional[UiController] = None,
                 show_wizard: bool = False) -> None:
        super().__init__()
        self.runtime = runtime
        self.controller = controller or UiController(runtime)
        self.settings = runtime.settings_provider()
        self.theme = Theme.from_settings(self.settings)
        self._stream_text: List[str] = []
        self._streaming = False
        self._tray: Optional[QSystemTrayIcon] = None
        self.orb: Optional[Any] = None

        name = self.settings.general.assistant_name or "JARVIS"
        self.setWindowTitle(f"{name} — local AI assistant")
        self.setObjectName("root")
        self.resize(1360, 880)
        self.setMinimumSize(1024, 680)
        icon_path = paths.icon_path()
        if icon_path and os.path.exists(str(icon_path)):
            self.setWindowIcon(QIcon(str(icon_path)))
        if 0.05 < float(self.settings.ui.window_opacity) <= 1.0:
            self.setWindowOpacity(float(self.settings.ui.window_opacity))

        self._build_ui()
        self._build_shortcuts()
        self._build_tray()
        self._build_orb()

        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(30)
        self._drain_timer.timeout.connect(self._drain_updates)
        self._drain_timer.start()

        self._metrics_timer = QTimer(self)
        self._metrics_timer.setInterval(
            max(250, int(self.settings.ui.metrics_refresh_ms or 1000)))
        self._metrics_timer.timeout.connect(self._refresh_status)
        self._metrics_timer.start()

        self.show_page("dashboard")
        self._refresh_status()
        self.conversation.append_notice(
            f"{name} ready",
            "Everything runs on this computer: the model (Ollama), the vision, the "
            "voice and the memory. Nothing is uploaded."
            + ("  DEMO MODE is active - actions are simulated."
               if self.runtime.platform.is_simulated else ""),
            "warn" if self.runtime.platform.is_simulated else "ok")
        if show_wizard or not bool(self.settings.general.first_run_complete):
            QTimer.singleShot(250, self.run_wizard)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("root")
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        self.pages = build_pages(self.theme, self.controller, central)
        self.stack = QStackedWidget()
        for page_id in PAGE_ORDER:
            self.stack.addWidget(self.pages[page_id])

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.stack)

        conversation_holder = QFrame()
        conversation_holder.setObjectName("card")
        convo_layout = QVBoxLayout(conversation_holder)
        convo_layout.setContentsMargins(10, 8, 10, 8)
        convo_layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Conversation")
        title.setObjectName("cardTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.trace_toggle = QPushButton("Tool trace")
        self.trace_toggle.setCheckable(True)
        self.trace_toggle.setChecked(bool(self.settings.ui.show_tool_trace))
        self.trace_toggle.setObjectName("ghost")
        self.trace_toggle.toggled.connect(self._toggle_trace)
        header.addWidget(self.trace_toggle)
        clear_button = QPushButton("Clear view")
        clear_button.setObjectName("ghost")
        clear_button.clicked.connect(self._clear_view)
        header.addWidget(clear_button)
        convo_layout.addLayout(header)

        inner = QSplitter(Qt.Orientation.Horizontal)
        self.conversation = ConversationView(self.theme)
        self.conversation.setMinimumHeight(140)
        inner.addWidget(self.conversation)
        self.trace = ToolTracePanel(self.theme)
        self.trace.setVisible(bool(self.settings.ui.show_tool_trace))
        inner.addWidget(self.trace)
        inner.setSizes([760, 380])
        convo_layout.addWidget(inner, 1)

        self.command_bar = CommandBar(self.theme)
        self.command_bar.submitted.connect(self._on_submit)
        self.command_bar.stop_requested.connect(self._on_stop)
        self.command_bar.mic_requested.connect(self._on_mic)
        self.command_bar.mic_released.connect(self._on_mic_release)
        convo_layout.addWidget(self.command_bar)

        splitter.addWidget(conversation_holder)
        splitter.setSizes([520, 340])
        right.addWidget(splitter, 1)

        self.status_strip = StatusStrip(self.theme)
        right.addWidget(self.status_strip)

        root.addLayout(right, 1)
        self.setCentralWidget(central)
        QApplication.instance().setStyleSheet(stylesheet(self.theme))

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(int(self.theme.metrics["sidebar_width"]))
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 18, 12, 12)
        layout.setSpacing(6)

        brand = QLabel(self.settings.general.assistant_name or "JARVIS")
        brand.setObjectName("brandTitle")
        layout.addWidget(brand)
        subtitle = QLabel("LOCAL AI · OFFLINE")
        subtitle.setObjectName("brandSubtitle")
        layout.addWidget(subtitle)
        layout.addSpacing(14)

        self.nav_buttons: Dict[str, QPushButton] = {}
        button_group_layout = QVBoxLayout()
        button_group_layout.setSpacing(2)
        for item in nav_items():
            button = QPushButton(f"{item['icon']}   {item['title']}")
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, page=item["id"]:
                                   self.show_page(page))
            self.nav_buttons[item["id"]] = button
            button_group_layout.addWidget(button)
        layout.addLayout(button_group_layout)
        layout.addStretch(1)

        self.sidebar_status = QLabel("Starting…")
        self.sidebar_status.setObjectName("hint")
        self.sidebar_status.setWordWrap(True)
        layout.addWidget(self.sidebar_status)

        self.stop_button = QPushButton("■  Stop current task")
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._on_stop)
        layout.addWidget(self.stop_button)
        return sidebar

    def _build_shortcuts(self) -> None:
        """Push-to-talk plus a few conveniences, bound from settings."""
        key = str(self.settings.speech.push_to_talk_key or "ctrl+space").strip()
        sequence = QKeySequence(key.replace("+", "+"))
        if not sequence.isEmpty():
            talk = QShortcut(sequence, self)
            talk.setContext(Qt.ShortcutContext.ApplicationShortcut)
            talk.activated.connect(self._on_push_to_talk)
        stop = QShortcut(QKeySequence("Esc"), self)
        stop.setContext(Qt.ShortcutContext.WindowShortcut)
        stop.activated.connect(self._on_stop)
        focus = QShortcut(QKeySequence("Ctrl+L"), self)
        focus.activated.connect(self.command_bar.focus_input)
        for index, page_id in enumerate(PAGE_ORDER, start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index}"), self)
            shortcut.activated.connect(lambda page=page_id: self.show_page(page))

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = self.windowIcon()
        if icon.isNull():
            icon = QApplication.style().standardIcon(
                QApplication.style().StandardPixelMetric.SP_ComputerIcon)
        self._tray = QSystemTrayIcon(icon, self)
        name = self.settings.general.assistant_name or "JARVIS"
        show_action = QAction(f"Show {name}", self)
        show_action.triggered.connect(self._show_from_tray)
        talk_action = QAction("Speak a command", self)
        talk_action.triggered.connect(self._on_push_to_talk)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_application)
        menu_actions = [show_action, talk_action, None, quit_action]
        from PySide6.QtWidgets import QMenu

        menu = QMenu()
        for action in menu_actions:
            if action is None:
                menu.addSeparator()
            else:
                menu.addAction(action)
        self._tray.setContextMenu(menu)
        self._tray.setToolTip(f"{name} - local AI assistant")
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _build_orb(self) -> None:
        """The floating bottom-right orb: state colours + mini chat.

        Independent top-level widget, so it stays visible (and JARVIS keeps
        working) when this window is minimised or closed to the tray.
        """
        self.orb = None
        if not bool(getattr(self.settings.ui, "show_desktop_orb", True)):
            return
        try:
            from .widgets.orb import DesktopOrb

            self.orb = DesktopOrb(self.controller, self.theme)
            self.orb.show()
        except Exception:  # noqa: BLE001 - the orb is an enhancement, never fatal
            log.exception("desktop orb could not be created")

    # ------------------------------------------------------------------
    # navigation
    # ------------------------------------------------------------------
    def show_page(self, page_id: str) -> None:
        if page_id not in self.pages:
            return
        self.stack.setCurrentWidget(self.pages[page_id])
        for key, button in self.nav_buttons.items():
            button.setChecked(key == page_id)
        page = self.pages[page_id]
        try:
            page.refresh()
        except Exception:  # noqa: BLE001
            log.exception("page refresh failed: %s", page_id)
        self.command_bar.focus_input()

    def run_wizard(self) -> None:
        from .wizard import SetupWizard

        wizard = SetupWizard(self.theme, self.controller, self)
        wizard.exec()
        self.settings = self.runtime.settings_provider()
        self._refresh_status()
        self.show_page("dashboard")

    # ------------------------------------------------------------------
    # command handling
    # ------------------------------------------------------------------
    def _on_submit(self, text: str) -> None:
        if not text.strip():
            return
        # The controller echoes the user line back through the queue, so the
        # conversation shows it exactly once.
        self._begin_stream()
        accepted = self.controller.submit(text)
        if not accepted:
            self._end_stream("")
            self.conversation.append_notice(
                "Busy", "JARVIS is still working on the previous request. "
                        "Press Stop to interrupt it.", "warn")

    def _on_stop(self) -> None:
        self.controller.stop()
        self._end_stream("")
        self.conversation.append_notice("Stopped", "The current task was cancelled.",
                                        "warn")
        self.command_bar.set_busy(False)
        self.stop_button.setEnabled(False)

    def _on_mic(self) -> None:
        self._listen()

    def _on_mic_release(self) -> None:
        self.command_bar.set_mic_listening(False)

    def _on_push_to_talk(self) -> None:
        if self.command_bar.mic_button.isChecked():
            self.command_bar.set_mic_listening(False)
            self._on_mic_release()
            return
        self.command_bar.set_mic_listening(True)
        self._listen()

    def _listen(self) -> None:
        speech = getattr(self.runtime, "speech", None)
        if speech is None:
            self.conversation.append_notice(
                "Voice unavailable",
                "The speech subsystem is not loaded on this machine. You can still "
                "type commands below.", "warn")
            self.command_bar.set_mic_available(False, "No speech subsystem")
            return
        stopper = getattr(speech, "stop_speaking", None)
        if callable(stopper):
            stopper()                                  # barge-in
        self.command_bar.set_mic_listening(True)
        self.status_strip.set_state(AgentState.LISTENING.value)
        self.controller.listen_once()

    # ------------------------------------------------------------------
    # update pump
    # ------------------------------------------------------------------
    def _drain_updates(self) -> None:
        for update in self.controller.drain(limit=80):
            try:
                self._handle_update(update.kind, update.data)
            except Exception:  # noqa: BLE001
                log.exception("UI update failed (%s)", update.kind)

    def _handle_update(self, kind: str, data: Any) -> None:
        payload = data if isinstance(data, dict) else {}
        if kind == K_STATE:
            state = str(payload.get("state", "idle"))
            self.status_strip.set_state(state)
            if self.orb is not None:
                self.orb.set_state(state)
            dashboard = self.pages.get("dashboard")
            if dashboard is not None:
                dashboard.core_panel.set_state(state)
            busy = state not in (AgentState.IDLE.value, AgentState.ERROR.value)
            self.command_bar.set_busy(busy)
            self.stop_button.setEnabled(busy)
        elif kind == K_STREAM:
            self._extend_stream(str(payload.get("text", "")))
        elif kind == K_MESSAGE:
            role = str(payload.get("role", "assistant"))
            text = str(payload.get("text", ""))
            if role == "assistant":
                self._end_stream(text)
            else:
                self.conversation.append_line(role, text, ts=payload.get("ts"))
            if self.orb is not None:
                self.orb.append_line(role, text)
        elif kind == K_THOUGHT:
            thought = str(payload.get("thought", ""))
            if thought and bool(self.settings.ui.show_tool_trace):
                self.trace.set_status_text(thought[:300])
        elif kind == K_TOOL:
            self._handle_tool(payload)
        elif kind == K_APPROVAL:
            self._show_approval(payload)
        elif kind == K_QUESTION:
            self._show_question(payload)
        elif kind == K_APPROVAL_RESOLVED:
            pass
        elif kind == K_TASK:
            stage = payload.get("stage")
            if stage == "started":
                self.command_bar.set_busy(True)
                self.stop_button.setEnabled(True)
                self.trace.clear()
            elif stage == "finished":
                self.command_bar.set_busy(False)
                self.stop_button.setEnabled(False)
        elif kind == K_METRICS or kind == K_OLLAMA:
            self._refresh_status()
        elif kind == K_NOTIFICATION:
            self._notify(str(payload.get("title", "JARVIS")),
                         str(payload.get("message", "")))
        elif kind == K_SPEECH:
            listening = payload.get("kind") == "listening"
            if listening:
                self.command_bar.set_mic_listening(False)
        elif kind == K_LEVEL:
            if self.orb is not None:
                try:
                    self.orb.set_level(float(payload.get("level", 0.0)))
                except Exception:  # noqa: BLE001
                    pass
            dashboard = self.pages.get("dashboard")
            if dashboard is not None:
                dashboard.core_panel.set_levels(mic=float(payload.get("level", 0.0)))
        elif kind == K_TTS_LEVEL:
            dashboard = self.pages.get("dashboard")
            if dashboard is not None:
                dashboard.core_panel.set_levels(tts=float(payload.get("level", 0.0)))
        elif kind == K_LOG:
            pass

        page = self.stack.currentWidget()
        handler = getattr(page, "on_event", None)
        if callable(handler):
            try:
                handler(kind, data)
            except Exception:  # noqa: BLE001
                log.debug("page event handler failed", exc_info=True)

    def _handle_tool(self, payload: Dict[str, Any]) -> None:
        tool = str(payload.get("tool", ""))
        phase = str(payload.get("phase", ""))
        if phase == "call":
            self.status_strip.state_label.setText(f"Running {tool}…")
            if bool(self.settings.ui.show_tool_trace):
                self.trace.add_call(int(payload.get("step", 0) or 0), tool,
                                    payload.get("args") or {})
            return
        verification = payload.get("verification") or {}
        verified = verification.get("succeeded") if verification.get("checked") else None
        message = str(payload.get("message") or payload.get("error") or "")
        ok = bool(payload.get("ok"))
        if bool(self.settings.ui.show_tool_trace):
            self.trace.add_result(int(payload.get("step", 0) or 0), tool, ok, message,
                                  verified, str(verification.get("detail", "")),
                                  float(payload.get("duration_ms", 0.0)))
        self.conversation.append_tool(tool, ok, message, verified,
                                      str(verification.get("detail", "")),
                                      float(payload.get("duration_ms", 0.0)))
        if payload.get("blocked"):
            self.conversation.append_notice(
                "Refused by security policy",
                f"{tool}: {payload.get('reason', '')}", "danger")
        elif payload.get("denied"):
            self.conversation.append_notice(
                "You declined this action", f"{tool}: {payload.get('reason', '')}",
                "warn")

    # ------------------------------------------------------------------
    # approvals
    # ------------------------------------------------------------------
    def _show_approval(self, request: Dict[str, Any]) -> None:
        request_id = int(request.get("id", 0) or 0)
        dialog = ApprovalDialog(self.theme, request, self)
        dialog.exec()
        outcome = {"approved": Outcome.APPROVED,
                   "approved_for_session": Outcome.APPROVED_FOR_SESSION,
                   "denied": Outcome.DENIED}.get(dialog.outcome, Outcome.DENIED)
        self.controller.resolve_approval(request_id, outcome, note=dialog.note)
        self.conversation.append_notice(
            "Approval " + ("granted" if outcome is not Outcome.DENIED else "declined"),
            f"{request.get('tool', '')}: "
            f"{'allowed' if outcome is not Outcome.DENIED else 'not allowed'}"
            + (f" ({dialog.note})" if dialog.note else ""),
            "ok" if outcome is not Outcome.DENIED else "warn")

    def _show_question(self, request: Dict[str, Any]) -> None:
        request_id = int(request.get("id", 0) or 0)
        dialog = QuestionDialog(self.theme, request, self)
        accepted = dialog.exec() == QuestionDialog.DialogCode.Accepted
        answer = dialog.answer if accepted else ""
        self.controller.resolve_approval(request_id, Outcome.APPROVED, answer=answer)
        self.conversation.append_line("system",
                                      f"JARVIS asked: {request.get('question', '')}\n"
                                      f"You answered: {answer or '(skipped)'}")

    # ------------------------------------------------------------------
    # streaming text
    # ------------------------------------------------------------------
    def _begin_stream(self) -> None:
        self._stream_text = []
        self._streaming = True

    def _extend_stream(self, text: str) -> None:
        if not text:
            return
        if not self._streaming:
            self._streaming = True
            self._stream_text = []
            self.conversation.begin_stream("assistant", text)
        else:
            self.conversation.update_stream(text)
        self._stream_text = [text]

    def _end_stream(self, final_text: str) -> None:
        if not self._streaming:
            if final_text:
                self.conversation.append_line("assistant", final_text)
            return
        self._streaming = False
        fallback = final_text or "".join(self._stream_text)
        self.conversation.end_stream(fallback)
        self._stream_text = []

    # ------------------------------------------------------------------
    def _refresh_status(self) -> None:
        try:
            status = self.controller.status()
        except Exception:  # noqa: BLE001
            return
        self.status_strip.update_from(status)
        ollama = status.get("ollama") or {}
        speech = status.get("speech") or {}
        stt_ok = bool((speech.get("stt") or {}).get("available"))
        self.command_bar.set_mic_available(
            stt_ok, "" if stt_ok else str((speech.get("stt") or {}).get("detail", "")))
        parts = []
        if not ollama.get("reachable"):
            parts.append("Ollama is not reachable - open the Models page.")
        elif not ollama.get("main_model"):
            parts.append("No model selected - open the Models page.")
        resources = status.get("resources") or {}
        if str(resources.get("pressure", "ok")) != "ok":
            parts.append(f"Resource pressure: {resources.get('pressure')}")
        if not parts:
            parts.append("All systems nominal.")
        self.sidebar_status.setText(" · ".join(parts))
        self.command_bar.set_hint(
            "Enter to send · Esc to stop · "
            f"{self.settings.speech.push_to_talk_key} to talk · "
            "Ctrl+1…8 to switch pages")

    def _notify(self, title: str, message: str) -> None:
        if not message:
            return
        if self._tray is not None and not self.isActiveWindow():
            self._tray.showMessage(title, message[:220], self.windowIcon(), 4000)
        self.status_strip.state_label.setToolTip(f"{title}: {message}")

    # ------------------------------------------------------------------
    def _toggle_trace(self, visible: bool) -> None:
        self.trace.setVisible(bool(visible))
        self.controller.save_settings({"ui": {"show_tool_trace": bool(visible)}})

    def _clear_view(self) -> None:
        self.conversation.clear_all()
        self.trace.clear()

    # ------------------------------------------------------------------
    # tray / close
    # ------------------------------------------------------------------
    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.command_bar.focus_input()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if bool(self.settings.ui.close_to_tray) and self._tray is not None:
            event.ignore()
            self.hide()
            self._tray.showMessage(
                self.settings.general.assistant_name or "JARVIS",
                "Still running in the background. Right-click the tray icon to quit.",
                self.windowIcon(), 2500)
            return
        self.quit_application()
        event.accept()

    def quit_application(self) -> None:
        answer = QMessageBox.question(
            self, "Quit JARVIS",
            "Shut down JARVIS?\n\nAny task in progress will be stopped.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._drain_timer.stop()
        self._metrics_timer.stop()
        if self.orb is not None:
            try:
                self.orb.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.controller.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.runtime.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("runtime shutdown failed")
        QApplication.instance().quit()
