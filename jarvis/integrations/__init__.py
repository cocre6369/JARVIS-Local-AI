"""Native application integrations.

JARVIS prefers a real integration over blind mouse automation whenever one
exists (specification section 8), because it is faster and far more reliable:

``smtc``      Windows System Media Transport Controls - the same API the media
              keys use.  Reports what is playing in *any* media app (Spotify,
              YouTube in a browser, Groove) and can play/pause/skip it.
``spotify``   SMTC first, then Spotify's own window through UI Automation, then
              media keys as a last resort.
``browser``   Address-bar driven navigation plus a full-screen toggle, with
              Chrome DevTools Protocol support when the user opts in.
``explorer``  File Explorer navigation by address bar.

Every integration reports honestly which path it used, and every one of them
still goes through the same permission layer as a raw mouse click.
"""

from .smtc import MediaSession, SmtcMedia, create_smtc  # noqa: F401
from .spotify import SpotifyIntegration  # noqa: F401
from .browser import BrowserIntegration  # noqa: F401
from .explorer import ExplorerIntegration  # noqa: F401

__all__ = ["MediaSession", "SmtcMedia", "create_smtc", "SpotifyIntegration",
           "BrowserIntegration", "ExplorerIntegration"]
