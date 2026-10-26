"""Automation: what JARVIS can actually drive on this PC, and how carefully."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..theme import Theme
from .base import Page, card, hint

CAPTURE_BACKENDS = [("auto", "Automatic"), ("mss", "mss (fast, cross-platform)"),
                    ("qt", "Qt screen grab"), ("win32", "Windows GDI"),
                    ("simulated", "Simulated (Demo Mode)")]
OCR_BACKENDS = [("auto", "Automatic"), ("windows", "Windows OCR (WinRT)"),
                ("tesseract", "Tesseract"), ("rapidocr", "RapidOCR"),
                ("none", "Off")]
INPUT_BACKENDS = [("auto", "Automatic"), ("pyautogui", "PyAutoGUI"),
                  ("win32", "Win32 SendInput"), ("xdotool", "xdotool (Linux)")]


class AutomationPage(Page):
    """Capability report plus timing, vision and loop settings."""

    def __init__(self, theme: Theme, controller: Any,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(theme, controller, "Automation",
                         "Every subsystem is probed at start-up. This page shows "
                         "exactly what works on this machine and why.", parent)

        # --- capabilities ----------------------------------------------------
        cap_frame, cap_layout = card("What works on this PC")
        self.cap_table = QTableWidget(0, 4)
        self.cap_table.setHorizontalHeaderLabels(
            ["Subsystem", "Status", "Backend", "Detail"])
        header = self.cap_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.cap_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cap_table.setMaximumHeight(240)
        cap_layout.addWidget(self.cap_table)
        self.raw_summary = QPlainTextEdit()
        self.raw_summary.setObjectName("logView")
        self.raw_summary.setReadOnly(True)
        self.raw_summary.setMaximumHeight(150)
        cap_layout.addWidget(self.raw_summary)
        cap_buttons = QHBoxLayout()
        self.reprobe = QPushButton("Re-probe now")
        self.reprobe.clicked.connect(self._reprobe)
        cap_buttons.addWidget(self.reprobe)
        cap_buttons.addStretch(1)
        cap_layout.addLayout(cap_buttons)
        self.body.addWidget(cap_frame)

        settings = self.settings()

        # --- input -----------------------------------------------------------
        input_frame, input_layout = card("Mouse and keyboard")
        form = QFormLayout()
        a = settings.automation
        self.input_backend = QComboBox()
        for value, label in INPUT_BACKENDS:
            self.input_backend.addItem(label, value)
        index = self.input_backend.findData(str(a.input_backend))
        self.input_backend.setCurrentIndex(index if index >= 0 else 0)
        self.typing_interval = QDoubleSpinBox()
        self.typing_interval.setRange(0.0, 0.5)
        self.typing_interval.setSingleStep(0.005)
        self.typing_interval.setDecimals(3)
        self.typing_interval.setValue(float(a.typing_interval_s))
        self.click_settle = QDoubleSpinBox()
        self.click_settle.setRange(0.0, 3.0)
        self.click_settle.setSingleStep(0.02)
        self.click_settle.setDecimals(2)
        self.click_settle.setValue(float(a.click_settle_s))
        self.launch_wait = QDoubleSpinBox()
        self.launch_wait.setRange(0.0, 30.0)
        self.launch_wait.setSingleStep(0.5)
        self.launch_wait.setDecimals(1)
        self.launch_wait.setValue(float(a.app_launch_wait_s))
        self.ready_wait = QDoubleSpinBox()
        self.ready_wait.setRange(0.0, 60.0)
        self.ready_wait.setSingleStep(1.0)
        self.ready_wait.setDecimals(1)
        self.ready_wait.setSuffix(" s")
        self.ready_wait.setValue(float(a.app_ready_wait_s))
        self.element_wait = QDoubleSpinBox()
        self.element_wait.setRange(0.0, 30.0)
        self.element_wait.setSingleStep(0.5)
        self.element_wait.setDecimals(1)
        self.element_wait.setSuffix(" s")
        self.element_wait.setValue(float(a.element_wait_s))
        self.smooth_mouse = QCheckBox("Move the mouse smoothly (visible, slightly slower)")
        self.smooth_mouse.setChecked(bool(a.move_mouse_smoothly))
        self.mouse_duration = QDoubleSpinBox()
        self.mouse_duration.setRange(0.0, 1.5)
        self.mouse_duration.setSingleStep(0.02)
        self.mouse_duration.setDecimals(2)
        self.mouse_duration.setValue(float(a.mouse_move_duration_s))
        self.adaptive = QCheckBox("Adaptive waits: poll for the change instead of "
                                  "sleeping a fixed time (faster)")
        self.adaptive.setChecked(bool(a.adaptive_waits))
        self.poll_interval = QDoubleSpinBox()
        self.poll_interval.setRange(0.005, 0.5)
        self.poll_interval.setSingleStep(0.005)
        self.poll_interval.setDecimals(3)
        self.poll_interval.setValue(float(a.settle_poll_interval_s))
        self.native_first = QCheckBox("Prefer native app integrations over clicking "
                                      "(Spotify, browsers, Explorer)")
        self.native_first.setChecked(bool(a.prefer_native_integrations))
        form.addRow("Input backend", self.input_backend)
        form.addRow("Keystroke interval (s)", self.typing_interval)
        form.addRow("Settle after a click (s)", self.click_settle)
        form.addRow("Wait after launching (s)", self.launch_wait)
        form.addRow("Wait for app content (s)", self.ready_wait)
        form.addRow("Keep looking for elements (s)", self.element_wait)
        form.addRow("", self.smooth_mouse)
        form.addRow("Mouse move duration (s)", self.mouse_duration)
        form.addRow("", self.adaptive)
        form.addRow("Poll interval (s)", self.poll_interval)
        form.addRow("", self.native_first)
        input_layout.addLayout(form)
        input_layout.addWidget(hint(
            "Verification is always on: JARVIS looks at the screen again after an "
            "action and reports whether it actually worked. That cannot be turned "
            "off, but adaptive waits make the checking cheaper."))
        self.body.addWidget(input_frame)

        # --- vision ----------------------------------------------------------
        vision_frame, vision_layout = card("Screen understanding")
        v = settings.vision
        vform = QFormLayout()
        self.capture_backend = QComboBox()
        for value, label in CAPTURE_BACKENDS:
            self.capture_backend.addItem(label, value)
        index = self.capture_backend.findData(str(v.capture_backend))
        self.capture_backend.setCurrentIndex(index if index >= 0 else 0)
        self.ocr_backend = QComboBox()
        for value, label in OCR_BACKENDS:
            self.ocr_backend.addItem(label, value)
        index = self.ocr_backend.findData(str(v.ocr_backend))
        self.ocr_backend.setCurrentIndex(index if index >= 0 else 0)
        self.max_dim = QSpinBox()
        self.max_dim.setRange(320, 2560)
        self.max_dim.setSingleStep(64)
        self.max_dim.setValue(int(v.max_dimension))
        self.jpeg_quality = QSpinBox()
        self.jpeg_quality.setRange(20, 95)
        self.jpeg_quality.setValue(int(v.jpeg_quality))
        self.use_uia = QCheckBox("Use UI Automation to read controls (much more "
                                 "reliable than OCR)")
        self.use_uia.setChecked(bool(v.use_uia))
        self.uia_max = QSpinBox()
        self.uia_max.setRange(10, 1000)
        self.uia_max.setValue(int(v.uia_max_elements))
        self.skip_ocr = QCheckBox("Skip OCR when UI Automation already found the "
                                  "controls (faster)")
        self.skip_ocr.setChecked(bool(v.skip_ocr_when_uia_rich))
        self.uia_rich = QSpinBox()
        self.uia_rich.setRange(1, 500)
        self.uia_rich.setValue(int(v.uia_rich_threshold))
        self.cache_ttl = QDoubleSpinBox()
        self.cache_ttl.setRange(0.0, 60.0)
        self.cache_ttl.setSingleStep(0.1)
        self.cache_ttl.setDecimals(2)
        self.cache_ttl.setValue(float(v.cache_ttl_s))
        self.element_ttl = QDoubleSpinBox()
        self.element_ttl.setRange(0.0, 30.0)
        self.element_ttl.setSingleStep(0.1)
        self.element_ttl.setDecimals(2)
        self.element_ttl.setValue(float(v.element_cache_ttl_s))
        self.observe_interval = QSpinBox()
        self.observe_interval.setRange(250, 30000)
        self.observe_interval.setSingleStep(100)
        self.observe_interval.setSuffix(" ms")
        self.observe_interval.setValue(int(v.observation_interval_ms))
        self.observe_after = QCheckBox("Look at the screen again after each action")
        self.observe_after.setChecked(bool(v.observe_after_action))
        self.mask_passwords = QCheckBox("Mask password fields in screenshots "
                                        "(locked on)")
        self.mask_passwords.setChecked(True)
        self.mask_passwords.setEnabled(False)
        vform.addRow("Capture backend", self.capture_backend)
        vform.addRow("OCR backend", self.ocr_backend)
        vform.addRow("Max image size (px)", self.max_dim)
        vform.addRow("Image quality", self.jpeg_quality)
        vform.addRow("", self.use_uia)
        vform.addRow("Max controls per window", self.uia_max)
        vform.addRow("", self.skip_ocr)
        vform.addRow("'Rich' UIA threshold", self.uia_rich)
        vform.addRow("Screenshot cache (s)", self.cache_ttl)
        vform.addRow("Control-tree cache (s)", self.element_ttl)
        vform.addRow("Background observation every", self.observe_interval)
        vform.addRow("", self.observe_after)
        vform.addRow("", self.mask_passwords)
        vision_layout.addLayout(vform)
        self.body.addWidget(vision_frame)

        # --- limits ----------------------------------------------------------
        limits_frame, limits_layout = card("Loop limits and folders")
        lform = QFormLayout()
        self.max_steps = QSpinBox()
        self.max_steps.setRange(1, 200)
        self.max_steps.setValue(int(a.max_steps_per_task))
        self.max_identical = QSpinBox()
        self.max_identical.setRange(1, 10)
        self.max_identical.setValue(int(a.max_identical_actions))
        self.max_failures = QSpinBox()
        self.max_failures.setRange(1, 20)
        self.max_failures.setValue(int(a.max_consecutive_failures))
        self.max_wait = QDoubleSpinBox()
        self.max_wait.setRange(1.0, 600.0)
        self.max_wait.setSuffix(" s")
        self.max_wait.setValue(float(a.max_wait_s))
        self.writable_roots = QLineEdit(", ".join(a.writable_roots or []))
        self.writable_roots.setPlaceholderText(
            "Extra folders JARVIS may write to without asking (comma separated)")
        self.devtools = QCheckBox("Drive browsers through DevTools (needs the "
                                  "browser started with a debug port)")
        self.devtools.setChecked(bool(a.browser_devtools))
        self.devtools_port = QSpinBox()
        self.devtools_port.setRange(1024, 65535)
        self.devtools_port.setValue(int(a.browser_devtools_port))
        lform.addRow("Max steps per task", self.max_steps)
        lform.addRow("Stop after identical actions", self.max_identical)
        lform.addRow("Stop after consecutive failures", self.max_failures)
        lform.addRow("Max wait for a change", self.max_wait)
        lform.addRow("Extra writable folders", self.writable_roots)
        lform.addRow("", self.devtools)
        lform.addRow("DevTools port", self.devtools_port)
        limits_layout.addLayout(lform)
        limits_layout.addWidget(hint(
            "By default JARVIS may only write inside your own folders (Desktop, "
            "Documents, Downloads, Pictures, Music, Videos, Temp). System folders "
            "are refused no matter what is entered here."))
        self.body.addWidget(limits_frame)

        self.add_footer_button("Save automation settings", self._save, "primary")
        self.add_footer_button("Reload", self.refresh, "ghost")

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self._fill_capabilities()
        settings = self.settings()
        a, v = settings.automation, settings.vision
        for combo, value in ((self.input_backend, a.input_backend),
                             (self.capture_backend, v.capture_backend),
                             (self.ocr_backend, v.ocr_backend)):
            index = combo.findData(str(value))
            combo.setCurrentIndex(index if index >= 0 else 0)
        self.typing_interval.setValue(float(a.typing_interval_s))
        self.click_settle.setValue(float(a.click_settle_s))
        self.launch_wait.setValue(float(a.app_launch_wait_s))
        self.ready_wait.setValue(float(a.app_ready_wait_s))
        self.element_wait.setValue(float(a.element_wait_s))
        self.smooth_mouse.setChecked(bool(a.move_mouse_smoothly))
        self.mouse_duration.setValue(float(a.mouse_move_duration_s))
        self.adaptive.setChecked(bool(a.adaptive_waits))
        self.poll_interval.setValue(float(a.settle_poll_interval_s))
        self.native_first.setChecked(bool(a.prefer_native_integrations))
        self.max_dim.setValue(int(v.max_dimension))
        self.jpeg_quality.setValue(int(v.jpeg_quality))
        self.use_uia.setChecked(bool(v.use_uia))
        self.uia_max.setValue(int(v.uia_max_elements))
        self.skip_ocr.setChecked(bool(v.skip_ocr_when_uia_rich))
        self.uia_rich.setValue(int(v.uia_rich_threshold))
        self.cache_ttl.setValue(float(v.cache_ttl_s))
        self.element_ttl.setValue(float(v.element_cache_ttl_s))
        self.observe_interval.setValue(int(v.observation_interval_ms))
        self.observe_after.setChecked(bool(v.observe_after_action))
        self.max_steps.setValue(int(a.max_steps_per_task))
        self.max_identical.setValue(int(a.max_identical_actions))
        self.max_failures.setValue(int(a.max_consecutive_failures))
        self.max_wait.setValue(float(a.max_wait_s))
        self.writable_roots.setText(", ".join(a.writable_roots or []))
        self.devtools.setChecked(bool(a.browser_devtools))
        self.devtools_port.setValue(int(a.browser_devtools_port))

    def _fill_capabilities(self) -> None:
        runtime = self.controller.runtime
        caps = runtime.platform.capability_list()
        table = self.cap_table
        table.setRowCount(0)
        for cap in caps:
            if cap.name == "simulation" and not cap.available:
                continue
            row = table.rowCount()
            table.insertRow(row)
            name_item = QTableWidgetItem(cap.name.replace("_", " ").title())
            state_item = QTableWidgetItem("OK" if cap.available else "MISSING")
            state_item.setForeground(QColor(self.theme.colour(
                "success" if cap.available else "warning")))
            backend_item = QTableWidgetItem(str(cap.backend or "-"))
            detail_item = QTableWidgetItem(str(cap.detail or ""))
            for item in (name_item, state_item, backend_item, detail_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, name_item)
            table.setItem(row, 1, state_item)
            table.setItem(row, 2, backend_item)
            table.setItem(row, 3, detail_item)
        self.raw_summary.setPlainText(self.controller.platform_summary())

    def _reprobe(self) -> None:
        runtime = self.controller.runtime
        runtime.registry.invalidate_availability()
        if runtime.vision is not None:
            runtime.vision.invalidate()
        self._fill_capabilities()
        unavailable = runtime.registry.unavailable_report()
        self.set_status(
            "Re-probed. " + (f"{len(unavailable)} tool(s) unavailable here."
                             if unavailable else "Every tool is available."),
            "ok" if not unavailable else "warn")

    # ------------------------------------------------------------------
    def _save(self) -> None:
        result = self.controller.save_settings({
            "automation": {
                "input_backend": str(self.input_backend.currentData()),
                "typing_interval_s": float(self.typing_interval.value()),
                "click_settle_s": float(self.click_settle.value()),
                "app_launch_wait_s": float(self.launch_wait.value()),
                "app_ready_wait_s": float(self.ready_wait.value()),
                "element_wait_s": float(self.element_wait.value()),
                "move_mouse_smoothly": bool(self.smooth_mouse.isChecked()),
                "mouse_move_duration_s": float(self.mouse_duration.value()),
                "adaptive_waits": bool(self.adaptive.isChecked()),
                "settle_poll_interval_s": float(self.poll_interval.value()),
                "prefer_native_integrations": bool(self.native_first.isChecked()),
                "max_steps_per_task": int(self.max_steps.value()),
                "max_identical_actions": int(self.max_identical.value()),
                "max_consecutive_failures": int(self.max_failures.value()),
                "max_wait_s": float(self.max_wait.value()),
                "writable_roots": [p.strip() for p in
                                   self.writable_roots.text().split(",") if p.strip()],
                "browser_devtools": bool(self.devtools.isChecked()),
                "browser_devtools_port": int(self.devtools_port.value()),
            },
            "vision": {
                "capture_backend": str(self.capture_backend.currentData()),
                "ocr_backend": str(self.ocr_backend.currentData()),
                "max_dimension": int(self.max_dim.value()),
                "jpeg_quality": int(self.jpeg_quality.value()),
                "use_uia": bool(self.use_uia.isChecked()),
                "uia_max_elements": int(self.uia_max.value()),
                "skip_ocr_when_uia_rich": bool(self.skip_ocr.isChecked()),
                "uia_rich_threshold": int(self.uia_rich.value()),
                "cache_ttl_s": float(self.cache_ttl.value()),
                "element_cache_ttl_s": float(self.element_ttl.value()),
                "observation_interval_ms": int(self.observe_interval.value()),
                "observe_after_action": bool(self.observe_after.isChecked()),
            },
        })
        if result.get("ok"):
            self.controller.runtime.registry.invalidate_availability()
            if self.controller.runtime.vision is not None:
                self.controller.runtime.vision.invalidate()
            self.set_status("Automation settings saved.", "ok")
            self._fill_capabilities()
        else:
            self.set_status(f"Could not save: {result.get('error')}", "danger")

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.refresh()
