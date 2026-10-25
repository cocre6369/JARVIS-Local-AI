"""JARVIS - a fully local, autonomous AI desktop assistant.

Version history
---------------
1.0.0  First complete build: Ollama reasoning core, tool-calling agent loop,
       Windows desktop automation, vision (screenshot + OCR + UI Automation),
       local speech-to-text, local neural text-to-speech, SQLite memory,
       a hard-enforced security/permission layer and a resource guard that
       keeps JARVIS inside a 12 GB RAM / 8 GB VRAM budget.
"""

__version__ = "1.0.0"
__app_name__ = "JARVIS"
__author__ = "JARVIS Project"

#: Every release is local-only.  This flag is asserted by the network guard in
#: :mod:`jarvis.security.netguard` and by the test-suite.
LOCAL_ONLY = True
