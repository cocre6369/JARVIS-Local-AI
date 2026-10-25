"""Helpers for talking to WinRT APIs from synchronous code.

JARVIS's cores (agent loop, tools) are thread based, not asyncio based, but
WinRT operations are asynchronous.  ``await_op`` bridges the gap and copes with
the three different projection flavours that have shipped over the years
(``winrt.system.await``, plain awaitables, and ``add_completed`` callbacks) so a
Windows update or a different wheel version cannot break OCR or media control.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


def import_winrt(module_paths) -> Optional[Any]:
    """Import the first module path that works."""
    import importlib

    if isinstance(module_paths, str):
        module_paths = [module_paths]
    last: Optional[Exception] = None
    for path in module_paths:
        try:
            return importlib.import_module(path)
        except Exception as exc:  # noqa: BLE001
            last = exc
    log.debug("WinRT import failed for %s: %s", module_paths, last)
    return None


def await_op(op: Any, timeout: float = 20.0) -> Any:
    """Await a WinRT IAsyncOperation and return its result."""
    if op is None:
        return None
    # Already a plain value?
    if not hasattr(op, "get_results"):
        return op

    # 1. modern projections expose winrt.system.await
    try:
        system = import_winrt("winrt.system")
        fn = getattr(system, "await", None) if system is not None else None
        if fn is not None:
            return fn(op)
    except Exception as exc:  # noqa: BLE001
        log.debug("winrt.system.await unavailable: %s", exc)

    # 2. asyncio
    try:
        return asyncio.run(_coro(op, timeout))
    except RuntimeError:
        # An event loop is already running on this thread (e.g. called from Qt).
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_coro(op, timeout))
            finally:
                loop.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("asyncio bridge failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.debug("asyncio bridge failed: %s", exc)

    # 3. completed callback
    box: Dict[str, Any] = {}
    event = threading.Event()

    def _done(_op: Any, _status: Any) -> None:
        try:
            box["result"] = op.get_results()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            event.set()

    try:
        op.add_completed(_done)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"cannot await WinRT operation: {exc}") from exc
    if not event.wait(timeout):
        raise TimeoutError("WinRT operation timed out")
    if "error" in box:
        raise box["error"]
    return box.get("result")


async def _coro(op: Any, timeout: float) -> Any:
    return await asyncio.wait_for(op, timeout=timeout)
