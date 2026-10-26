"""Local-only network policy.

JARVIS has no cloud AI backend, no telemetry and no upload path.  The only
HTTP client in the application talks to the local Ollama server.  This module
makes that a *checked* invariant rather than an assumption:

* :func:`assert_local_endpoint` is called by
  :class:`jarvis.llm.ollama_client.OllamaClient` before every connection.
* :func:`audit_open_sockets` can be invoked from the settings page to prove
  nothing unexpected is talking to the internet.

The user's own browser is a different matter - JARVIS may *ask the browser* to
open a public web page (that is the user's network traffic, in the user's
browser), but JARVIS itself never POSTs your data anywhere.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import List, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


class NetworkPolicyViolation(RuntimeError):
    """Raised when code attempts to reach a non-local AI/telemetry endpoint."""


def is_local_hostname(host: str) -> bool:
    if not host:
        return False
    host = host.strip().lower().rstrip(".")
    if host in LOCAL_HOSTNAMES:
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_link_local


def is_private_hostname(host: str) -> bool:
    """Loopback OR RFC1918 - allows a LAN-hosted Ollama box."""
    if is_local_hostname(host):
        return True
    try:
        ip = ipaddress.ip_address(host.strip().lower().rstrip("."))
    except ValueError:
        return False
    return ip.is_private


def assert_local_endpoint(url: str, allow_private_lan: bool = True) -> str:
    """Validate an endpoint JARVIS itself will connect to.

    Returns the normalised host.  Raises :class:`NetworkPolicyViolation`
    otherwise.  DNS is *not* resolved here on purpose - resolving a hostile
    name is itself a (small) information leak; the check is on the literal.
    """
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = (parsed.hostname or "").strip().lower()
    scheme = (parsed.scheme or "http").lower()
    if scheme not in ("http", "https"):
        raise NetworkPolicyViolation(f"Unsupported scheme '{scheme}' for {url}")
    if not host:
        raise NetworkPolicyViolation(f"No host in endpoint '{url}'")
    if is_local_hostname(host):
        return host
    if allow_private_lan and is_private_hostname(host):
        log.warning("Ollama endpoint %s is on a private LAN address, not "
                    "loopback. Traffic stays inside your network but is not "
                    "loopback-only.", host)
        return host
    raise NetworkPolicyViolation(
        f"Refusing to contact non-local endpoint '{host}'. JARVIS is "
        "local-only: no cloud AI, no telemetry, no uploads. Point the Ollama "
        "setting at 127.0.0.1 (or a private LAN address) instead."
    )


def resolve_host_ip(host: str) -> Optional[str]:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def audit_open_sockets(include_all: bool = False) -> List[dict]:
    """List the network connections owned by this process (and its children).

    Used by the Privacy page to *prove* JARVIS is not phoning home.
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return []
    out: List[dict] = []
    procs = [psutil.Process()]
    try:
        procs.extend(psutil.Process().children(recursive=True))
    except Exception:  # noqa: BLE001
        pass
    for proc in procs:
        try:
            for c in proc.net_connections(kind="inet"):
                remote = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else ""
                local = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else ""
                if not include_all and not remote:
                    continue
                out.append({
                    "pid": proc.pid,
                    "status": c.status,
                    "local": local,
                    "remote": remote,
                    "remote_is_local": is_private_hostname(c.raddr.ip) if c.raddr else True,
                })
        except Exception:  # noqa: BLE001 - permission errors are normal
            continue
    return out


_URL_RE = re.compile(r"https?://[^\s\"'<>)]+")


def find_remote_urls(text: str) -> List[str]:
    """Every non-local URL mentioned in a string (used by the policy engine)."""
    out = []
    for u in _URL_RE.findall(text or ""):
        try:
            host = urlparse(u).hostname or ""
        except ValueError:
            continue
        if host and not is_private_hostname(host):
            out.append(u)
    return out
