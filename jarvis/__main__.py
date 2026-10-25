"""``python -m jarvis`` - the same entry point the frozen executable uses."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
