# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for the JARVIS Windows executable.

Build from the repository root::

    python -m PyInstaller packaging/jarvis.spec --noconfirm

Design notes
------------
* **onedir**, not onefile.  A single 300 MB self-extracting blob takes seconds to
  start every launch and trips antivirus heuristics; a folder plus an installer
  starts in under a second.  The installer (``installer.iss``) makes the folder
  invisible to the user.
* Every JARVIS module is pulled in explicitly.  The codebase imports optional
  backends *lazily* (behind capability probes), and PyInstaller's static scan
  occasionally misses a name inside a try/except deep in a function, so the
  whole package is collected instead of guessed at.
* Console=False: JARVIS is a windowed app.  ``jarvis.exe`` therefore has no
  console; logs live in ``%LOCALAPPDATA%\\JARVIS\\logs``.
"""

import os
import sys
from pathlib import Path

HERE = Path(SPECPATH).resolve()
ROOT = HERE.parent
ASSETS = HERE / "assets"
ICON = ASSETS / "jarvis.ico"

sys.path.insert(0, str(ROOT))

# --- modules PyInstaller cannot see because they are imported lazily --------
HIDDEN_IMPORTS = [
    # the application itself
    "jarvis", "jarvis.app", "jarvis.cli", "jarvis.config", "jarvis.events",
    "jarvis.logging_setup", "jarvis.paths",
    "jarvis.core", "jarvis.core.agent", "jarvis.core.broker", "jarvis.core.intents",
    "jarvis.core.planner", "jarvis.core.prompts", "jarvis.core.runtime",
    "jarvis.core.session",
    "jarvis.integrations", "jarvis.integrations.browser",
    "jarvis.integrations.explorer", "jarvis.integrations.smtc",
    "jarvis.integrations.spotify",
    "jarvis.llm", "jarvis.llm.ollama_client", "jarvis.llm.protocol",
    "jarvis.llm.router",
    "jarvis.memory", "jarvis.memory.store",
    "jarvis.platform", "jarvis.platform.base", "jarvis.platform.screen_backends",
    "jarvis.platform.simulated",
    "jarvis.security", "jarvis.security.audit", "jarvis.security.guard",
    "jarvis.security.netguard", "jarvis.security.policy", "jarvis.security.redaction",
    "jarvis.speech", "jarvis.speech.audio_io", "jarvis.speech.service",
    "jarvis.speech.stt", "jarvis.speech.tts", "jarvis.speech.wake",
    "jarvis.tools", "jarvis.tools.base", "jarvis.tools.registry",
    "jarvis.tools.timing",
    "jarvis.ui", "jarvis.ui.controller", "jarvis.ui.main_window",
    "jarvis.ui.render", "jarvis.ui.theme", "jarvis.ui.visual_core",
    "jarvis.ui.wizard", "jarvis.ui.pages", "jarvis.ui.widgets",
    # optional but shipped when installed: local speech / vision / automation
    "faster_whisper", "sounddevice", "soundfile", "webrtcvad", "numpy",
    "PIL", "mss", "pyautogui", "pygetwindow", "win32api", "win32gui",
    "win32con", "win32process", "pythoncom", "comtypes", "uiautomation",
    "piper", "pytesseract", "rapidfuzz", "packaging",
]

a = Analysis(
    [str(ROOT / "jarvis" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # never shipped: JARVIS has no cloud AI and no notebooks
        "tkinter", "_tkinter", "matplotlib", "scipy", "pandas", "IPython",
        "notebook", "pytest", "torch", "tensorflow", "onnxruntime",
    ],
    noarchive=False,
)

# Every JARVIS module, whatever the scan decided.
import PyInstaller.utils.hooks as _hooks

for module in ("jarvis",):
    a.hiddenimports += [name for name in _hooks.collect_submodules(module)
                        if name not in a.hiddenimports]

# PyInstaller 6: PYZ takes only the analysed modules (bytecode encryption
# and the old zipped_data/cipher arguments were removed).
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JARVIS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX-compressed exes trip Windows Defender
    console=False,                  # windowed app; logs go to the data dir
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JARVIS",
)
