"""Application discovery and launching.

"Open Spotify" only works if JARVIS can actually find Spotify.  On Windows the
resolver combines four sources, all of them read-only:

1. Start Menu shortcuts (``*.lnk`` parsed with a small, dependency-free Shell
   Link reader - no COM, no pywin32).
2. ``Get-StartApps`` (PowerShell) so UWP/Store apps resolve to their
   ``shell:AppsFolder`` AUMID.
3. ``HKLM/HKCU ... App Paths`` and the ``Uninstall`` key (Display Name ->
   InstallLocation/DisplayIcon).  Reading the registry is allowed; *writing* to
   it is prohibited by the security layer.
4. ``PATH`` plus the usual install directories.

Results are cached for the lifetime of the process and fuzzy-matched with
:class:`difflib.SequenceMatcher`, so "chrom", "google chrome browser" and
"Chrome" all resolve to the same executable.
"""

from __future__ import annotations

import ctypes
import difflib
import logging
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

CACHE_TTL_S = 600.0


@dataclass
class AppCandidate:
    name: str
    target: str                 # exe path, URL, or shell:AppsFolder AUMID
    kind: str = "exe"           # exe | uwp | url | folder | desktop-entry
    source: str = ""            # start-menu | registry | path | powershell
    icon: str = ""
    publisher: str = ""
    aliases: List[str] = field(default_factory=list)

    def score(self, query: str) -> float:
        q = _norm(query)
        n = _norm(self.name)
        if not q:
            return 0.0
        if q == n:
            return 1.0
        if n.startswith(q):
            return 0.92 + 0.05 * (len(q) / max(1, len(n)))
        if q in n:
            return 0.80
        for alias in self.aliases:
            a = _norm(alias)
            if a == q:
                return 0.98
            if q in a or a in q:
                return 0.85
        stem = _norm(os.path.splitext(os.path.basename(self.target))[0])
        if stem and (q == stem or q in stem or stem in q):
            return 0.88
        ratio = difflib.SequenceMatcher(None, q, n).ratio()
        # token overlap boosts multi-word queries ("adobe after effects")
        qt = set(re.split(r"[\s\-_.]+", q))
        nt = set(re.split(r"[\s\-_.]+", n))
        if qt and nt:
            overlap = len(qt & nt) / len(qt)
            ratio = max(ratio, overlap * 0.95)
        return round(ratio, 4)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "target": self.target, "kind": self.kind,
                "source": self.source, "aliases": list(self.aliases)}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# ---------------------------------------------------------------------------
# .lnk (Shell Link) reader - dependency free
# ---------------------------------------------------------------------------
def read_lnk_target(path: str) -> str:
    """Extract the local target path from a Windows shortcut file.

    Implements the minimum of MS-SHLLINK needed to read ``LocalBasePath`` from
    the ``LinkInfo`` structure, plus the ``CommonPathSuffix`` join.  Returns ""
    when the shortcut has no local target (UWP shortcuts store an AUMID in a
    property store instead - those come from ``Get-StartApps``).
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(65536)
    except OSError:
        return ""
    if len(data) < 76:
        return ""
    header_size, clsid = struct.unpack_from("<I16s", data, 0)
    if header_size != 0x4C or clsid != bytes.fromhex("0002140100000000C000000000000046"):
        return ""
    flags = struct.unpack_from("<I", data, 20)[0]
    HAS_LINK_TARGET_ID_LIST = 0x01
    HAS_LINK_INFO = 0x02
    offset = 76
    if flags & HAS_LINK_TARGET_ID_LIST:
        if offset + 2 > len(data):
            return ""
        id_list_size = struct.unpack_from("<H", data, offset)[0]
        offset += 2 + id_list_size
    if not (flags & HAS_LINK_INFO):
        return ""
    if offset + 4 > len(data):
        return ""
    info_start = offset
    info_size = struct.unpack_from("<I", data, offset)[0]
    if info_size < 28 or info_start + info_size > len(data):
        return ""
    header_size_info = struct.unpack_from("<I", data, info_start + 4)[0]
    local_base_path_offset = struct.unpack_from("<I", data, info_start + 16)[0]
    common_path_suffix_offset = struct.unpack_from("<I", data, info_start + 24)[0]
    volume_id_offset = struct.unpack_from("<I", data, info_start + 8)[0]
    data_offset = struct.unpack_from("<I", data, info_start + 20)[0]

    def _cstring(pos: int) -> str:
        if pos <= 0 or pos >= len(data):
            return ""
        end = data.find(b"\x00", pos)
        if end < 0:
            end = len(data)
        raw = data[pos:end]
        for enc in ("mbcs", "cp1252", "utf-8", "latin-1"):
            try:
                return raw.decode(enc)
            except Exception:  # noqa: BLE001
                continue
        return raw.decode("latin-1", "replace")

    base = ""
    if header_size_info >= 0x24 and local_base_path_offset:
        base = _cstring(info_start + local_base_path_offset)
    elif data_offset:
        base = _cstring(info_start + data_offset)
    suffix = ""
    if common_path_suffix_offset:
        suffix = _cstring(info_start + common_path_suffix_offset)
    if base and suffix:
        base = os.path.join(base, suffix)
    if not base and volume_id_offset:
        base = _cstring(info_start + volume_id_offset)
    return base.strip()


# ---------------------------------------------------------------------------
# Registry (read-only) helpers
# ---------------------------------------------------------------------------
if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import ctypes.wintypes as _wt

    _advapi32 = ctypes.windll.advapi32  # type: ignore[attr-defined]
    _HKEY_LOCAL_MACHINE = 0x80000002
    _HKEY_CURRENT_USER = 0x80000001
    _KEY_READ = 0x20019
    _KEY_WOW64_64KEY = 0x0100
    _KEY_WOW64_32KEY = 0x0200
    _REG_SZ = 1
    _REG_EXPAND_SZ = 2


def _reg_open(hive: int, subkey: str, wow64: int = 0) -> Optional[Any]:
    try:
        handle = _wt.HKEY()
        rc = _advapi32.RegOpenKeyExW(
            _wt.HKEY(hive), subkey, 0, _KEY_READ | wow64, ctypes.byref(handle))
        return handle if rc == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _reg_enum_keys(handle: Any) -> List[str]:
    out: List[str] = []
    index = 0
    while True:
        try:
            buf = ctypes.create_unicode_buffer(512)
            length = _wt.DWORD(512)
            rc = _advapi32.RegEnumKeyExW(handle, index, buf, ctypes.byref(length),
                                         None, None, None, None)
        except Exception:  # noqa: BLE001
            break
        if rc != 0:
            break
        out.append(buf.value)
        index += 1
        if index > 4000:
            break
    return out


def _reg_get_value(handle: Any, name: str) -> str:
    try:
        size = _wt.DWORD(0)
        _advapi32.RegQueryValueExW(handle, name, None, None, None, ctypes.byref(size))
        if size.value == 0 or size.value > 8192:
            return ""
        buf = ctypes.create_unicode_buffer(size.value + 2)
        vtype = _wt.DWORD(0)
        rc = _advapi32.RegQueryValueExW(handle, name, None, ctypes.byref(vtype),
                                        buf, ctypes.byref(size))
        if rc != 0 or vtype.value not in (_REG_SZ, _REG_EXPAND_SZ):
            return ""
        value = buf.value
        return os.path.expandvars(value) if "%" in value else value
    except Exception:  # noqa: BLE001
        return ""


def _reg_close(handle: Any) -> None:
    try:
        _advapi32.RegCloseKey(handle)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
class WindowsApps:
    name = "windows"

    def __init__(self, use_powershell: bool = True) -> None:
        if os.name != "nt":
            raise OSError("WindowsApps requires Windows")
        self.use_powershell = use_powershell
        self._cache: List[AppCandidate] = []
        self._cache_ts: float = 0.0

    # ------------------------------------------------------------------
    def _start_menu_dirs(self) -> List[str]:
        out = []
        for env in ("APPDATA", "PROGRAMDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            out.append(os.path.join(base, "Microsoft", "Windows", "Start Menu",
                                    "Programs"))
        desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
        public_desktop = os.path.join(os.environ.get("PUBLIC", ""), "Desktop")
        out.extend([desktop, public_desktop])
        return [d for d in out if d and os.path.isdir(d)]

    def _from_shortcuts(self) -> List[AppCandidate]:
        out: List[AppCandidate] = []
        for root in self._start_menu_dirs():
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    if not fn.lower().endswith(".lnk"):
                        continue
                    full = os.path.join(dirpath, fn)
                    name = fn[:-4]
                    target = read_lnk_target(full)
                    if not target:
                        continue
                    if target.lower().endswith((".exe", ".bat", ".cmd", ".msc",
                                               ".cpl", ".url")):
                        out.append(AppCandidate(
                            name=name, target=target, kind="exe",
                            source="start-menu",
                            aliases=_aliases_for(name)))
        return out

    def _from_registry(self) -> List[AppCandidate]:
        out: List[AppCandidate] = []
        if os.name != "nt":
            return out
        # App Paths
        for hive, base in ((_HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
                           (_HKEY_CURRENT_USER,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")):
            for wow in (_KEY_WOW64_64KEY, _KEY_WOW64_32KEY):
                handle = _reg_open(hive, base, wow)
                if handle is None:
                    continue
                try:
                    for key in _reg_enum_keys(handle):
                        sub = _reg_open(hive, f"{base}\\{key}", wow)
                        if sub is None:
                            continue
                        try:
                            path = _reg_get_value(sub, None) or _reg_get_value(sub, "")
                            if not path:
                                # default value: name=None
                                path = _reg_default(sub)
                            if path and os.path.exists(path):
                                out.append(AppCandidate(
                                    name=os.path.splitext(key)[0], target=path,
                                    kind="exe", source="registry-app-paths",
                                    aliases=_aliases_for(key)))
                        finally:
                            _reg_close(sub)
                finally:
                    _reg_close(handle)
        # Uninstall entries (read-only: DisplayName + InstallLocation/DisplayIcon)
        uninst_bases = [
            (_HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (_HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (_HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, base in uninst_bases:
            for wow in (_KEY_WOW64_64KEY, _KEY_WOW64_32KEY):
                handle = _reg_open(hive, base, wow)
                if handle is None:
                    continue
                try:
                    for key in _reg_enum_keys(handle):
                        sub = _reg_open(hive, f"{base}\\{key}", wow)
                        if sub is None:
                            continue
                        try:
                            display = _reg_get_value(sub, "DisplayName")
                            if not display or _reg_get_value(sub, "SystemComponent") == "1":
                                continue
                            install = _reg_get_value(sub, "InstallLocation")
                            icon = _reg_get_value(sub, "DisplayIcon")
                            target = ""
                            if install:
                                for fn in os.listdir(install) if os.path.isdir(install) else []:
                                    if fn.lower().endswith(".exe"):
                                        target = os.path.join(install, fn)
                                        break
                            if not target and icon:
                                cand = icon.split(",")[0].strip('"')
                                if os.path.exists(cand):
                                    target = cand
                            if target:
                                out.append(AppCandidate(
                                    name=display, target=target, kind="exe",
                                    source="registry-uninstall",
                                    publisher=_reg_get_value(sub, "Publisher"),
                                    aliases=_aliases_for(display)))
                        finally:
                            _reg_close(sub)
                finally:
                    _reg_close(handle)
        return out

    def _from_powershell(self) -> List[AppCandidate]:
        """``Get-StartApps`` - covers Store/UWP apps that have no .lnk target."""
        if not self.use_powershell or not shutil.which("powershell"):
            return []
        script = ("Get-StartApps | ForEach-Object { \"$($_.Name)`t$($_.AppID)\" }")
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:  # noqa: BLE001
            log.debug("Get-StartApps failed: %s", exc)
            return []
        results: List[AppCandidate] = []
        for line in (out.stdout or "").splitlines():
            if "\t" not in line:
                continue
            name, appid = line.split("\t", 1)
            name, appid = name.strip(), appid.strip()
            if not name or not appid:
                continue
            if os.path.isabs(appid) and os.path.exists(appid):
                results.append(AppCandidate(name=name, target=appid, kind="exe",
                                            source="get-startapps",
                                            aliases=_aliases_for(name)))
            elif "!" in appid or appid.count("_") >= 3:
                results.append(AppCandidate(
                    name=name, target=f"shell:AppsFolder\\{appid}", kind="uwp",
                    source="get-startapps", aliases=_aliases_for(name)))
        return results

    def _from_path(self) -> List[AppCandidate]:
        out: List[AppCandidate] = []
        exts = [e for e in os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(";") if e]
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory or not os.path.isdir(directory):
                continue
            try:
                entries = os.listdir(directory)
            except OSError:
                continue
            for fn in entries:
                if os.path.splitext(fn)[1].upper() in [e.upper() for e in exts]:
                    full = os.path.join(directory, fn)
                    if os.path.isfile(full):
                        out.append(AppCandidate(
                            name=os.path.splitext(fn)[0], target=full, kind="exe",
                            source="path"))
        return out

    # ------------------------------------------------------------------
    def installed(self, refresh: bool = False) -> List[AppCandidate]:
        now = time.time()
        if not refresh and self._cache and now - self._cache_ts < CACHE_TTL_S:
            return list(self._cache)
        seen: Dict[str, AppCandidate] = {}

        def add(cands: Sequence[AppCandidate]) -> None:
            for c in cands:
                if not c.target:
                    continue
                key = _norm(c.name) or c.target.lower()
                prev = seen.get(key)
                # prefer the richer source
                rank = {"start-menu": 4, "get-startapps": 4, "registry-app-paths": 3,
                        "registry-uninstall": 3, "path": 1}.get(c.source, 2)
                prev_rank = {"start-menu": 4, "get-startapps": 4,
                             "registry-app-paths": 3, "registry-uninstall": 3,
                             "path": 1}.get(prev.source, 2) if prev else -1
                if prev is None or rank > prev_rank:
                    if prev is not None and prev.target != c.target:
                        c.aliases = sorted(set(c.aliases + prev.aliases + [prev.name]))
                    seen[key] = c

        add(self._from_shortcuts())
        add(self._from_registry())
        add(self._from_powershell())
        add(self._from_path())
        out = sorted(seen.values(), key=lambda c: c.name.lower())
        self._cache = out
        self._cache_ts = now
        return list(out)

    def resolve(self, query: str, limit: int = 5) -> List[AppCandidate]:
        q = (query or "").strip()
        if not q:
            return []
        scored = [(c.score(q), c) for c in self.installed()]
        scored = [(s, c) for s, c in scored if s >= 0.55]
        scored.sort(key=lambda t: (-t[0], t[1].name.lower()))
        return [c for _s, c in scored[:limit]]

    def launch(self, target: str, arguments: Sequence[str] = ()) -> Dict[str, Any]:
        target = (target or "").strip()
        if not target:
            return {"ok": False, "error": "no target"}
        args = [str(a) for a in arguments or ()]
        try:
            if target.lower().startswith(("http://", "https://", "file://", "mailto:")):
                os.startfile(target)  # type: ignore[attr-defined]
                return {"ok": True, "target": target, "kind": "url"}
            if target.lower().startswith("shell:"):
                subprocess.Popen(["explorer.exe", target],
                                 creationflags=_detached_flags())
                return {"ok": True, "target": target, "kind": "shell"}
            if os.path.isdir(target):
                subprocess.Popen(["explorer.exe", os.path.normpath(target)],
                                 creationflags=_detached_flags())
                return {"ok": True, "target": target, "kind": "folder"}
            if os.path.isfile(target):
                ext = os.path.splitext(target)[1].lower()
                if ext in (".exe", ".com", ".scr"):
                    subprocess.Popen([target, *args], cwd=os.path.dirname(target) or None,
                                     creationflags=_detached_flags(), close_fds=True)
                elif ext in (".bat", ".cmd"):
                    subprocess.Popen(["cmd.exe", "/c", target, *args],
                                     cwd=os.path.dirname(target) or None,
                                     creationflags=_detached_flags())
                else:
                    # documents, media, .lnk, .msc, .cpl -> let Windows decide
                    os.startfile(target)  # type: ignore[attr-defined]
                return {"ok": True, "target": target, "kind": "file", "pid": None}
            # Not a path: try `start` so file associations and App Paths apply.
            subprocess.Popen(["cmd.exe", "/c", "start", "", target, *args],
                             creationflags=_detached_flags())
            return {"ok": True, "target": target, "kind": "association"}
        except FileNotFoundError as exc:
            return {"ok": False, "error": f"not found: {target} ({exc})"}
        except OSError as exc:
            return {"ok": False, "error": f"could not launch {target}: {exc}"}


def _detached_flags() -> int:
    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    return flags


def _reg_default(handle: Any) -> str:
    """Read the (Default) value of an open key."""
    try:
        size = _wt.DWORD(0)
        _advapi32.RegQueryValueExW(handle, None, None, None, None, ctypes.byref(size))
        if size.value == 0 or size.value > 8192:
            return ""
        buf = ctypes.create_unicode_buffer(size.value + 2)
        vtype = _wt.DWORD(0)
        rc = _advapi32.RegQueryValueExW(handle, None, None, ctypes.byref(vtype),
                                        buf, ctypes.byref(size))
        return buf.value if rc == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _aliases_for(name: str) -> List[str]:
    base = re.sub(r"\s*\d{4}(\.\d+)*\s*$", "", name or "")   # "Blender 4.2"
    out = set()
    if base and base != name:
        out.add(base)
    without_suffix = re.sub(r"\.(exe|app)$", "", name or "", flags=re.I)
    if without_suffix != name:
        out.add(without_suffix)
    initials = "".join(w[0] for w in re.split(r"\s+", name or "") if w)
    if len(initials) >= 2:
        out.add(initials)
    return sorted(out)


# ---------------------------------------------------------------------------
class DesktopApps:
    """Linux/macOS resolver: .desktop entries + $PATH + /Applications."""

    name = "desktop"

    def __init__(self) -> None:
        self._cache: List[AppCandidate] = []
        self._cache_ts = 0.0

    def _search_dirs(self) -> List[str]:
        dirs = [
            "/usr/share/applications",
            "/usr/local/share/applications",
            os.path.expanduser("~/.local/share/applications"),
            "/var/lib/flatpak/exports/share/applications",
            os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
            "/Applications",
            os.path.expanduser("~/Applications"),
        ]
        xdg = os.environ.get("XDG_DATA_DIRS")
        if xdg:
            for d in xdg.split(os.pathsep):
                dirs.append(os.path.join(d, "applications"))
        return [d for d in dirs if d and os.path.isdir(d)]

    def installed(self, refresh: bool = False) -> List[AppCandidate]:
        if not refresh and self._cache and time.time() - self._cache_ts < CACHE_TTL_S:
            return list(self._cache)
        out: List[AppCandidate] = []
        for d in self._search_dirs():
            try:
                entries = os.listdir(d)
            except OSError:
                continue
            for fn in entries:
                full = os.path.join(d, fn)
                if fn.endswith(".desktop"):
                    parsed = _parse_desktop(full)
                    if parsed:
                        out.append(parsed)
                elif fn.endswith(".app") and os.path.isdir(full):
                    exe = os.path.join(full, "Contents", "MacOS", fn[:-4])
                    out.append(AppCandidate(name=fn[:-4],
                                            target=exe if os.path.exists(exe) else full,
                                            kind="desktop-entry", source="applications",
                                            aliases=_aliases_for(fn[:-4])))
        # $PATH executables
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory or not os.path.isdir(directory):
                continue
            try:
                for fn in os.listdir(directory):
                    full = os.path.join(directory, fn)
                    if os.path.isfile(full) and os.access(full, os.X_OK):
                        out.append(AppCandidate(name=fn, target=full, kind="exe",
                                                source="path"))
            except OSError:
                continue
        self._cache = out
        self._cache_ts = time.time()
        return list(out)

    def resolve(self, query: str, limit: int = 5) -> List[AppCandidate]:
        q = (query or "").strip()
        if not q:
            return []
        found = shutil.which(q)
        scored = [(c.score(q), c) for c in self.installed()]
        scored = [(s, c) for s, c in scored if s >= 0.55]
        scored.sort(key=lambda t: (-t[0], t[1].name.lower()))
        out = [c for _s, c in scored[:limit]]
        if found and not any(os.path.abspath(c.target) == os.path.abspath(found)
                             for c in out):
            out.insert(0, AppCandidate(name=os.path.basename(found), target=found,
                                       kind="exe", source="which"))
        return out[:limit]

    def launch(self, target: str, arguments: Sequence[str] = ()) -> Dict[str, Any]:
        target = (target or "").strip()
        if not target:
            return {"ok": False, "error": "no target"}
        args = [str(a) for a in arguments or ()]
        try:
            if target.startswith(("http://", "https://", "file://", "mailto:")):
                opener = "xdg-open" if sys.platform.startswith("linux") else "open"
                subprocess.Popen([opener, target])
                return {"ok": True, "target": target, "kind": "url"}
            if target.endswith(".desktop"):
                launcher = shutil.which("gtk-launch")
                if launcher:
                    subprocess.Popen([launcher, os.path.basename(target)[:-8]])
                    return {"ok": True, "target": target, "kind": "desktop-entry"}
                parsed = _parse_desktop(target)
                if parsed:
                    return self.launch(parsed.target, arguments)
                return {"ok": False, "error": "cannot parse .desktop entry"}
            if os.path.isdir(target):
                opener = "xdg-open" if sys.platform.startswith("linux") else "open"
                subprocess.Popen([opener, target])
                return {"ok": True, "target": target, "kind": "folder"}
            if not os.path.exists(target):
                resolved = shutil.which(target)
                if not resolved:
                    return {"ok": False, "error": f"not found: {target}"}
                target = resolved
            if sys.platform == "darwin" and target.endswith(".app"):
                subprocess.Popen(["open", "-a", target, *args])
            else:
                subprocess.Popen([target, *args], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "target": target, "kind": "file"}
        except OSError as exc:
            return {"ok": False, "error": f"could not launch {target}: {exc}"}


def _parse_desktop(path: str) -> Optional[AppCandidate]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    section = ""
    values: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        if section != "Desktop Entry" or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip()
    name = values.get("Name")
    exe = values.get("Exec")
    if not name or not exe:
        return None
    if values.get("NoDisplay", "").lower() == "true":
        return None
    argv = shlex.split(exe)
    argv = [a for a in argv if a not in ("%u", "%U", "%f", "%F", "%i", "%c", "%k")]
    if not argv:
        return None
    target = argv[0]
    if not os.path.isabs(target):
        resolved = shutil.which(target)
        if not resolved:
            return None
        target = resolved
    return AppCandidate(name=name, target=target, kind="desktop-entry",
                        source="desktop-file",
                        aliases=_aliases_for(name) + [values.get("GenericName", "")],
                        icon=values.get("Icon", ""))


# ---------------------------------------------------------------------------
class NullApps:
    name = "none"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def installed(self, refresh: bool = False) -> List[AppCandidate]:
        return []

    def resolve(self, query: str, limit: int = 5) -> List[AppCandidate]:
        return []

    def launch(self, target: str, arguments: Sequence[str] = ()) -> Dict[str, Any]:
        return {"ok": False, "error": f"no application backend ({self.reason})"}


def create_app_backend(preferred: str = "auto"):
    errors: List[str] = []
    if preferred == "auto":
        order = ["windows", "desktop"] if os.name == "nt" else ["desktop", "windows"]
    else:
        order = [preferred]
    for choice in order:
        try:
            if choice == "windows":
                return WindowsApps(), "Windows Start Menu / registry / Get-StartApps"
            if choice == "desktop":
                return DesktopApps(), ".desktop entries + $PATH"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{choice}: {exc}")
            log.debug("app backend %s unavailable: %s", choice, exc)
    return NullApps("; ".join(errors)), ("; ".join(errors) or "unsupported platform")
