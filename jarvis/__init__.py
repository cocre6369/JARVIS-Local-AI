"""JARVIS - a fully local, autonomous AI desktop assistant.

Version history
---------------
1.0.5  Stop acting on substitutes: element matching needs a real match before
       JARVIS clicks or types, similar-task replay needs a much closer request,
       and the model is explicitly forbidden from clicking the nearest file or
       icon when the requested app/control is missing.
1.0.4  Fix a start-up crash: the tray icon fallback used a Qt enum that does
       not exist (StandardPixelMetric instead of StandardPixmap) and is now
       crash-proof; the icon ships inside the frozen bundle.
1.0.3  Real-app vision: OCR fallback now fires when UI Automation exposes only
       window chrome (Spotify and other Electron apps), with OCR coordinates
       mapped to true screen positions.  Stumbled tasks are never remembered
       as replay shortcuts.  Closing the window minimises to the tray (locked
       on) and JARVIS keeps working; a floating bottom-right orb shows the
       agent state in colour with a fold-out mini chat.
1.0.2  No more flashing cmd windows: every child process is spawned console-free.
       Apps must finish LOADING before JARVIS acts: launch waits for real
       content, and element lookups keep re-looking while a UI loads.
1.0.1  Fix a launch crash (missing Qt imports on the Voice page) and the
       "app keeps opening and closing" bug: launch_application now detects
       already-running apps by process as well as window (tray apps included)
       and never starts a duplicate single-instance app.
1.0.0  First complete build: Ollama reasoning core, tool-calling agent loop,
       Windows desktop automation, vision (screenshot + OCR + UI Automation),
       local speech-to-text, local neural text-to-speech, SQLite memory,
       a hard-enforced security/permission layer and a resource guard that
       keeps JARVIS inside a 12 GB RAM / 8 GB VRAM budget.
"""

__version__ = "1.0.5"
__app_name__ = "JARVIS"
__author__ = "JARVIS Project"

#: Every release is local-only.  This flag is asserted by the network guard in
#: :mod:`jarvis.security.netguard` and by the test-suite.
LOCAL_ONLY = True
