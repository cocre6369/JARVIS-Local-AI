"""CI gate: no raw subprocess calls outside ``jarvis/_proc.py``.

JARVIS.exe is a windowed app; every console child spawned with plain
``subprocess`` gets its own visible cmd window that flashes on screen (the
"cmd prompt keeps opening and closing" bug).  ``jarvis/_proc.py`` adds
CREATE_NO_WINDOW + hidden STARTUPINFO on Windows.  This gate fails the build
the moment a raw ``subprocess.run/Popen/call/check_output`` sneaks back in.
"""

from __future__ import annotations

import os
import re
import sys

ALLOWED = os.path.join("jarvis", "_proc.py")
PATTERN = re.compile(r"\bsubprocess\.(run|Popen|call|check_output|check_call)\(")


def main() -> int:
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "jarvis")
    bad = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, os.path.dirname(root))
            if rel.replace("/", os.sep) == ALLOWED:
                continue
            with open(full, "r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    if PATTERN.search(line):
                        bad.append(f"{rel}:{lineno}: {line.strip()}")
    if bad:
        print("Raw subprocess calls found - route them through jarvis/_proc.py "
              "(run/popen/call/check_output) so no console window can flash:")
        for item in bad:
            print("  " + item)
        return 1
    print("subprocess gate OK: every child process is console-free")
    return 0


if __name__ == "__main__":
    sys.exit(main())
