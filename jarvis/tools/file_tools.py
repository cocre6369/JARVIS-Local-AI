"""File management.

Path handling here is deliberately defensive:

* user-friendly names ("Downloads", "my desktop", "~/Documents/report.md") are
  resolved through the real known-folder locations,
* system-critical locations are rejected before anything touches the disk (the
  policy engine independently rejects them too - defence in depth),
* destructive operations (delete, rename, move, overwrite, bulk organise) are
  classified so the security layer asks the user first,
* every operation is verified by re-stat-ing the filesystem afterwards.

Deletes go to the Recycle Bin whenever ``send2trash`` is available, so the
default path is reversible.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..security.policy import Risk
from .base import Tool, ToolContext, ToolResult, ToolSpec, Verification

log = logging.getLogger(__name__)

MAX_SCAN_FILES = 60000
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".log", ".xml", ".html", ".htm", ".css",
    ".js", ".ts", ".jsx", ".tsx", ".py", ".rb", ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".php", ".sh", ".bat", ".ps1", ".sql", ".env",
    ".gitignore", ".srt", ".vtt",
}
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".doc", ".odt", ".rtf", ".xlsx", ".pptx",
                       ".epub"}
SEARCH_ROOT_LIMIT = 4


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def known_folders() -> Dict[str, Path]:
    """Map friendly names to real folders on this machine."""
    out: Dict[str, Path] = {}
    home = _home()
    defaults = {
        "home": home,
        "desktop": "Desktop", "documents": "Documents", "downloads": "Downloads",
        "music": "Music", "pictures": "Pictures", "videos": "Videos",
        "screenshots": Path("Pictures") / "Screenshots",
        "projects": "Projects", "src": "Source", "temp": None,
    }
    for key, value in defaults.items():
        if value is None:
            continue
        p = home / value if not isinstance(value, Path) else home / value
        if p.is_dir():
            out[key] = p
    # Windows known folders from the environment are authoritative.
    env_map = [
        ("desktop", (("USERPROFILE", "Desktop"), ("OneDrive", "Desktop"))),
        ("documents", (("USERPROFILE", "Documents"), ("OneDrive", "Documents"))),
        ("downloads", (("USERPROFILE", "Downloads"),)),
        ("music", (("USERPROFILE", "Music"),)),
        ("pictures", (("USERPROFILE", "Pictures"),)),
        ("videos", (("USERPROFILE", "Videos"),)),
    ]
    for key, combos in env_map:
        for env, sub in combos:
            base = os.environ.get(env)
            if base:
                cand = Path(base) / sub
                if cand.is_dir():
                    out[key] = cand
                    break
    for env, key in (("TMP", "temp"), ("TEMP", "temp")):
        v = os.environ.get(env)
        if v and Path(v).is_dir():
            out.setdefault(key, Path(v))
            break
    out.setdefault("temp", Path.home())
    return out


def resolve_path(raw: str, must_exist: bool = False) -> Tuple[Path, str]:
    """Turn a user/model supplied string into a concrete path.

    Returns ``(path, error)`` - exactly one of them is meaningful.
    """
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return Path(), "No path was given."
    text = os.path.expandvars(text)
    folders = known_folders()
    low = text.lower().rstrip("/\\")
    # "my downloads folder", "downloads", "the desktop"
    cleaned = re.sub(r"^(my|the|our)\s+", "", low)
    cleaned = re.sub(r"\s+(folder|directory|dir)$", "", cleaned)
    if cleaned in folders and Path(folders[cleaned]).is_dir():
        return folders[cleaned], ""
    if low.startswith(("~",)):
        cand = Path(os.path.expanduser(text))
        return (cand, "") if (cand.exists() or not must_exist) else (
            cand, f"{cand} does not exist.")
    p = Path(text)
    if not p.is_absolute():
        # relative to a known folder if it looks like "Downloads/report.md"
        head = p.parts[0].lower() if p.parts else ""
        if head in folders:
            p = folders[head].joinpath(*p.parts[1:])
        else:
            p = folders.get("home", Path.cwd()) / p
    p = Path(os.path.normpath(str(p)))
    if must_exist and not p.exists():
        return p, f"{p} does not exist."
    return p, ""


def is_protected(path: Path) -> Optional[str]:
    """Return a reason string when the path must not be touched."""
    try:
        text = str(path).lower().replace("/", "\\")
    except Exception:  # noqa: BLE001
        return "unreadable path"
    from ..security.policy import CRITICAL_PATH_PREFIXES, ELEVATION_REQUIRED_PREFIXES

    for pref in CRITICAL_PATH_PREFIXES:
        p = pref.lower().replace("/", "\\")
        if text == p or text.startswith(p + "\\") or text.startswith(p + "/"):
            return f"{path} is inside a system-critical location ({pref})."
    for pref in ELEVATION_REQUIRED_PREFIXES:
        p = pref.lower().replace("/", "\\")
        if text.startswith(p + "\\") or text.startswith(p + "/"):
            return (f"{path} is inside {pref}, which needs administrator rights. "
                    "JARVIS never elevates.")
    return None


def writable_roots(settings: Any) -> List[Path]:
    folders = known_folders()
    roots = [folders.get(k) for k in ("home", "desktop", "documents", "downloads",
                                      "music", "pictures", "videos", "temp")]
    roots = [r for r in roots if r]
    extras = list(getattr(settings.automation, "writable_roots", []) or [])
    # ``allowed_roots`` is the older/looser name for the same idea: folders the
    # user has explicitly handed to JARVIS.  Both are honoured, both are still
    # checked against the prohibited system paths by the policy engine.
    extras += list(getattr(settings.automation, "allowed_roots", []) or [])
    seen = set()
    for extra in extras:
        p = Path(os.path.expandvars(str(extra))).expanduser()
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.is_dir():
            roots.append(p)
    return roots


def inside_writable(path: Path, settings: Any) -> bool:
    try:
        resolved = Path(os.path.abspath(str(path)))
    except Exception:  # noqa: BLE001
        return False
    for root in writable_roots(settings):
        try:
            r = Path(os.path.abspath(str(root)))
            if resolved == r or str(resolved).lower().startswith(str(r).lower() + os.sep):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}PB"


# ---------------------------------------------------------------------------
class SearchFilesTool(Tool):
    spec = ToolSpec(
        name="search_files",
        description=(
            "Find files and folders by name (wildcards allowed, e.g. '*.mp4', "
            "'*minecraft*screenshot*') or by text content. Defaults to searching "
            "your personal folders only. Can sort by newest, largest or name - "
            "use sort='newest' for 'my latest screenshot' style requests. "
            "Never searches protected system locations or credential stores."),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Filename pattern, part of a name, or text "
                                         "to look for inside files."},
                "root": {"type": "string",
                         "description": "Folder to search (default: your personal "
                                        "folders). Accepts 'Downloads', 'Desktop', "
                                        "'Documents' or an absolute path."},
                "extensions": {"type": "array", "items": {"type": "string"},
                               "description": "Restrict to these extensions, e.g. "
                                              "['.png','.jpg']."},
                "sort": {"type": "string",
                         "enum": ["relevance", "newest", "oldest", "largest", "name"]},
                "search_contents": {"type": "boolean",
                                    "description": "Also look inside text files for "
                                                   "the query (slower)."},
                "limit": {"type": "integer", "description": "Max results (default 25)."},
                "max_depth": {"type": "integer",
                              "description": "How deep to recurse (default 6)."},
            },
            "required": ["query"],
        },
        category="file_read", risk=Risk.ALLOWED, reversible=True, cost="medium")

    def run(self, query: str = "", root: str = "", extensions: Optional[List[str]] = None,
            sort: str = "relevance", search_contents: bool = False, limit: int = 25,
            max_depth: int = 6, **_: Any) -> ToolResult:
        query = (query or "").strip()
        if not query:
            return self._fail("A search query is required.")
        limit = max(1, min(int(limit or 25), 200))
        max_depth = max(1, min(int(max_depth or 6), 12))

        roots: List[Path] = []
        if root:
            p, err = resolve_path(root)
            if err:
                return self._fail(err)
            if is_protected(p):
                return ToolResult(ok=False, error=is_protected(p),
                                  data={"root": str(p)})
            roots = [p]
        else:
            folders = known_folders()
            for key in ("downloads", "documents", "desktop", "pictures", "videos",
                        "music", "home"):
                cand = folders.get(key)
                if cand and cand.is_dir() and cand not in roots:
                    roots.append(cand)
                if len(roots) >= SEARCH_ROOT_LIMIT + 2:
                    break
            roots = [r for r in roots if not is_protected(r)][:SEARCH_ROOT_LIMIT + 2]
        if not roots:
            return self._fail("No searchable folder could be determined.")

        exts = {("." + e.strip().lstrip(".")).lower()
                for e in (extensions or []) if str(e).strip()}
        pattern = query.lower()
        wildcard = any(ch in pattern for ch in "*?[")
        hits: List[Dict[str, Any]] = []
        scanned = 0
        t0 = time.time()
        content_matches = 0
        for r in roots:
            for path, depth in _walk(r, max_depth):
                scanned += 1
                if scanned > MAX_SCAN_FILES or time.time() - t0 > 25:
                    break
                try:
                    name = path.name
                    is_dir = path.is_dir()
                    if exts and not is_dir and path.suffix.lower() not in exts:
                        continue
                    matched = False
                    if is_dir:
                        matched = (pattern in name.lower()) if not wildcard else \
                            fnmatch.fnmatch(name.lower(), pattern)
                    else:
                        matched = (pattern in name.lower()) if not wildcard else \
                            fnmatch.fnmatch(name.lower(), pattern)
                    if not matched and search_contents and not is_dir \
                            and path.suffix.lower() in TEXT_EXTENSIONS:
                        if _file_contains(path, query):
                            matched = True
                            content_matches += 1
                    if not matched:
                        continue
                    st = path.stat()
                    hits.append({
                        "path": str(path), "name": name, "is_folder": is_dir,
                        "size": st.st_size, "size_h": human_size(st.st_size),
                        "modified": st.st_mtime,
                        "modified_h": time.strftime("%Y-%m-%d %H:%M",
                                                    time.localtime(st.st_mtime)),
                        "content_match": bool(search_contents and not is_dir
                                              and pattern not in name.lower()),
                    })
                except OSError:
                    continue
            if len(hits) > 2000:
                break

        sort = (sort or "relevance").lower()
        if sort == "newest":
            hits.sort(key=lambda h: h["modified"], reverse=True)
        elif sort == "oldest":
            hits.sort(key=lambda h: h["modified"])
        elif sort == "largest":
            hits.sort(key=lambda h: h["size"], reverse=True)
        elif sort == "name":
            hits.sort(key=lambda h: h["name"].lower())
        else:
            def score(h: Dict[str, Any]) -> float:
                n = h["name"].lower()
                s = 0.0
                if n == pattern:
                    s += 3
                elif n.startswith(pattern):
                    s += 2
                elif pattern in n:
                    s += 1.5
                if h.get("content_match"):
                    s += 1
                if not h["is_folder"]:
                    s += 0.2
                return s + h["modified"] / 1e12
            hits.sort(key=score, reverse=True)

        results = hits[:limit]
        data = {
            "query": query, "roots": [str(r) for r in roots],
            "scanned": scanned, "matches": len(hits),
            "results": [{"path": h["path"], "name": h["name"],
                         "is_folder": h["is_folder"], "size_h": h["size_h"],
                         "modified": h["modified_h"],
                         "content_match": h["content_match"]} for h in results],
            "search_time_s": round(time.time() - t0, 2),
        }
        if not results:
            return ToolResult(
                ok=False,
                error=(f"No file or folder matching '{query}' was found in "
                       f"{len(roots)} folder(s) ({scanned} entries scanned)."),
                data=data, evidence={"matches": 0})
        return ToolResult(ok=True,
                          message=f"Found {len(hits)} match(es); showing {len(results)}",
                          data=data, evidence={"matches": len(hits),
                                               "top": results[0]["path"]})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        n = int(ev.get("matches", 0))
        return Verification(checked=True, succeeded=n > 0,
                            detail=(f"{n} match(es); best: {ev.get('top', '')}"
                                    if n else "search completed with no matches"),
                            evidence=ev)


def _walk(root: Path, max_depth: int) -> Iterable[Tuple[Path, int]]:
    """Breadth-first walk with a depth cap that skips junk and protected dirs."""
    skip_names = {"node_modules", "__pycache__", ".git", ".venv", "venv",
                  "appdata", "$recycle.bin", "system volume information",
                  ".cache", ".npm", "site-packages", ".mypy_cache", ".tox",
                  "windows", "program files", "program files (x86)",
                  "programdata"}
    stack: List[Tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop(0)
        if depth > max_depth:
            continue
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            name = entry.name.lower()
            if name in skip_names or name.startswith("."):
                continue
            if is_protected(entry):
                continue
            yield entry, depth + 1
            if entry.is_dir() and depth + 1 < max_depth:
                stack.append((entry, depth + 1))


def _file_contains(path: Path, needle: str, limit_bytes: int = 400_000) -> bool:
    try:
        if path.stat().st_size > 20 * 1024 * 1024:
            return False
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            chunk = fh.read(limit_bytes)
        return needle.lower() in chunk.lower()
    except OSError:
        return False


class ListDirectoryTool(Tool):
    spec = ToolSpec(
        name="list_directory",
        description="List the contents of a folder (names, sizes, modified "
                    "dates, subfolders). Accepts 'Downloads', 'my desktop' or an "
                    "absolute path.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder to list."},
                "pattern": {"type": "string", "description": "Optional wildcard filter."},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
        category="file_read", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, path: str = "", pattern: str = "", limit: int = 120,
            **_: Any) -> ToolResult:
        p, err = resolve_path(path)
        if err:
            return self._fail(err)
        if not p.exists():
            return self._fail(f"{p} does not exist.")
        if not p.is_dir():
            return self._fail(f"{p} is a file, not a folder.")
        if is_protected(p):
            return ToolResult(ok=False, error=str(is_protected(p)))
        limit = max(1, min(int(limit or 120), 500))
        folders, files = [], []
        try:
            entries = sorted(p.iterdir(), key=lambda e: e.name.lower())
        except (OSError, PermissionError) as exc:
            return self._fail(f"Cannot read {p}: {exc}")
        for e in entries:
            if e.name.startswith("."):
                continue
            if pattern and not fnmatch.fnmatch(e.name.lower(), pattern.lower()):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            item = {"name": e.name, "path": str(e),
                    "size_h": human_size(st.st_size) if e.is_file() else "-",
                    "modified": time.strftime("%Y-%m-%d %H:%M",
                                              time.localtime(st.st_mtime))}
            (folders if e.is_dir() else files).append(item)
        data = {"path": str(p), "folder_count": len(folders), "file_count": len(files),
                "folders": folders[:limit], "files": files[:limit]}
        return ToolResult(ok=True,
                          message=f"{p.name}: {len(folders)} folder(s), {len(files)} file(s)",
                          data=data, evidence={"folders": len(folders),
                                               "files": len(files)})


class ReadDocumentTool(Tool):
    spec = ToolSpec(
        name="read_document",
        description="Read the contents of a text-like document (.txt, .md, .csv, "
                    ".json, .log, source code, subtitles). Returns at most the "
                    "first N characters. PDF/Office files are summarised only if "
                    "an optional reader is installed - otherwise the tool says so "
                    "instead of guessing. Credential files are refused.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_chars": {"type": "integer", "description": "Default 6000."},
                "line_start": {"type": "integer", "description": "Start at this line."},
            },
            "required": ["path"],
        },
        category="file_read", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, path: str = "", max_chars: int = 6000, line_start: int = 0,
            **_: Any) -> ToolResult:
        p, err = resolve_path(path, must_exist=True)
        if err:
            return self._fail(err)
        from ..security.policy import CREDENTIAL_STORE_MARKERS

        low = str(p).lower()
        if any(m in low for m in CREDENTIAL_STORE_MARKERS):
            return ToolResult(
                ok=False,
                error="That file looks like a credential or key store. JARVIS "
                      "never reads those.",
                data={"path": str(p)})
        if p.is_dir():
            return self._fail(f"{p} is a folder; use list_directory.")
        max_chars = max(200, min(int(max_chars or 6000), 60000))
        suffix = p.suffix.lower()
        try:
            if suffix in TEXT_EXTENSIONS or suffix == "":
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
                start = max(0, int(line_start or 0))
                body = "".join(lines[start:])[:max_chars]
                from ..security.redaction import redact_text

                return ToolResult(
                    ok=True,
                    message=f"Read {p.name} ({len(lines)} lines)",
                    data={"path": str(p), "lines": len(lines),
                          "text": redact_text(body),
                          "truncated": len(body) >= max_chars},
                    evidence={"chars": len(body), "lines": len(lines)})
            if suffix == ".pdf":
                return self._read_pdf(p, max_chars)
            if suffix in (".docx", ".odt"):
                return self._read_docx(p, max_chars)
            if suffix in DOCUMENT_EXTENSIONS:
                return ToolResult(
                    ok=False,
                    error=(f"JARVIS cannot read {suffix} files directly. I can open "
                           f"it in its normal application and read what appears on "
                           "screen instead."),
                    data={"path": str(p)})
        except OSError as exc:
            return self._fail(f"Could not read {p}: {exc}")
        # unknown extension: try as text, fall back to binary summary
        try:
            with open(p, "rb") as fh:
                head = fh.read(4096)
            if b"\x00" in head:
                return ToolResult(ok=True, message=f"{p.name} is a binary file",
                                  data={"path": str(p), "binary": True,
                                        "size_h": human_size(p.stat().st_size)})
            from ..security.redaction import redact_text

            return ToolResult(ok=True, message=f"Read {p.name} as text",
                              data={"path": str(p), "text": redact_text(
                                  head.decode("utf-8", "replace")[:max_chars])})
        except OSError as exc:
            return self._fail(f"Could not read {p}: {exc}")

    def _read_pdf(self, p: Path, max_chars: int) -> ToolResult:
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception:  # noqa: BLE001
            try:
                from PyPDF2 import PdfReader  # type: ignore
            except Exception:  # noqa: BLE001
                return ToolResult(
                    ok=False,
                    error=("No PDF reader is installed, so I cannot extract the text. "
                           "I can open the PDF and read it from the screen instead."),
                    data={"path": str(p)})
        try:
            reader = PdfReader(str(p))
            text = []
            for i, page in enumerate(reader.pages[:40]):
                text.append(page.extract_text() or "")
                if sum(len(t) for t in text) > max_chars:
                    break
            body = "\n".join(text)[:max_chars]
            from ..security.redaction import redact_text

            return ToolResult(ok=True,
                              message=f"Read {p.name} ({len(reader.pages)} pages)",
                              data={"path": str(p), "pages": len(reader.pages),
                                    "text": redact_text(body)},
                              evidence={"chars": len(body)})
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"PDF text extraction failed: {exc}")

    def _read_docx(self, p: Path, max_chars: int) -> ToolResult:
        import zipfile
        import xml.etree.ElementTree as ET

        try:
            with zipfile.ZipFile(p) as zf:
                xml = zf.read("word/document.xml")
            root = ET.fromstring(xml)
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            parts = [t.text or "" for t in root.iter(f"{ns}t")]
            body = " ".join(parts)[:max_chars]
            from ..security.redaction import redact_text

            return ToolResult(ok=True, message=f"Read {p.name}",
                              data={"path": str(p), "text": redact_text(body)},
                              evidence={"chars": len(body)})
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"Could not read {p.name}: {exc}")


class CreateFolderTool(Tool):
    spec = ToolSpec(
        name="create_folder",
        description="Create a new folder (parents included). Refuses system "
                    "locations; asks for approval when the target is outside "
                    "your personal folders.",
        parameters={"type": "object",
                    "properties": {"path": {"type": "string",
                                            "description": "Folder path to create."}},
                    "required": ["path"]},
        category="file_create", risk=Risk.ALLOWED, reversible=True)

    def run(self, path: str = "", **_: Any) -> ToolResult:
        p, err = resolve_path(path)
        if err:
            return self._fail(err)
        protected = is_protected(p)
        if protected:
            return ToolResult(ok=False, error=protected, data={"path": str(p)})
        if p.exists():
            return ToolResult(ok=True, message=f"{p} already exists",
                              data={"path": str(p), "created": False},
                              evidence={"exists": True, "created": False})
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._fail(f"Could not create {p}: {exc}")
        return ToolResult(ok=True, message=f"Created folder {p}",
                          data={"path": str(p), "created": True},
                          evidence={"exists": p.is_dir(), "created": True})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        p, _ = resolve_path(str(args.get("path", "")))
        ok = p.is_dir()
        return Verification(checked=True, succeeded=ok,
                            detail=(f"{p} exists and is a folder" if ok
                                    else f"{p} was not created"),
                            evidence={"path": str(p)})


class WriteFileTool(Tool):
    spec = ToolSpec(
        name="write_file",
        description="Write text to a file. Creating a new file inside your own "
                    "folders is allowed; overwriting an existing file always "
                    "requires your approval because the old contents are lost.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "text": {"type": "string", "description": "Content to write."},
                "append": {"type": "boolean", "description": "Append instead of replacing."},
            },
            "required": ["path", "text"],
        },
        category="file_write", risk=Risk.ALLOWED, reversible=False)

    def risk_for(self, args: Dict[str, Any]) -> Risk:
        p, _ = resolve_path(str(args.get("path", "")))
        if args.get("append"):
            return Risk.ALLOWED
        return Risk.APPROVAL if p.exists() else Risk.ALLOWED

    def run(self, path: str = "", text: str = "", append: bool = False,
            **_: Any) -> ToolResult:
        p, err = resolve_path(path)
        if err:
            return self._fail(err)
        protected = is_protected(p)
        if protected:
            return ToolResult(ok=False, error=protected, data={"path": str(p)})
        if not inside_writable(p.parent, self.settings):
            return ToolResult(
                ok=False,
                error=(f"{p.parent} is outside the folders JARVIS may write to "
                       "without approval."),
                data={"path": str(p)})
        existed = p.exists()
        if existed and not append:
            try:
                backup = p.with_suffix(p.suffix + ".jarvis-backup")
                shutil.copy2(p, backup)
            except OSError as exc:
                log.warning("Could not back up %s before overwriting: %s", p, exc)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a" if append else "w", encoding="utf-8") as fh:
                fh.write(str(text))
        except OSError as exc:
            return self._fail(f"Could not write {p}: {exc}")
        return ToolResult(ok=True,
                          message=(f"Appended {len(str(text))} characters to {p.name}"
                                   if append else
                                   f"Wrote {len(str(text))} characters to {p.name}"),
                          data={"path": str(p), "bytes": len(str(text)),
                                "overwrote": bool(existed and not append),
                                "backup": str(p.with_suffix(p.suffix + ".jarvis-backup"))
                                if existed and not append else ""},
                          evidence={"written": p.is_file(), "size": p.stat().st_size
                                    if p.is_file() else 0})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        p, _ = resolve_path(str(args.get("path", "")))
        ok = p.is_file()
        detail = f"{p} exists ({human_size(p.stat().st_size)})" if ok else f"{p} missing"
        if ok:
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    tail = fh.read()[-200:]
                expected = str(args.get("text", ""))[-120:]
                if expected and expected.strip() and expected.strip() not in tail \
                        and not str(args.get("append")):
                    detail += " (content differs from what was requested)"
                    ok = False
            except OSError:
                pass
        return Verification(checked=True, succeeded=ok, detail=detail,
                            evidence={"path": str(p)})


class RenameFileTool(Tool):
    spec = ToolSpec(
        name="rename_file",
        description="Rename or move a single file or folder within the same "
                    "directory. Requires approval because links to the old name "
                    "stop working.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Existing file/folder."},
                "new_name": {"type": "string", "description": "New file name (not a path)."},
            },
            "required": ["path", "new_name"],
        },
        category="file_move", risk=Risk.APPROVAL, reversible=True)

    def run(self, path: str = "", new_name: str = "", **_: Any) -> ToolResult:
        p, err = resolve_path(path, must_exist=True)
        if err:
            return self._fail(err)
        new_name = os.path.basename(str(new_name or "").strip().strip('"'))
        if not new_name:
            return self._fail("A new name is required.")
        if any(c in new_name for c in '\\/:*?"<>|'):
            return self._fail(f"'{new_name}' contains characters Windows does not allow.")
        protected = is_protected(p)
        if protected:
            return ToolResult(ok=False, error=protected)
        target = p.parent / new_name
        if target.exists():
            return ToolResult(ok=False,
                              error=f"{target} already exists - refusing to overwrite.",
                              data={"path": str(p), "target": str(target)})
        try:
            p.rename(target)
        except OSError as exc:
            return self._fail(f"Rename failed: {exc}")
        return ToolResult(ok=True, message=f"Renamed {p.name} -> {new_name}",
                          data={"from": str(p), "to": str(target)},
                          evidence={"to_exists": target.exists(),
                                    "from_gone": not p.exists()})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        ok = bool(ev.get("to_exists")) and bool(ev.get("from_gone"))
        return Verification(checked=True, succeeded=ok,
                            detail=("Renamed successfully" if ok
                                    else "Filesystem does not reflect the rename"),
                            evidence=ev)


class MoveFileTool(Tool):
    spec = ToolSpec(
        name="move_file",
        description="Move a single file or folder to another folder. Requires "
                    "approval. Overwriting an existing destination is refused "
                    "unless copy_overwrite is explicitly requested and approved.",
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "destination": {"type": "string",
                                "description": "Destination folder, or full new path."},
                "copy": {"type": "boolean", "description": "Copy instead of move."},
            },
            "required": ["source", "destination"],
        },
        category="file_move", risk=Risk.APPROVAL, reversible=True)

    def run(self, source: str = "", destination: str = "", copy: bool = False,
            **_: Any) -> ToolResult:
        src, err = resolve_path(source, must_exist=True)
        if err:
            return self._fail(err)
        dst, err = resolve_path(destination)
        if err:
            return self._fail(err)
        for p in (src, dst):
            protected = is_protected(p)
            if protected:
                return ToolResult(ok=False, error=protected)
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            return ToolResult(ok=False,
                              error=f"{dst} already exists. Nothing was moved.",
                              data={"source": str(src), "destination": str(dst)})
        if not inside_writable(dst.parent, self.settings):
            return ToolResult(ok=False,
                              error=f"{dst.parent} is outside the folders JARVIS "
                                    "may write to without approval.")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if copy:
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            else:
                shutil.move(str(src), str(dst))
        except (OSError, shutil.Error) as exc:
            return self._fail(f"{'Copy' if copy else 'Move'} failed: {exc}")
        return ToolResult(ok=True,
                          message=f"{'Copied' if copy else 'Moved'} {src.name} -> {dst}",
                          data={"source": str(src), "destination": str(dst),
                                "copy": bool(copy)},
                          evidence={"dst_exists": dst.exists(),
                                    "src_gone": (not src.exists()) if not copy else True})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        ok = bool(ev.get("dst_exists")) and bool(ev.get("src_gone"))
        return Verification(checked=True, succeeded=ok,
                            detail=("Verified on disk" if ok else
                                    "Filesystem does not reflect the operation"),
                            evidence=ev)


class DeleteFileTool(Tool):
    spec = ToolSpec(
        name="delete_file",
        description=(
            "Delete a file or folder. ALWAYS requires your explicit approval. "
            "Goes to the Recycle Bin when that is possible, so it can normally be "
            "undone; if the Recycle Bin is unavailable JARVIS says so before "
            "deleting permanently. System files, credential stores and anything "
            "outside your personal folders are refused outright."),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "permanent": {"type": "boolean",
                              "description": "Skip the Recycle Bin (irreversible)."},
            },
            "required": ["path"],
        },
        category="file_delete", risk=Risk.APPROVAL, reversible=True)

    def run(self, path: str = "", permanent: bool = False, **_: Any) -> ToolResult:
        p, err = resolve_path(path, must_exist=True)
        if err:
            return self._fail(err)
        protected = is_protected(p)
        if protected:
            return ToolResult(ok=False, error=protected, data={"path": str(p)})
        from ..security.policy import CREDENTIAL_STORE_MARKERS

        if any(m in str(p).lower() for m in CREDENTIAL_STORE_MARKERS):
            return ToolResult(ok=False,
                              error="That file looks like a credential store. "
                                    "JARVIS will not delete it.")
        if not inside_writable(p, self.settings):
            return ToolResult(ok=False,
                              error=f"{p} is outside your personal folders.")
        if str(p) in (str(_home()), str(_home()) + os.sep):
            return ToolResult(ok=False, error="Refusing to delete your home folder.")
        method = "permanent"
        if not permanent:
            try:
                from send2trash import send2trash  # type: ignore

                send2trash(str(p))
                method = "recycle-bin"
            except Exception as exc:  # noqa: BLE001
                log.info("Recycle Bin unavailable (%s); deleting permanently", exc)
                method = "permanent (Recycle Bin unavailable)"
                try:
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                except OSError as exc2:
                    return self._fail(f"Delete failed: {exc2}")
        else:
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
            except OSError as exc:
                return self._fail(f"Delete failed: {exc}")
        return ToolResult(ok=True, message=f"Deleted {p.name} ({method})",
                          data={"path": str(p), "method": method},
                          evidence={"gone": not p.exists(), "method": method})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        p, _ = resolve_path(str(args.get("path", "")))
        gone = not p.exists()
        return Verification(checked=True, succeeded=gone,
                            detail=(f"{p} no longer exists" if gone
                                    else f"{p} is still there"),
                            evidence={"gone": gone})


class FindLargestFilesTool(Tool):
    spec = ToolSpec(
        name="find_largest_files",
        description="Report the biggest files (and optionally folders) under a "
                    "path - the usual first step for 'my disk is full' or 'find "
                    "the largest files in my downloads'.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder to scan."},
                "limit": {"type": "integer", "description": "How many (default 15)."},
                "include_folders": {"type": "boolean"},
            },
            "required": ["path"],
        },
        category="file_read", risk=Risk.ALLOWED, reversible=True, cost="medium")

    def run(self, path: str = "", limit: int = 15, include_folders: bool = False,
            **_: Any) -> ToolResult:
        p, err = resolve_path(path)
        if err:
            return self._fail(err)
        if not p.is_dir():
            return self._fail(f"{p} is not a folder.")
        if is_protected(p):
            return ToolResult(ok=False, error=str(is_protected(p)))
        limit = max(1, min(int(limit or 15), 100))
        files: List[Dict[str, Any]] = []
        t0 = time.time()
        for entry, _depth in _walk(p, 8):
            if time.time() - t0 > 20:
                break
            try:
                if entry.is_file():
                    st = entry.stat()
                    files.append({"path": str(entry), "name": entry.name,
                                  "size": st.st_size, "size_h": human_size(st.st_size),
                                  "modified": time.strftime(
                                      "%Y-%m-%d", time.localtime(st.st_mtime))})
                elif include_folders:
                    total = sum(f.stat().st_size for f in entry.rglob("*")
                                if f.is_file())
                    files.append({"path": str(entry), "name": entry.name + "/",
                                  "size": total, "size_h": human_size(total),
                                  "modified": ""})
            except OSError:
                continue
        files.sort(key=lambda f: f["size"], reverse=True)
        top = files[:limit]
        total = sum(f["size"] for f in files)
        data = {"path": str(p), "scanned": len(files), "total_size_h": human_size(total),
                "largest": top}
        if not top:
            return ToolResult(ok=False, error=f"No files found under {p}.", data=data)
        return ToolResult(ok=True,
                          message=f"Largest of {len(files)} files under {p.name} "
                                  f"(total {human_size(total)})",
                          data=data, evidence={"count": len(files),
                                               "top": top[0]["path"]})


class OrganiseFolderTool(Tool):
    spec = ToolSpec(
        name="organise_folder",
        description=(
            "Tidy a folder by moving files into subfolders grouped by extension "
            "('by_type'), by month modified ('by_date') or by size class "
            "('by_size'). This moves many files at once, so it always asks for "
            "approval first and reports exactly what it would do. Existing "
            "subfolders are never touched."),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder to organise."},
                "by": {"type": "string", "enum": ["by_type", "by_date", "by_size"]},
                "dry_run": {"type": "boolean",
                            "description": "Report the plan without moving anything "
                                           "(default true)."},
            },
            "required": ["path"],
        },
        category="bulk_file_move", risk=Risk.APPROVAL, reversible=True)

    def run(self, path: str = "", by: str = "by_type", dry_run: bool = True,
            **_: Any) -> ToolResult:
        p, err = resolve_path(path)
        if err:
            return self._fail(err)
        if not p.is_dir():
            return self._fail(f"{p} is not a folder.")
        protected = is_protected(p)
        if protected:
            return ToolResult(ok=False, error=protected)
        if not inside_writable(p, self.settings):
            return ToolResult(ok=False,
                              error=f"{p} is outside the folders JARVIS may "
                                    "reorganise without approval.")
        by = (by or "by_type").lower()
        plan: List[Dict[str, str]] = []
        try:
            entries = [e for e in p.iterdir() if e.is_file() and not e.name.startswith(".")]
        except OSError as exc:
            return self._fail(f"Cannot read {p}: {exc}")
        for e in entries:
            if by == "by_type":
                bucket = e.suffix.lower().lstrip(".") or "no-extension"
                bucket = _type_bucket(bucket)
            elif by == "by_date":
                bucket = time.strftime("%Y-%m", time.localtime(e.stat().st_mtime))
            elif by == "by_size":
                size = e.stat().st_size
                bucket = ("large-100mb+" if size > 100 * 1024 * 1024 else
                          "medium-10mb-100mb" if size > 10 * 1024 * 1024 else
                          "small-under-10mb")
            else:
                return self._fail(f"Unknown organisation mode '{by}'.")
            plan.append({"file": str(e), "to": str(p / bucket / e.name),
                         "bucket": bucket})
        moved = 0
        errors: List[str] = []
        if not dry_run:
            for item in plan:
                src, dst = Path(item["file"]), Path(item["to"])
                if dst.exists():
                    errors.append(f"{dst.name} already exists in {item['bucket']}")
                    continue
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                    moved += 1
                except OSError as exc:
                    errors.append(f"{src.name}: {exc}")
        buckets = sorted({i["bucket"] for i in plan})
        data = {"path": str(p), "mode": by, "dry_run": bool(dry_run),
                "planned": len(plan), "moved": moved, "buckets": buckets,
                "plan": plan[:60], "errors": errors[:20]}
        msg = (f"Plan: {len(plan)} file(s) into {len(buckets)} group(s): "
               f"{', '.join(buckets[:8])}" if dry_run else
               f"Moved {moved} of {len(plan)} file(s)")
        return ToolResult(ok=True, message=msg, data=data,
                          evidence={"moved": moved, "planned": len(plan),
                                    "dry_run": bool(dry_run)})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        if ev.get("dry_run"):
            return Verification(checked=True, succeeded=True,
                                detail="Dry run - nothing was moved yet",
                                evidence=ev)
        ok = int(ev.get("moved", 0)) > 0
        return Verification(checked=True, succeeded=ok,
                            detail=f"{ev.get('moved', 0)} of {ev.get('planned', 0)} "
                                   "files moved",
                            evidence=ev)


_TYPE_BUCKETS = {
    "jpg": "Images", "jpeg": "Images", "png": "Images", "gif": "Images",
    "webp": "Images", "bmp": "Images", "svg": "Images", "heic": "Images",
    "mp4": "Videos", "mkv": "Videos", "avi": "Videos", "mov": "Videos",
    "webm": "Videos", "wmv": "Videos",
    "mp3": "Audio", "wav": "Audio", "flac": "Audio", "m4a": "Audio",
    "ogg": "Audio", "aac": "Audio",
    "zip": "Archives", "rar": "Archives", "7z": "Archives", "tar": "Archives",
    "gz": "Archives", "iso": "Archives",
    "pdf": "Documents", "doc": "Documents", "docx": "Documents", "odt": "Documents",
    "xls": "Documents", "xlsx": "Documents", "ppt": "Documents", "pptx": "Documents",
    "txt": "Documents", "md": "Documents", "csv": "Documents", "rtf": "Documents",
    "exe": "Programs", "msi": "Programs", "appx": "Programs", "bat": "Programs",
    "py": "Code", "js": "Code", "ts": "Code", "json": "Code", "html": "Code",
    "css": "Code", "ps1": "Code", "c": "Code", "cpp": "Code", "cs": "Code",
    "jarvis": "Projects", "blend": "Projects", "psd": "Projects", "aep": "Projects",
    "prproj": "Projects", "flp": "Projects",
}


def _type_bucket(ext: str) -> str:
    return _TYPE_BUCKETS.get(ext.lower(), "Other")


class GetFileInfoTool(Tool):
    spec = ToolSpec(
        name="get_file_info",
        description="Report metadata for one file or folder: size, created and "
                    "modified times, type, and whether it is readable.",
        parameters={"type": "object",
                    "properties": {"path": {"type": "string"}}, "required": ["path"]},
        category="file_read", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, path: str = "", **_: Any) -> ToolResult:
        p, err = resolve_path(path, must_exist=True)
        if err:
            return self._fail(err)
        try:
            st = p.stat()
        except OSError as exc:
            return self._fail(f"Cannot stat {p}: {exc}")
        info = {
            "path": str(p), "name": p.name, "is_folder": p.is_dir(),
            "extension": p.suffix.lower(), "size": st.st_size,
            "size_h": human_size(st.st_size),
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_ctime)),
            "readable": os.access(p, os.R_OK), "writable": os.access(p, os.W_OK),
        }
        if p.is_dir():
            try:
                children = list(p.iterdir())
                info["child_count"] = len(children)
                info["child_folders"] = sum(1 for c in children if c.is_dir())
            except OSError:
                info["child_count"] = -1
        return ToolResult(ok=True, message=f"{p.name} ({info['size_h']})", data=info,
                          evidence={"exists": True, "size": st.st_size})


def build(ctx: ToolContext) -> List[Tool]:
    return [SearchFilesTool(ctx), ListDirectoryTool(ctx), ReadDocumentTool(ctx),
            CreateFolderTool(ctx), WriteFileTool(ctx), RenameFileTool(ctx),
            MoveFileTool(ctx), DeleteFileTool(ctx), FindLargestFilesTool(ctx),
            OrganiseFolderTool(ctx), GetFileInfoTool(ctx)]
