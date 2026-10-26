"""Models: Ollama status, model selection per role, and safe downloads."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..controller import K_MODELS, K_OLLAMA
from ..theme import Theme
from .base import Page, card, hint


class ModelsPage(Page):
    """Live Ollama management."""

    def __init__(self, theme: Theme, controller: Any,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(theme, controller, "Models",
                         "JARVIS reasons with Ollama on this PC. Pick one model per "
                         "role; only one stays loaded in VRAM at a time.", parent)
        self._models: List[Dict[str, Any]] = []
        self._pulling = False

        # --- connection ------------------------------------------------------
        conn_frame, conn = card("Ollama")
        form = QFormLayout()
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("127.0.0.1")
        self.port_edit = QSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(11434)
        self.status_label = QLabel("checking…")
        self.install_hint = hint("")
        self.install_hint.setVisible(False)
        form.addRow("Host", self.host_edit)
        form.addRow("Port", self.port_edit)
        form.addRow("Status", self.status_label)
        conn.addLayout(form)
        conn.addWidget(self.install_hint)
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Re-check Ollama")
        self.refresh_button.clicked.connect(self._refresh_now)
        self.save_conn = QPushButton("Save connection")
        self.save_conn.setObjectName("primary")
        self.save_conn.clicked.connect(self._save_connection)
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.save_conn)
        buttons.addStretch(1)
        conn.addLayout(buttons)
        self.body.addWidget(conn_frame)

        # --- roles -----------------------------------------------------------
        roles_frame, roles = card("Roles")
        role_form = QFormLayout()
        self.main_combo = QComboBox()
        self.vision_combo = QComboBox()
        self.fast_combo = QComboBox()
        for combo in (self.main_combo, self.vision_combo, self.fast_combo):
            combo.setEditable(False)
            combo.setMinimumWidth(320)
        role_form.addRow("Main (reasoning + tool calling)", self.main_combo)
        role_form.addRow("Vision (screenshots)", self.vision_combo)
        role_form.addRow("Fast (short commands)", self.fast_combo)
        roles.addLayout(role_form)
        roles.addWidget(hint(
            "Vision and Fast are optional: leave them empty and JARVIS uses the "
            "main model when it can, or skips the feature and tells you. "
            "Only models that support tool calling can drive the computer."))
        role_buttons = QHBoxLayout()
        self.save_roles = QPushButton("Save model choices")
        self.save_roles.setObjectName("primary")
        self.save_roles.clicked.connect(self._save_roles)
        self.auto_pick = QPushButton("Pick the best available")
        self.auto_pick.clicked.connect(self._auto_pick)
        self.test_button = QPushButton("Test the main model")
        self.test_button.clicked.connect(self._test_main)
        self.unload_button = QPushButton("Unload from VRAM")
        self.unload_button.clicked.connect(self._unload)
        for button in (self.save_roles, self.auto_pick, self.test_button,
                       self.unload_button):
            role_buttons.addWidget(button)
        role_buttons.addStretch(1)
        roles.addLayout(role_buttons)
        self.body.addWidget(roles_frame)

        # --- installed models ------------------------------------------------
        list_frame, list_layout = card("Installed models")
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Model", "Size", "Family", "Tools", "Vision", "Context"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        list_layout.addWidget(self.table)
        table_buttons = QHBoxLayout()
        self.use_main = QPushButton("Use as main")
        self.use_main.clicked.connect(lambda: self._use_selected("main"))
        self.use_vision = QPushButton("Use as vision")
        self.use_vision.clicked.connect(lambda: self._use_selected("vision"))
        self.use_fast = QPushButton("Use as fast")
        self.use_fast.clicked.connect(lambda: self._use_selected("fast"))
        self.delete_button = QPushButton("Delete model")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._delete_model)
        for button in (self.use_main, self.use_vision, self.use_fast, self.delete_button):
            table_buttons.addWidget(button)
        table_buttons.addStretch(1)
        list_layout.addLayout(table_buttons)
        self.body.addWidget(list_frame, 1)

        # --- download --------------------------------------------------------
        pull_frame, pull = card("Download a model")
        pull.addWidget(hint(
            "Downloading needs your approval every time - it is the only network "
            "access JARVIS performs, and it only ever talks to Ollama, which "
            "fetches from its own registry."))
        pull_row = QHBoxLayout()
        self.pull_edit = QLineEdit()
        self.pull_edit.setPlaceholderText("e.g. qwen2.5:7b-instruct")
        self.pull_button = QPushButton("Download")
        self.pull_button.clicked.connect(self._pull)
        pull_row.addWidget(self.pull_edit, 1)
        pull_row.addWidget(self.pull_button)
        pull.addLayout(pull_row)
        self.pull_bar = QProgressBar()
        self.pull_bar.setRange(0, 100)
        self.pull_bar.setValue(0)
        self.pull_bar.setVisible(False)
        pull.addWidget(self.pull_bar)
        self.pull_label = hint("")
        pull.addWidget(self.pull_label)
        self.body.addWidget(pull_frame)

        # --- tuning ----------------------------------------------------------
        tuning_frame, tuning = card("Generation settings")
        tuning_form = QFormLayout()
        self.ctx_spin = QSpinBox()
        self.ctx_spin.setRange(1024, 131072)
        self.ctx_spin.setSingleStep(1024)
        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.05)
        self.temp_spin.setDecimals(2)
        self.keepalive_edit = QLineEdit()
        self.keepalive_edit.setPlaceholderText("10m")
        self.warm_box = QPushButton("Warm the main model now")
        self.warm_box.clicked.connect(self._warm)
        tuning_form.addRow("Context window (tokens)", self.ctx_spin)
        tuning_form.addRow("Temperature", self.temp_spin)
        tuning_form.addRow("Keep loaded for", self.keepalive_edit)
        tuning_form.addRow("", self.warm_box)
        tuning.addLayout(tuning_form)
        tuning.addWidget(hint("A larger context uses more VRAM. On an 8 GB card, "
                              "8192 tokens is a good balance for a 7B model."))
        tuning_row = QHBoxLayout()
        self.save_tuning = QPushButton("Save generation settings")
        self.save_tuning.setObjectName("primary")
        self.save_tuning.clicked.connect(self._save_tuning)
        tuning_row.addWidget(self.save_tuning)
        tuning_row.addStretch(1)
        tuning.addLayout(tuning_row)
        self.body.addWidget(tuning_frame)

        self.add_footer_button("Refresh", self._refresh_now, "ghost")

        self._poll = QTimer(self)
        self._poll.setInterval(int(self.settings().ollama.refresh_interval_ms or 15000))
        self._poll.timeout.connect(self._poll_tick)

    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.refresh()
        if not self._poll.isActive():
            self._poll.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._poll.stop()
        super().hideEvent(event)

    def _poll_tick(self) -> None:
        if self.isVisible() and not self._pulling:
            self.controller.refresh_models()

    def refresh(self) -> None:
        settings = self.settings()
        self.host_edit.setText(str(settings.ollama.host))
        self.port_edit.setValue(int(settings.ollama.port))
        self.ctx_spin.setValue(int(settings.ollama.num_ctx))
        self.temp_spin.setValue(float(settings.ollama.temperature))
        self.keepalive_edit.setText(str(settings.ollama.keep_alive))
        self.controller.refresh_models()
        self._apply_status(self.controller.status())

    def _refresh_now(self) -> None:
        self.controller.refresh_models()
        self.set_status("Asking Ollama for its model list…")

    def _apply_status(self, status: Dict[str, Any]) -> None:
        ollama = status.get("ollama") or {}
        reachable = bool(ollama.get("reachable"))
        installed = bool(ollama.get("installed"))
        detail = str(ollama.get("detail") or "")
        if reachable:
            self.status_label.setText("Connected")
            self.status_label.setStyleSheet(f"color: {self.theme.colour('success')};")
            self.install_hint.setVisible(False)
        else:
            self.status_label.setText("Not reachable")
            self.status_label.setStyleSheet(f"color: {self.theme.colour('danger')};")
            self.install_hint.setText(
                ("Ollama is not answering at "
                 f"{self.host_edit.text().strip()}:{self.port_edit.value()}.\n"
                 + ("It is installed - start it from the system tray, or run "
                    "'ollama serve'.\n" if installed else
                    "It does not appear to be installed. Download it from "
                    "https://ollama.com/download, install it, then pull a model:\n"
                    "    ollama pull qwen2.5:7b-instruct\n")
                 + (detail or "")))
            self.install_hint.setVisible(True)
        models = list(ollama.get("models") or [])
        if models != self._models:
            self._models = models
            self._fill_table()
            self._fill_combos()
        for combo, key in ((self.main_combo, "main_model"),
                           (self.vision_combo, "vision_model"),
                           (self.fast_combo, "fast_model")):
            wanted = str(ollama.get(key) or "")
            index = combo.findData(wanted)
            if index >= 0:
                combo.setCurrentIndex(index)
        tools = status.get("tools") or {}
        self.set_status(f"{len(models)} model(s) installed · "
                        f"{tools.get('available', 0)}/{tools.get('total', 0)} tools ready")

    def _fill_table(self) -> None:
        table = self.table
        table.setRowCount(0)
        for model in self._models:
            row = table.rowCount()
            table.insertRow(row)
            values = (model.get("name", ""), f"{float(model.get('size_gb', 0)):.2f} GB",
                      model.get("family", "") or "-",
                      "yes" if model.get("supports_tools") else "no",
                      "yes" if model.get("supports_vision") else "no",
                      str(model.get("context_length") or "-"))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column in (3, 4):
                    item.setForeground(QColor(self.theme.colour(
                        "success" if value == "yes" else "text_muted")))
                table.setItem(row, column, item)
            table.item(row, 0).setData(Qt.ItemDataRole.UserRole, model.get("name", ""))

    def _fill_combos(self) -> None:
        current = {combo: combo.currentData() for combo in
                   (self.main_combo, self.vision_combo, self.fast_combo)}
        names = [m.get("name", "") for m in self._models]
        for combo in (self.main_combo, self.vision_combo, self.fast_combo):
            combo.clear()
            combo.addItem("(not set)", "")
            for name in names:
                marker = ""
                model = next((m for m in self._models if m.get("name") == name), {})
                if combo is self.main_combo and not model.get("supports_tools"):
                    marker = "  (no tool calling)"
                if combo is self.vision_combo and not model.get("supports_vision"):
                    marker = "  (no vision)"
                combo.addItem(f"{name}{marker}", name)
            wanted = current.get(combo)
            index = combo.findData(wanted) if wanted else -1
            combo.setCurrentIndex(index if index >= 0 else 0)

    def _selected_model(self) -> str:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return ""
        return str(self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole) or "")

    # ------------------------------------------------------------------
    def _save_connection(self) -> None:
        result = self.controller.save_settings({"ollama": {
            "host": self.host_edit.text().strip() or "127.0.0.1",
            "port": int(self.port_edit.value()),
        }})
        self.set_status("Connection saved; re-checking…" if result.get("ok")
                        else f"Could not save: {result.get('error')}",
                        "ok" if result.get("ok") else "danger")
        self.controller.refresh_models()

    def _save_roles(self) -> None:
        result = self.controller.save_settings({"ollama": {
            "main_model": str(self.main_combo.currentData() or ""),
            "vision_model": str(self.vision_combo.currentData() or ""),
            "fast_model": str(self.fast_combo.currentData() or ""),
        }})
        if result.get("ok"):
            self.controller.runtime.router.invalidate()
            self.set_status("Model roles saved.", "ok")
            threading.Thread(target=self.controller.runtime.warm_main_model,
                             name="jarvis-warm", daemon=True).start()
        else:
            self.set_status(f"Could not save: {result.get('error')}", "danger")

    def _save_tuning(self) -> None:
        result = self.controller.save_settings({"ollama": {
            "num_ctx": int(self.ctx_spin.value()),
            "temperature": float(self.temp_spin.value()),
            "keep_alive": self.keepalive_edit.text().strip() or "10m",
        }})
        self.set_status("Generation settings saved." if result.get("ok")
                        else f"Could not save: {result.get('error')}",
                        "ok" if result.get("ok") else "danger")

    def _use_selected(self, role: str) -> None:
        model = self._selected_model()
        if not model:
            self.set_status("Select a model in the table first.", "warn")
            return
        key = {"main": "main_model", "vision": "vision_model",
               "fast": "fast_model"}[role]
        self.controller.save_settings({"ollama": {key: model}})
        self.controller.runtime.router.invalidate()
        self.set_status(f"{model} is now the {role} model.", "ok")
        self.refresh()

    def _auto_pick(self) -> None:
        chosen = self.controller.runtime.router.auto_select_main()
        if not chosen:
            self.set_status("No models are installed yet.", "warn")
            return
        self.controller.save_settings({"ollama": {"main_model": chosen}})
        self.controller.runtime.router.invalidate()
        self.set_status(f"Selected {chosen} as the main model.", "ok")
        self.refresh()

    def _test_main(self) -> None:
        model = str(self.main_combo.currentData() or "")
        if not model:
            self.set_status("Choose a main model first.", "warn")
            return
        self.set_status(f"Testing {model}… (this can take a few seconds)")
        self.test_button.setEnabled(False)

        def _run() -> None:
            started = time.time()
            try:
                result = self.controller.runtime.ollama.chat(
                    [{"role": "user", "content": "Reply with the single word: ready"}],
                    model=model, stream=False)
                took = (time.time() - started) * 1000
                text = (result.content or "").strip()[:120]
                message = (f"{model} answered '{text}' in {took:.0f} ms "
                           f"({result.tokens_per_second:.1f} tok/s)") \
                    if result.ok else f"{model} failed: {result.error}"
                ok = result.ok
            except Exception as exc:  # noqa: BLE001
                message, ok = f"{model} failed: {exc}", False
            QTimer.singleShot(0, lambda: self._test_done(message, ok))

        threading.Thread(target=_run, name="jarvis-model-test", daemon=True).start()

    def _test_done(self, message: str, ok: bool) -> None:
        self.test_button.setEnabled(True)
        self.set_status(message, "ok" if ok else "danger")

    def _unload(self) -> None:
        model = self._selected_model() or str(self.main_combo.currentData() or "")
        if not model:
            self.set_status("Select a model first.", "warn")
            return
        freed = self.controller.runtime.ollama.unload(model)
        self.set_status(f"{model} unloaded from VRAM." if freed
                        else f"Could not unload {model}.", "ok" if freed else "warn")

    def _warm(self) -> None:
        model = str(self.main_combo.currentData() or "")
        if not model:
            self.set_status("Choose a main model first.", "warn")
            return
        self.set_status(f"Loading {model} into VRAM…")
        threading.Thread(
            target=lambda: QTimer.singleShot(
                0, lambda: self.set_status(
                    f"{model} is loaded and ready." if
                    self.controller.runtime.warm_main_model() else
                    f"Could not warm {model}.", "ok")),
            name="jarvis-warm", daemon=True).start()

    def _delete_model(self) -> None:
        model = self._selected_model()
        if not model:
            self.set_status("Select a model first.", "warn")
            return
        answer = QMessageBox.question(
            self, "Delete model",
            f"Delete '{model}' from this computer?\n\nThe files are removed from "
            "Ollama's storage. This cannot be undone without downloading it again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        ok = self.controller.runtime.ollama.delete_model(model)
        self.set_status(f"Deleted {model}." if ok else f"Could not delete {model}.",
                        "ok" if ok else "danger")
        self.controller.refresh_models()

    # ------------------------------------------------------------------
    def _pull(self) -> None:
        model = self.pull_edit.text().strip()
        if not model:
            self.set_status("Type a model name to download.", "warn")
            return
        answer = QMessageBox.question(
            self, "Approve download",
            f"Download '{model}' from Ollama's registry?\n\n"
            "What: a model file, typically 2-8 GB, saved to Ollama's folder.\n"
            "Consequences: uses disk space and bandwidth; nothing is uploaded.\n"
            "Reversible: yes - you can delete it from this page.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._pulling = True
        self.pull_bar.setVisible(True)
        self.pull_bar.setValue(0)
        self.pull_button.setEnabled(False)
        self.pull_label.setText(f"Downloading {model}…")

        def _progress(data: Dict[str, Any]) -> None:
            total = float(data.get("total") or 0)
            completed = float(data.get("completed") or 0)
            percent = int(min(100, (completed / total) * 100)) if total else 0
            status = str(data.get("status") or "")
            QTimer.singleShot(0, lambda: self._pull_progress(percent, status,
                                                             completed, total))

        def _run() -> None:
            try:
                ok = self.controller.runtime.ollama.pull(model, on_progress=_progress)
                message = f"{model} downloaded." if ok else f"Download of {model} failed."
            except Exception as exc:  # noqa: BLE001
                ok, message = False, f"Download failed: {exc}"
            self._pulling = False
            QTimer.singleShot(0, lambda: self._pull_done(ok, message))

        threading.Thread(target=_run, name="jarvis-pull", daemon=True).start()

    def _pull_progress(self, percent: int, status: str, completed: float,
                       total: float) -> None:
        self.pull_bar.setValue(percent)
        size = f"{completed / (1024 ** 3):.2f}/{total / (1024 ** 3):.2f} GB" \
            if total else ""
        self.pull_label.setText(f"{status} {percent}% {size}".strip())

    def _pull_done(self, ok: bool, message: str) -> None:
        self.pull_button.setEnabled(True)
        self.pull_bar.setVisible(False)
        self.pull_label.setText(message)
        self.set_status(message, "ok" if ok else "danger")
        if ok:
            self.controller.refresh_models()

    # ------------------------------------------------------------------
    def on_event(self, kind: str, data: Any) -> None:
        if kind in (K_OLLAMA, K_MODELS):
            self._apply_status(self.controller.status())
