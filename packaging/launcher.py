"""PyInstaller entry point for the frozen JARVIS executable.

The spec used to point at ``jarvis/__main__.py``, but PyInstaller executes the
entry script as a top-level ``__main__`` *without* a parent package, so its
``from .cli import main`` dies with
``ImportError: attempted relative import with no known parent package``.
This launcher uses absolute imports only; ``python -m jarvis`` keeps using the
original module unchanged.
"""

import sys

from jarvis.cli import main

if __name__ == "__main__":
    sys.exit(main())
