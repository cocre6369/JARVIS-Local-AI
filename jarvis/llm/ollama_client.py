"""Local Ollama integration.

This is JARVIS's only AI backend.  There is no OpenAI, no Anthropic, no cloud
endpoint anywhere in the project - :func:`assert_local_endpoint` is called
before a socket is opened, so an accidental (or malicious) configuration change
raises instead of silently sending your data somewhere.

The client is deliberately built on the standard library (``urllib``) so the
packaged .exe has one less native dependency and so streaming NDJSON works
without pulling in an async HTTP stack.  Every call happens on a worker thread;
the Qt event loop is never blocked.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from ..security.netguard import NetworkPolicyViolation, assert_local_endpoint

log = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    """Base class for every Ollama failure."""


class OllamaUnavailable(OllamaError):
    """Server not reachable - not installed, not running, or wrong port."""


class OllamaTimeout(OllamaError):
    """Request exceeded the configured timeout."""


class OllamaModelError(OllamaError):
    """The server answered but the model failed (OOM, bad name, ...)."""


@dataclass
class ModelInfo:
    name: str
    size_bytes: int = 0
    modified_at: str = ""
    family: str = ""
    parameter_size: str = ""
    quantization: str = ""
    capabilities: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)
    #: Normalised capability flags used everywhere else in the app.
    supports_tools: bool = False
    supports_vision: bool = False
    supports_system: bool = True
    is_embedding: bool = False
    context_length: int = 0

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 ** 3)

    @property
    def short_name(self) -> str:
        return self.name.split(":")[0]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "size_bytes": self.size_bytes,
            "size_gb": round(self.size_gb, 2), "modified_at": self.modified_at,
            "family": self.family, "parameter_size": self.parameter_size,
            "quantization": self.quantization, "capabilities": self.capabilities,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "is_embedding": self.is_embedding,
            "context_length": self.context_length,
        }


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    model: str = ""
    done_reason: str = ""
    prompt_eval_count: int = 0
    eval_count: int = 0
    total_duration_ms: float = 0.0
    tokens_per_second: float = 0.0
    images_used: int = 0
    used_tools_api: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------
#: Families that expose a tool-calling head in Ollama.  Used only as a
#: fallback when the server does not report capabilities.
_TOOL_FAMILIES = {
    "llama3.1", "llama3.2", "llama3.3", "llama4", "qwen2", "qwen2.5", "qwen3",
    "mistral-nemo", "mistral-small", "mistral-large", "mixtral", "command-r",
    "firefunction", "functionary", "granite", "granite3", "nemotron",
    "ministral-3b", "ministral-8b", "hfctranslate", "deepseek-r1", "deepseek-v2",
    "deepseek-v3", "gemma3", "aya-expanse", "smolvm", "toolace", "hammer",
    "atlas", "bespoke", "grok", "olmo3",
}
_VISION_FAMILIES = {
    "llava", "bakllava", "llama3.2-vision", "llama3.2v", "qwen2vl", "qwen2.5vl",
    "qwen3vl", "minicpm-v", "moondream", "cogvlm", "yi-vl", "glm4v", "gemma3",
    "granite-vision", "llama4", "aya-vision", "mistral-small3.2", "smolvlm",
    "pixtral", "llava-phi3", "llava-llama3",
}
_VISION_NAME_MARKERS = (
    "llava", "vision", "-v:", "vl", "minicpm-v", "moondream", "cogvlm",
    "bakllava", "pixtral", "gemma3", "aya-vision", "smolvlm", "granite-vision",
    "qwen2.5vl", "qwen3vl", "llama3.2-vision", "mistral-small3.2",
)


def detect_capabilities(model_name: str, server_caps: Sequence[str],
                        family: str = "", model_info: Optional[Dict[str, Any]] = None
                        ) -> Dict[str, bool]:
    """Merge server-reported capabilities with name/family heuristics."""
    caps = {c.lower() for c in (server_caps or [])}
    name = (model_name or "").lower()
    fam = (family or "").lower()
    base_family = fam.split(".")[0] if fam else ""

    tools = "tools" in caps or "tool_use" in caps
    vision = "vision" in caps or "image" in caps
    embedding = "embedding" in caps or "embeddings" in caps
    completion = "completion" in caps

    if not caps:
        # Heuristic fallback for older Ollama builds.
        if fam in _TOOL_FAMILIES or base_family in _TOOL_FAMILIES:
            tools = True
        if any(m in name for m in ("functionary", "firefunction", "toolace",
                                   "hammer", "bespoke", "atlas")):
            tools = True
        if fam in _VISION_FAMILIES or any(m in name for m in _VISION_NAME_MARKERS):
            vision = True
        if "embed" in name or "bge" in name or "nomic-embed" in name or \
                "mxbai-embed" in name or "snowflake-arctic-embed" in name:
            embedding = True
            completion = False

    if model_info:
        arch = str(model_info.get("architecture", "")).lower()
        if arch in ("clip", "siglip", "t2i", "wav2vec"):
            completion = False
    return {
        "tools": bool(tools),
        "vision": bool(vision),
        "embedding": bool(embedding),
        "completion": bool(completion) or not embedding,
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class OllamaClient:
    def __init__(self, settings_provider: Callable[[], Any],
                 allow_private_lan: bool = True) -> None:
        self._settings_provider = settings_provider
        self._allow_private_lan = allow_private_lan
        self._lock = threading.RLock()
        self._models_cache: List[ModelInfo] = []
        self._models_cache_ts: float = 0.0
        self._show_cache: Dict[str, Dict[str, Any]] = {}
        self._version: str = ""
        self._last_error: str = ""
        self._healthy: Optional[bool] = None
        self._healthy_ts: float = 0.0
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})  # never route localhost via a proxy
        )

    # ------------------------------------------------------------------
    @property
    def settings(self):
        return self._settings_provider()

    @property
    def base_url(self) -> str:
        return self.settings.base_url

    @property
    def last_error(self) -> str:
        return self._last_error

    def _assert_local(self) -> None:
        assert_local_endpoint(self.base_url, allow_private_lan=self._allow_private_lan)

    # ------------------------------------------------------------------
    # low level HTTP
    # ------------------------------------------------------------------
    def _request(self, path: str, payload: Optional[Dict[str, Any]] = None,
                 timeout: Optional[float] = None,
                 stream: bool = False) -> Any:
        self._assert_local()
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method="POST" if data is not None else "GET",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "JARVIS-local-assistant/1.0"},
        )
        to = float(timeout if timeout is not None else self.settings.connect_timeout_s)
        try:
            resp = self._opener.open(req, timeout=to)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:600]
            except Exception:  # noqa: BLE001
                pass
            self._set_unhealthy(str(exc))
            try:
                detail = json.loads(body).get("error", body)
            except Exception:  # noqa: BLE001
                detail = body or str(exc)
            raise OllamaModelError(f"Ollama HTTP {exc.code} on {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            self._set_unhealthy(str(reason))
            raise OllamaUnavailable(_friendly_connect_error(reason)) from exc
        except socket.timeout as exc:
            self._set_unhealthy("timeout")
            raise OllamaTimeout(f"Ollama request to {path} timed out after {to}s") from exc
        except NetworkPolicyViolation:
            raise
        except Exception as exc:  # noqa: BLE001
            self._set_unhealthy(str(exc))
            raise OllamaUnavailable(f"Could not reach Ollama at {url}: {exc}") from exc

        self._set_healthy()
        if stream:
            return resp
        try:
            raw = resp.read()
        finally:
            resp.close()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise OllamaError(f"Ollama returned invalid JSON from {path}") from exc

    def _set_healthy(self) -> None:
        with self._lock:
            self._healthy = True
            self._healthy_ts = time.time()
            self._last_error = ""

    def _set_unhealthy(self, why: str) -> None:
        with self._lock:
            self._healthy = False
            self._healthy_ts = time.time()
            self._last_error = why

    # ------------------------------------------------------------------
    # health / discovery
    # ------------------------------------------------------------------
    def ping(self, timeout: Optional[float] = None) -> bool:
        try:
            self._request("/api/version", timeout=timeout or 2.5)
            return True
        except NetworkPolicyViolation as exc:
            log.error("Network policy violation: %s", exc)
            return False
        except OllamaError as exc:
            log.debug("Ollama ping failed: %s", exc)
            return False

    def status(self) -> Dict[str, Any]:
        """Cheap, cached health probe used by the status bar."""
        with self._lock:
            fresh = self._healthy is not None and (time.time() - self._healthy_ts) < 5.0
            healthy = bool(self._healthy)
            err = self._last_error
        if fresh:
            return {"connected": healthy, "error": err, "base_url": self.base_url,
                    "version": self._version, "cached": True}
        ok = self.ping()
        return {"connected": ok, "error": self._last_error,
                "base_url": self.base_url, "version": self.version(), "cached": False}

    def version(self) -> str:
        if self._version:
            return self._version
        try:
            data = self._request("/api/version", timeout=3.0)
            self._version = str(data.get("version", ""))
        except OllamaError:
            self._version = ""
        return self._version

    def list_models(self, force: bool = False, max_age_s: float = 10.0) -> List[ModelInfo]:
        with self._lock:
            if (not force and self._models_cache
                    and time.time() - self._models_cache_ts < max_age_s):
                return list(self._models_cache)
        try:
            data = self._request("/api/tags", timeout=6.0)
        except OllamaError as exc:
            log.warning("Could not list Ollama models: %s", exc)
            return list(self._models_cache)
        models: List[ModelInfo] = []
        for entry in data.get("models", []) or []:
            name = entry.get("name") or entry.get("model") or ""
            if not name:
                continue
            details = entry.get("details") or {}
            family = details.get("family") or details.get("architecture") or ""
            server_caps = entry.get("capabilities") or []
            caps = detect_capabilities(name, server_caps, family)
            mi = entry.get("model_info") or {}
            ctx = 0
            for key, value in mi.items():
                if key.endswith("context_length") and isinstance(value, int):
                    ctx = max(ctx, value)
            models.append(ModelInfo(
                name=name,
                size_bytes=int(entry.get("size") or 0),
                modified_at=str(entry.get("modified_at") or ""),
                family=family,
                parameter_size=str(details.get("parameter_size") or ""),
                quantization=str(details.get("quantization_level") or ""),
                capabilities=sorted(set(list(server_caps) + [
                    k for k, v in caps.items() if v])),
                details=details,
                supports_tools=caps["tools"],
                supports_vision=caps["vision"],
                is_embedding=caps["embedding"],
                context_length=ctx,
            ))
        models.sort(key=lambda m: m.name)
        with self._lock:
            self._models_cache = models
            self._models_cache_ts = time.time()
        return list(models)

    def model_names(self) -> List[str]:
        return [m.name for m in self.list_models()]

    def find_model(self, wanted: str) -> Optional[ModelInfo]:
        """Resolve a user-typed model name, tolerating a missing ``:tag``."""
        if not wanted:
            return None
        wanted = wanted.strip()
        models = self.list_models()
        exact = [m for m in models if m.name == wanted]
        if exact:
            return exact[0]
        low = wanted.lower()
        prefix = [m for m in models if m.name.lower().startswith(low)]
        if prefix:
            return sorted(prefix, key=lambda m: len(m.name))[0]
        base = [m for m in models if m.short_name.lower() == low.split(":")[0]]
        if base:
            return base[0]
        return None

    def show(self, model: str) -> Dict[str, Any]:
        with self._lock:
            if model in self._show_cache:
                return self._show_cache[model]
        try:
            data = self._request("/api/show", {"name": model}, timeout=8.0)
        except OllamaError as exc:
            log.debug("show(%s) failed: %s", model, exc)
            return {}
        with self._lock:
            self._show_cache[model] = data
        return data

    def capabilities(self, model: str) -> Dict[str, bool]:
        info = self.find_model(model) or ModelInfo(name=model)
        show = self.show(model)
        mi = show.get("model_info") or {}
        family = info.family or str(mi.get("general.architecture", ""))
        return detect_capabilities(
            model, show.get("capabilities") or info.capabilities, family, mi)

    def context_length(self, model: str) -> int:
        show = self.show(model)
        mi = show.get("model_info") or {}
        best = 0
        for key, value in mi.items():
            if key.endswith("context_length") and isinstance(value, int):
                best = max(best, value)
        return best

    # ------------------------------------------------------------------
    # generation
    # ------------------------------------------------------------------
    def _options(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        s = self.settings
        opts: Dict[str, Any] = {
            "temperature": s.temperature,
            "top_p": s.top_p,
            "num_ctx": s.num_ctx,
            "use_mmap": s.use_mmap,
        }
        if s.num_gpu is not None and int(s.num_gpu) >= 0:
            opts["num_gpu"] = int(s.num_gpu)
        if s.num_thread and int(s.num_thread) > 0:
            opts["num_thread"] = int(s.num_thread)
        if extra:
            opts.update(extra)
        return opts

    def chat(self,
             messages: Sequence[Dict[str, Any]],
             model: Optional[str] = None,
             tools: Optional[Sequence[Dict[str, Any]]] = None,
             images: Optional[Sequence[str]] = None,
             stream: bool = True,
             on_token: Optional[Callable[[str], None]] = None,
             options: Optional[Dict[str, Any]] = None,
             keep_alive: Optional[str] = None,
             response_format: Optional[Any] = None,
             timeout: Optional[float] = None,
             ) -> ChatResult:
        """One chat completion.  Streams NDJSON when ``stream`` is true."""
        s = self.settings
        model = (model or s.main_model or "").strip()
        if not model:
            raise OllamaModelError(
                "No Ollama model selected. Open the Models page and pick one.")

        payload: Dict[str, Any] = {
            "model": model,
            "messages": [_strip_message(m, images if m is messages[-1] else None)
                         for m in messages],
            "stream": bool(stream and s.streaming),
            "options": self._options(options),
            "keep_alive": keep_alive or s.keep_alive,
        }
        if tools:
            payload["tools"] = list(tools)
        if response_format is not None:
            payload["format"] = response_format

        t0 = time.time()
        result = ChatResult(model=model, used_tools_api=bool(tools))
        if images:
            result.images_used = len(images)

        if payload["stream"]:
            resp = self._request("/api/chat", payload,
                                 timeout=timeout or s.request_timeout_s, stream=True)
            try:
                for raw_line in resp:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    msg = chunk.get("message") or {}
                    piece = msg.get("content") or ""
                    if piece:
                        result.content += piece
                        if on_token:
                            try:
                                on_token(piece)
                            except Exception:  # noqa: BLE001
                                log.exception("on_token callback failed")
                    tcs = msg.get("tool_calls")
                    if tcs:
                        for tc in tcs:
                            fn = (tc or {}).get("function") or {}
                            name = fn.get("name") or ""
                            args = fn.get("arguments") or {}
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except json.JSONDecodeError:
                                    args = {"text": args}
                            if name:
                                result.tool_calls.append({"name": name, "arguments": args})
                    if chunk.get("error"):
                        result.error = str(chunk["error"])
                    if chunk.get("done"):
                        result.done_reason = str(chunk.get("done_reason") or "")
                        result.prompt_eval_count = int(chunk.get("prompt_eval_count") or 0)
                        result.eval_count = int(chunk.get("eval_count") or 0)
                        dur = chunk.get("total_duration")
                        if isinstance(dur, (int, float)) and dur > 0:
                            result.total_duration_ms = dur / 1e6
                        break
                    if time.time() - t0 > float(timeout or s.request_timeout_s):
                        raise OllamaTimeout(
                            f"Generation exceeded {timeout or s.request_timeout_s}s")
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
        else:
            data = self._request("/api/chat", payload,
                                 timeout=timeout or s.request_timeout_s)
            msg = data.get("message") or {}
            result.content = msg.get("content") or ""
            for tc in msg.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"text": args}
                if fn.get("name"):
                    result.tool_calls.append({"name": fn["name"], "arguments": args})
            result.done_reason = str(data.get("done_reason") or "")
            result.prompt_eval_count = int(data.get("prompt_eval_count") or 0)
            result.eval_count = int(data.get("eval_count") or 0)
            dur = data.get("total_duration")
            if isinstance(dur, (int, float)) and dur > 0:
                result.total_duration_ms = dur / 1e6
            if data.get("error"):
                result.error = str(data["error"])

        if result.total_duration_ms <= 0:
            result.total_duration_ms = (time.time() - t0) * 1000.0
        if result.eval_count and result.total_duration_ms > 0:
            result.tokens_per_second = result.eval_count / (result.total_duration_ms / 1000.0)
        if result.done_reason in ("length",) and not result.error:
            log.warning("Generation truncated (done_reason=length) for %s", model)
        return result

    def generate_json(self, prompt: str, schema: Optional[Dict[str, Any]] = None,
                      model: Optional[str] = None, system: str = "",
                      temperature: float = 0.0, timeout: Optional[float] = None,
                      ) -> Dict[str, Any]:
        """Structured output.  Uses Ollama's ``format`` (schema-aware on recent
        builds, ``"json"`` otherwise) and falls back to robust text parsing."""
        s = self.settings
        model = model or s.main_model
        response_format: Any = "json"
        if schema:
            try:
                # Recent Ollama accepts a full JSON-schema here.
                response_format = schema
            except Exception:  # noqa: BLE001
                response_format = "json"
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            res = self.chat(messages, model=model, stream=False,
                            options={"temperature": temperature, "num_predict": 1200},
                            response_format=response_format, timeout=timeout)
            text = res.content
        except OllamaError as exc:
            if "format" in str(exc).lower() or "schema" in str(exc).lower():
                # Older server: retry without structured format.
                res = self.chat(messages, model=model, stream=False,
                                options={"temperature": temperature},
                                timeout=timeout)
                text = res.content
            else:
                raise
        from .protocol import extract_json
        data = extract_json(text)
        if data is None:
            raise OllamaModelError(f"Model did not return usable JSON: {text[:200]!r}")
        return data if isinstance(data, dict) else {"value": data}

    def embeddings(self, model: str, prompt: str) -> List[float]:
        data = self._request("/api/embeddings", {"model": model, "prompt": prompt},
                             timeout=30.0)
        return list(data.get("embedding") or [])

    # ------------------------------------------------------------------
    # VRAM management
    # ------------------------------------------------------------------
    def unload(self, model: str) -> bool:
        """Free a model from VRAM (keep_alive=0).  Used by the resource guard."""
        if not model:
            return False
        try:
            self._request("/api/generate",
                          {"model": model, "prompt": "", "keep_alive": 0,
                           "stream": False, "options": {"num_predict": 0}},
                          timeout=15.0)
            log.info("Unloaded %s from GPU memory", model)
            return True
        except OllamaError as exc:
            log.warning("Could not unload %s: %s", model, exc)
            return False

    def warm(self, model: str) -> bool:
        """Load a model into memory without generating (keeps latency low)."""
        if not model:
            return False
        try:
            self._request("/api/generate",
                          {"model": model, "prompt": "", "keep_alive": self.settings.keep_alive,
                           "stream": False, "options": {"num_predict": 0}},
                          timeout=max(20.0, self.settings.request_timeout_s))
            return True
        except OllamaError as exc:
            log.warning("Could not warm %s: %s", model, exc)
            return False

    def running_models(self) -> List[str]:
        try:
            data = self._request("/api/ps", timeout=6.0)
        except OllamaError:
            return []
        out = []
        for m in data.get("models", []) or []:
            name = m.get("name") or m.get("model")
            if name:
                out.append(str(name))
        return out

    def pull(self, model: str, on_progress: Optional[Callable[[Dict[str, Any]], None]] = None
             ) -> bool:
        """Download a model.  Only reachable behind an explicit user approval
        (see ``APPR-DL-01`` / the Models page confirmation dialog)."""
        self._assert_local()
        payload = json.dumps({"name": model, "stream": True}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/pull", data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            resp = self._opener.open(req, timeout=3600)
        except Exception as exc:  # noqa: BLE001
            raise OllamaError(f"Pull failed: {exc}") from exc
        try:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if on_progress:
                    on_progress(chunk)
                if chunk.get("error"):
                    raise OllamaError(str(chunk["error"]))
                if chunk.get("done"):
                    return True
        finally:
            resp.close()
        return False

    def delete_model(self, model: str) -> bool:
        self._assert_local()
        try:
            self._request("/api/delete", {"name": model}, timeout=15.0)
        except OllamaError as exc:
            log.warning("delete_model(%s) failed: %s", model, exc)
            return False
        with self._lock:
            self._models_cache = [m for m in self._models_cache if m.name != model]
            self._show_cache.pop(model, None)
        return True


# ---------------------------------------------------------------------------
def _strip_message(msg: Dict[str, Any], images: Optional[Iterable[str]]) -> Dict[str, Any]:
    out = {
        "role": msg.get("role", "user"),
        "content": msg.get("content", "") or "",
    }
    if msg.get("tool_calls"):
        out["tool_calls"] = msg["tool_calls"]
    if msg.get("tool_name"):
        # Ollama expects tool output as a `tool` role message.
        out["role"] = "tool"
    imgs = msg.get("images")
    if imgs is None and images is not None:
        imgs = list(images)
    if imgs:
        out["images"] = [i for i in imgs if i]
    return out


def _friendly_connect_error(reason: Any) -> str:
    text = str(reason)
    if "refused" in text.lower() or "actively refused" in text.lower():
        return ("Ollama is not running. Start it (Ollama icon in the system "
                "tray, or `ollama serve`) and JARVIS will reconnect "
                "automatically.")
    if "timed out" in text.lower():
        return "Ollama did not respond in time. It may be busy loading a model."
    if "name or service not known" in text.lower() or "getaddrinfo" in text.lower():
        return "The configured Ollama host name could not be resolved."
    return f"Could not reach Ollama ({text})."


def is_ollama_installed() -> Dict[str, Any]:
    """Best-effort detection of a local Ollama installation.

    Returns install hints for the setup wizard; never raises.
    """
    import shutil

    exe = shutil.which("ollama")
    candidates = [
        exe,
        r"C:\Program Files\Ollama\ollama.exe",
        r"C:\Program Files\Ollama\bin\ollama.exe",
        "/usr/local/bin/ollama", "/opt/homebrew/bin/ollama", "/usr/bin/ollama",
    ]
    path = next((c for c in candidates if c), None)
    version = ""
    if path:
        try:
            import subprocess

            out = subprocess.run([path, "--version"], capture_output=True,
                                 text=True, timeout=6)
            version = (out.stdout or out.stderr).strip().splitlines()[0] if (
                out.stdout or out.stderr) else ""
        except Exception:  # noqa: BLE001
            version = ""
    return {
        "installed": bool(path),
        "path": path or "",
        "version": version,
        "install_url": "https://ollama.com/download/windows",
        "hint": ("Ollama was not found on this computer. Install it from "
                 "https://ollama.com/download, then run `ollama pull "
                 "qwen2.5:7b-instruct` (or any model you like) and restart "
                 "JARVIS.") if not path else "",
    }
