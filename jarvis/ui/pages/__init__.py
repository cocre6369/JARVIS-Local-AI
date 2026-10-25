"""The eight pages of the JARVIS interface."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtWidgets import QWidget

from ..theme import Theme
from .automation import AutomationPage
from .dashboard import DashboardPage
from .general import GeneralPage
from .history import HistoryPage
from .memory import MemoryPage
from .models import ModelsPage
from .permissions import PermissionsPage
from .voice import VoicePage

#: Sidebar order matches the design document.
PAGE_ORDER = ("dashboard", "history", "memory", "models", "voice", "permissions",
              "automation", "general")


def build_pages(theme: Theme, controller: Any,
                parent: Optional[QWidget] = None) -> Dict[str, Any]:
    """Construct every page once.  Keys match :data:`PAGE_ORDER`."""
    return {
        "dashboard": DashboardPage(theme, controller, parent),
        "history": HistoryPage(theme, controller, parent),
        "memory": MemoryPage(theme, controller, parent),
        "models": ModelsPage(theme, controller, parent),
        "voice": VoicePage(theme, controller, parent),
        "permissions": PermissionsPage(theme, controller, parent),
        "automation": AutomationPage(theme, controller, parent),
        "general": GeneralPage(theme, controller, parent),
    }


def page_titles() -> List[Dict[str, str]]:
    from ..theme import nav_items

    return nav_items()


__all__ = ["AutomationPage", "DashboardPage", "GeneralPage", "HistoryPage",
           "MemoryPage", "ModelsPage", "PAGE_ORDER", "PermissionsPage", "VoicePage",
           "build_pages", "page_titles"]
