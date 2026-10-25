"""File Explorer integration.

Opening a folder is trivial; the useful part is *navigating an already-open*
Explorer window and reading back what it now shows, so the model can verify
"Find my latest Minecraft screenshot" style tasks without guessing.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

EXPLORER_PROCESSES = ("explorer.exe", "explorer")


class ExplorerIntegration:
    name = "explorer"

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    def windows(self) -> List[Any]:
        out = []
        try:
            for w in self.ctx.platform.windows.windows() or []:
                cls = (w.class_name or "").lower()
                proc = (w.process_name or "").lower()
                title = (w.title or "").lower()
                if cls in ("cabinetwclass", "explorerwindow") or (
                        proc in EXPLORER_PROCESSES and title
                        and "taskbar" not in title):
                    out.append(w)
        except Exception:  # noqa: BLE001
            pass
        return out

    def active(self) -> Optional[Any]:
        wins = self.windows()
        if not wins:
            return None
        try:
            active = self.ctx.platform.windows.active()
            for w in wins:
                if active is not None and w.handle == active.handle:
                    return w
        except Exception:  # noqa: BLE001
            pass
        return wins[0]

    # ------------------------------------------------------------------
    def open(self, path: str) -> Dict[str, Any]:
        target = os.path.expandvars(str(path or "").strip())
        if not target:
            return {"ok": False, "error": "No path given."}
        result = self.ctx.platform.apps.launch(target) or {}
        if not result.get("ok"):
            return result
        deadline = time.time() + 5.0
        win = None
        while time.time() < deadline:
            for w in self.windows():
                if os.path.basename(target).lower() in (w.title or "").lower() \
                        or (w.title or "").lower() in os.path.basename(target).lower():
                    win = w
                    break
            if win:
                break
            time.sleep(0.3)
        return {"ok": True, "path": target,
                "window": win.as_dict() if win else None,
                "verified": win is not None}

    def navigate(self, path: str) -> Dict[str, Any]:
        """Type into the address bar of the front Explorer window and verify."""
        win = self.active()
        if win is None:
            return self.open(path)
        self.ctx.platform.windows.focus(win)
        time.sleep(0.3)
        self.ctx.platform.input.press(["ctrl", "l"])     # focus the address bar
        time.sleep(0.25)
        self.ctx.platform.input.press(["ctrl", "a"])
        self.ctx.platform.input.type_text(
            str(path), interval=float(self.ctx.settings.automation.typing_interval_s))
        self.ctx.platform.input.press(["enter"])
        time.sleep(1.1)
        after = self.active()
        wanted = os.path.basename(str(path).rstrip("\\/")).lower()
        title = (after.title or "").lower() if after else ""
        verified = bool(wanted) and (wanted in title or title in wanted)
        return {"ok": True, "path": str(path),
                "window": after.as_dict() if after else None,
                "window_title": after.title if after else "",
                "verified": verified,
                "note": "" if verified else
                "The address bar was submitted but the window title does not "
                "confirm the location; observe_screen to check."}

    def search(self, query: str) -> Dict[str, Any]:
        """Use Explorer's own search box (Ctrl+E) in the current window."""
        win = self.active()
        if win is None:
            return {"ok": False, "error": "No File Explorer window is open."}
        self.ctx.platform.windows.focus(win)
        time.sleep(0.3)
        self.ctx.platform.input.press(["ctrl", "e"])
        time.sleep(0.25)
        self.ctx.platform.input.type_text(
            str(query), interval=float(self.ctx.settings.automation.typing_interval_s))
        self.ctx.platform.input.press(["enter"])
        time.sleep(1.5)
        items = self.items()
        return {"ok": True, "query": str(query), "items": items[:40],
                "count": len(items)}

    def items(self) -> List[Dict[str, Any]]:
        """Read the file list currently shown, via UI Automation."""
        win = self.active()
        if win is None:
            return []
        try:
            elements = self.ctx.platform.uia.elements(root=win, max_count=400) or []
        except Exception as exc:  # noqa: BLE001
            log.debug("Explorer UIA read failed: %s", exc)
            return []
        out: List[Dict[str, Any]] = []
        for el in elements:
            if el.control_type in ("ListItem", "DataItem", "Text", "Hyperlink"):
                name = (el.name or "").strip()
                if not name or len(name) < 2:
                    continue
                out.append({"name": name, "type": el.control_type,
                            "x": el.center.x, "y": el.center.y,
                            "automation_id": el.automation_id})
        return out

    def select(self, name: str) -> Dict[str, Any]:
        """Select (single click) an item in the current Explorer view."""
        win = self.active()
        if win is None:
            return {"ok": False, "error": "No File Explorer window is open."}
        items = self.items()
        low = name.lower()
        match = next((i for i in items if i["name"].lower() == low), None) or \
            next((i for i in items if low in i["name"].lower()), None)
        if match is None:
            return {"ok": False, "error": f"'{name}' is not visible in this folder.",
                    "items": [i["name"] for i in items[:30]]}
        self.ctx.platform.input.click(int(match["x"]), int(match["y"]), "left", 1)
        time.sleep(0.25)
        return {"ok": True, "selected": match}

    def open_item(self, name: str) -> Dict[str, Any]:
        selected = self.select(name)
        if not selected.get("ok"):
            return selected
        self.ctx.platform.input.press(["enter"])
        time.sleep(0.8)
        return {"ok": True, "opened": name, "suggest_observe": True}
