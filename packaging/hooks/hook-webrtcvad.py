"""Local PyInstaller hook for ``webrtcvad``.

The pyinstaller-hooks-contrib hook for this module looks the library up via
``importlib.metadata.version("webrtcvad")``.  JARVIS installs the maintained
``webrtcvad-wheels`` distribution instead (prebuilt py3.11 wheels for
Windows); its metadata is registered as ``webrtcvad_wheels``, so the contrib
lookup raises ``PackageNotFoundError`` and aborts the whole freeze.

Hooks found on ``hookspath`` take priority over the contrib hooks, and the
module itself is a plain top-level extension module (``webrtcvad.pyd``) that
Analysis collects automatically - so an empty hook is all that is needed.
"""

hiddenimports: list = []
binaries: list = []
datas: list = []
