"""General: identity, appearance, logging, storage location and the ceilings."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ... import paths
from ..theme import Theme
from .base import Page, card, hint

PERSONALITIES = [("concise", "Concise - short, direct answers"),
                 ("detailed", "Detailed - explains what it did and why"),
                 ("playful", "Playful - dry, butler-like wit")]
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]
ACCENTS = [("#22d3ee", "Cyan (default)"), ("#3b82f6", "Blue"),
           ("#a78bfa", "Violet"), ("#34d399", "Green"), ("#fbbf24", "Amber"),
           ("#f87171", "Red")]


class GeneralPage(Page):
    """Identity, appearance, logging and the read-only resource ceilings."""

    def __init__(self, theme: Theme, controller: Any,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(theme, controller, "General",
                         "How JARVIS behaves and where it keeps its files.", parent)
        settings = self.settings()
        g, u, sec = settings.general, settings.ui, settings.security

        # --- identity --------------------------------------------------------
        ident_frame, ident = card("Assistant")
        form = QFormLayout()
        self.name_edit = QLineEdit(str(g.assistant_name))
        self.personality = QComboBox()
        for value, label in PERSONALITIES:
            self.personality.addItem(label, value)
        index = self.personality.findData(str(g.personality))
        self.personality.setCurrentIndex(index if index >= 0 else 0)
        self.language = QLineEdit(str(g.language))
        self.wizard_button = QPushButton("Run the first-time setup again")
        self.wizard_button.clicked.connect(self._run_wizard)
        form.addRow("Name", self.name_edit)
        form.addRow("Personality", self.personality)
        form.addRow("Language", self.language)
        form.addRow("", self.wizard_button)
        ident.addLayout(form)
        ident.addWidget(hint("The name and language are used in speech and in the "
                             "prompts JARVIS reasons with."))
        self.body.addWidget(ident_frame)

        # --- appearance ------------------------------------------------------
        look_frame, look = card("Appearance")
        lform = QFormLayout()
        self.accent = QComboBox()
        for value, label in ACCENTS:
            self.accent.addItem(label, value)
        index = self.accent.findData(str(u.accent))
        self.accent.setCurrentIndex(index if index >= 0 else 0)
        self.accent_preview = QLabel("  ●●●  ")
        self.accent.currentIndexChanged.connect(self._preview_accent)
        self.font_family = QLineEdit(str(u.font_family))
        self.mono_family = QLineEdit(str(u.mono_font_family))
        self.font_size = QSpinBox()
        self.font_size.setRange(8, 18)
        self.font_size.setValue(int(u.base_font_size))
        self.conversation_size = QSpinBox()
        self.conversation_size.setRange(9, 22)
        self.conversation_size.setValue(int(u.conversation_font_size))
        self.animation = QDoubleSpinBox()
        self.animation.setRange(0.0, 2.0)
        self.animation.setSingleStep(0.1)
        self.animation.setDecimals(2)
        self.animation.setValue(float(u.animation_speed))
        self.core_box = QPushButton("Show the animated core")
        self.core_box.setCheckable(True)
        self.core_box.setChecked(bool(u.core_visualiser))
        self.trace_box = QPushButton("Show the tool trace in the conversation")
        self.trace_box.setCheckable(True)
        self.trace_box.setChecked(bool(u.show_tool_trace))
        lform.addRow("Accent colour", self._row(self.accent, self.accent_preview))
        lform.addRow("Font", self.font_family)
        lform.addRow("Monospace font", self.mono_family)
        lform.addRow("Interface size (pt)", self.font_size)
        lform.addRow("Conversation size (pt)", self.conversation_size)
        lform.addRow("Animation speed (0 = off)", self.animation)
        lform.addRow("", self.core_box)
        lform.addRow("", self.trace_box)
        look.addLayout(lform)
        self.body.addWidget(look_frame)

        # --- startup and window ---------------------------------------------
        start_frame, start = card("Start-up and window")
        sform = QFormLayout()
        self.start_min = QPushButton("Start minimised to the tray")
        self.start_min.setCheckable(True)
        self.start_min.setChecked(bool(u.start_minimised))
        self.close_tray = QPushButton("Keep running in the tray when I close the window")
        self.close_tray.setCheckable(True)
        self.close_tray.setChecked(bool(u.close_to_tray))
        self.opacity = QDoubleSpinBox()
        self.opacity.setRange(0.5, 1.0)
        self.opacity.setSingleStep(0.05)
        self.opacity.setDecimals(2)
        self.opacity.setValue(float(u.window_opacity))
        self.metrics_box = QPushButton("Show live CPU / RAM / VRAM metrics")
        self.metrics_box.setCheckable(True)
        self.metrics_box.setChecked(bool(u.show_status_metrics))
        self.metrics_ms = QSpinBox()
        self.metrics_ms.setRange(250, 10000)
        self.metrics_ms.setSingleStep(250)
        self.metrics_ms.setSuffix(" ms")
        self.metrics_ms.setValue(int(u.metrics_refresh_ms))
        sform.addRow("", self.start_min)
        sform.addRow("", self.close_tray)
        sform.addRow("Window opacity", self.opacity)
        sform.addRow("", self.metrics_box)
        sform.addRow("Metrics refresh", self.metrics_ms)
        start.addLayout(sform)
        self.body.addWidget(start_frame)

        # --- logging and storage --------------------------------------------
        log_frame, log_layout = card("Logging and storage")
        gform = QFormLayout()
        self.log_level = QComboBox()
        self.log_level.addItems(LOG_LEVELS)
        self.log_level.setCurrentText(str(g.log_level))
        self.log_to_file = QPushButton("Write a log file")
        self.log_to_file.setCheckable(True)
        self.log_to_file.setChecked(bool(g.log_to_file))
        self.log_kept = QSpinBox()
        self.log_kept.setRange(1, 60)
        self.log_kept.setSuffix(" files")
        self.log_kept.setValue(int(g.log_files_kept))
        gform.addRow("Level", self.log_level)
        gform.addRow("", self.log_to_file)
        gform.addRow("Keep", self.log_kept)
        log_layout.addLayout(gform)
        log_layout.addWidget(hint(
            f"Everything JARVIS stores lives in:\n{paths.data_dir()}\n"
            "That folder holds the settings, the memory database, the security "
            "audit log, screenshots and the downloaded voices. Delete it and "
            "JARVIS starts fresh."))
        buttons = QHBoxLayout()
        self.open_folder = QPushButton("Open the JARVIS folder")
        self.open_folder.clicked.connect(self._open_folder)
        self.diagnostics = QPushButton("Write a diagnostics file")
        self.diagnostics.clicked.connect(self._diagnostics)
        buttons.addWidget(self.open_folder)
        buttons.addWidget(self.diagnostics)
        buttons.addStretch(1)
        log_layout.addLayout(buttons)
        self.body.addWidget(log_frame)

        # --- ceilings (read-only) -------------------------------------------
        ceil_frame, ceil = card("Resource ceilings (locked)")
        ceil_form = QFormLayout()
        ram = QLabel(f"{sec.max_ram_gb:.1f} GB maximum")
        vram = QLabel(f"{sec.max_vram_gb:.1f} GB maximum")
        ram.setStyleSheet(f"color: {theme.colour('accent')}; font-weight: 600;")
        vram.setStyleSheet(f"color: {theme.colour('accent')}; font-weight: 600;")
        ceil_form.addRow("Process RAM", ram)
        ceil_form.addRow("GPU memory", vram)
        ceil.addLayout(ceil_form)
        ceil.addWidget(hint(
            "These are hard ceilings, not targets. JARVIS monitors itself "
            "continuously and sheds load - smaller screenshots, no background "
            "observation, unloading models - before it gets near them, and tells "
            "you when it does. They cannot be raised from the interface."))
        telemetry = QPushButton("Telemetry and cloud AI: permanently off 🔒")
        telemetry.setCheckable(True)
        telemetry.setChecked(True)
        telemetry.setEnabled(False)
        ceil.addWidget(telemetry)
        self.body.addWidget(ceil_frame)

        self.add_footer_button("Save general settings", self._save, "primary")
        self.add_footer_button("Reload", self.refresh, "ghost")
        self._preview_accent()

    # ------------------------------------------------------------------
    def _row(self, *widgets: QWidget) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for widget in widgets:
            layout.addWidget(widget)
        layout.addStretch(1)
        return holder

    def _preview_accent(self) -> None:
        colour = str(self.accent.currentData() or "#22d3ee")
        self.accent_preview.setStyleSheet(f"color: {colour}; font-size: 14pt;")

    def refresh(self) -> None:
        settings = self.settings()
        g, u = settings.general, settings.ui
        self.name_edit.setText(str(g.assistant_name))
        index = self.personality.findData(str(g.personality))
        self.personality.setCurrentIndex(index if index >= 0 else 0)
        self.language.setText(str(g.language))
        index = self.accent.findData(str(u.accent))
        self.accent.setCurrentIndex(index if index >= 0 else 0)
        self.font_family.setText(str(u.font_family))
        self.mono_family.setText(str(u.mono_font_family))
        self.font_size.setValue(int(u.base_font_size))
        self.conversation_size.setValue(int(u.conversation_font_size))
        self.animation.setValue(float(u.animation_speed))
        self.core_box.setChecked(bool(u.core_visualiser))
        self.trace_box.setChecked(bool(u.show_tool_trace))
        self.start_min.setChecked(bool(u.start_minimised))
        self.close_tray.setChecked(bool(u.close_to_tray))
        self.opacity.setValue(float(u.window_opacity))
        self.metrics_box.setChecked(bool(u.show_status_metrics))
        self.metrics_ms.setValue(int(u.metrics_refresh_ms))
        self.log_level.setCurrentText(str(g.log_level))
        self.log_to_file.setChecked(bool(g.log_to_file))
        self.log_kept.setValue(int(g.log_files_kept))
        self._preview_accent()

    # ------------------------------------------------------------------
    def _save(self) -> None:
        result = self.controller.save_settings({
            "general": {
                "assistant_name": self.name_edit.text().strip() or "JARVIS",
                "personality": str(self.personality.currentData()),
                "language": self.language.text().strip() or "en-AU",
                "log_level": self.log_level.currentText(),
                "log_to_file": bool(self.log_to_file.isChecked()),
                "log_files_kept": int(self.log_kept.value()),
            },
            "ui": {
                "accent": str(self.accent.currentData()),
                "font_family": self.font_family.text().strip() or "Segoe UI",
                "mono_font_family": self.mono_family.text().strip() or "Cascadia Mono",
                "base_font_size": int(self.font_size.value()),
                "conversation_font_size": int(self.conversation_size.value()),
                "animation_speed": float(self.animation.value()),
                "core_visualiser": bool(self.core_box.isChecked()),
                "show_tool_trace": bool(self.trace_box.isChecked()),
                "start_minimised": bool(self.start_min.isChecked()),
                "close_to_tray": bool(self.close_tray.isChecked()),
                "window_opacity": float(self.opacity.value()),
                "show_status_metrics": bool(self.metrics_box.isChecked()),
                "metrics_refresh_ms": int(self.metrics_ms.value()),
            },
        })
        if result.get("ok"):
            self.set_status("Saved. Colours and sizes apply after a restart; "
                            "behaviour changes apply now.", "ok")
            from ...logging_setup import setup as configure_logging

            settings = self.settings()
            configure_logging(level=settings.general.log_level,
                              to_file=settings.general.log_to_file,
                              files_kept=settings.general.log_files_kept)
        else:
            self.set_status(f"Could not save: {result.get('error')}", "danger")

    def _open_folder(self) -> None:
        folder = str(paths.data_dir())
        try:
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            elif os.uname().sysname == "Darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
            self.set_status(f"Opened {folder}", "ok")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.information(self, "JARVIS folder",
                                    f"Your JARVIS folder is:\n{folder}\n\n"
                                    f"(Could not open it automatically: {exc})")

    def _diagnostics(self) -> None:
        from ...logging_setup import dump_diagnostics

        path = dump_diagnostics()
        QMessageBox.information(self, "Diagnostics",
                                f"Diagnostics written to:\n{path}\n\n"
                                "It contains versions, capability status and "
                                "resource numbers - no conversations, no screenshots.")

    def _run_wizard(self) -> None:
        self.set_status("Ask the main window to show the setup wizard.", "ok")
        window = self.window()
        runner = getattr(window, "run_wizard", None)
        if callable(runner):
            runner()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.refresh()
