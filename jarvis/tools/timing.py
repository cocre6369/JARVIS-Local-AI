"""Waiting, done properly.

Fixed sleeps are how an assistant becomes both slow *and* unreliable: too short
and it acts before the window has appeared, too long and every task drags.  So
JARVIS waits *adaptively* - it polls something cheap until the desktop stops
changing, and gives up at a bounded deadline.

Everything here reads live settings, so the Automation page takes effect
immediately:

``automation.adaptive_waits``       poll instead of sleeping blind
``automation.settle_poll_interval_s``  how often to poll
``automation.generic_wait_s``       the default "let it settle" pause
``automation.max_wait_s``           the hard deadline for any single wait
``automation.click_settle_s``       the pause after a click
``automation.app_launch_wait_s``    the pause after launching an app

None of these functions raise: a wait that cannot be measured degrades to the
plain sleep it replaced.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

#: How many identical samples in a row count as "the screen has settled".
STABLE_SAMPLES = 2


def _automation(ctx: Any) -> Any:
    settings = getattr(ctx, "settings", None)
    if settings is None:
        provider = getattr(ctx, "settings_provider", None)
        settings = provider() if callable(provider) else None
    return getattr(settings, "automation", None)


def _float(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def generic_wait(ctx: Any) -> float:
    """The default settle time when a tool has no better idea."""
    automation = _automation(ctx)
    return _float(getattr(automation, "generic_wait_s", 0.8), 0.8)


def max_wait(ctx: Any) -> float:
    automation = _automation(ctx)
    return _float(getattr(automation, "max_wait_s", 30.0), 30.0)


def poll_interval(ctx: Any) -> float:
    automation = _automation(ctx)
    return min(0.5, max(0.005, _float(getattr(automation, "settle_poll_interval_s",
                                              0.04), 0.04)))


def adaptive(ctx: Any) -> bool:
    automation = _automation(ctx)
    return bool(getattr(automation, "adaptive_waits", True))


def default_sample(ctx: Any) -> Callable[[], str]:
    """A cheap fingerprint of 'what the desktop looks like right now'.

    Window titles plus the focused control's name are enough to notice that a
    dialog appeared, a page finished loading, or nothing happened at all - and
    cost microseconds compared with a screenshot.
    """
    platform = getattr(ctx, "platform", None)

    def _sample() -> str:
        parts = []
        try:
            active = platform.windows.active()
            if active is not None:
                parts.append(str(getattr(active, "title", "")))
                parts.append(str(getattr(active, "process_name", "")))
        except Exception:  # noqa: BLE001
            pass
        try:
            focused = platform.uia.focused()
            if focused is not None:
                parts.append(str(getattr(focused, "name", "")))
                parts.append(str(getattr(focused, "value", ""))[:40])
        except Exception:  # noqa: BLE001
            pass
        return "|".join(parts)

    return _sample


def settle(ctx: Any, base_s: Optional[float] = None,
           sample: Optional[Callable[[], str]] = None,
           reason: str = "", timeout_s: Optional[float] = None) -> float:
    """Wait for the desktop to stop changing.  Returns the seconds waited.

    With adaptive waits off (or no way to sample) this is exactly the old
    ``time.sleep(base_s)`` - the caller's behaviour is unchanged, just
    configurable.
    """
    base = _float(base_s if base_s is not None else generic_wait(ctx), 0.05)
    if not adaptive(ctx) or sample is None and ctx is None:
        time.sleep(base)
        return base
    sampler = sample or default_sample(ctx)
    deadline_budget = _float(timeout_s, max(base * 2.0, base + 0.5))
    deadline_budget = min(deadline_budget, max(base, max_wait(ctx)))
    interval = poll_interval(ctx)
    started = time.time()
    last = ""
    stable = 0
    changed_once = False
    try:
        last = sampler()
    except Exception:  # noqa: BLE001
        time.sleep(base)
        return base
    while True:
        elapsed = time.time() - started
        if elapsed >= base and stable >= STABLE_SAMPLES and (changed_once or
                                                             elapsed >= base):
            break
        if elapsed >= deadline_budget:
            break
        time.sleep(interval)
        try:
            current = sampler()
        except Exception:  # noqa: BLE001
            break
        if current == last:
            stable += 1
        else:
            stable = 0
            changed_once = True
            last = current
    waited = time.time() - started
    if reason:
        log.debug("settle(%s): waited %.3fs (base %.3fs, adaptive=%s)",
                  reason, waited, base, changed_once)
    return waited


def wait_until(predicate: Callable[[], bool], ctx: Any,
               timeout_s: Optional[float] = None,
               reason: str = "") -> bool:
    """Poll ``predicate`` until it is true or the deadline passes."""
    budget = _float(timeout_s, max_wait(ctx))
    budget = min(budget, max_wait(ctx))
    interval = poll_interval(ctx)
    deadline = time.time() + budget
    while True:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            log.debug("wait_until(%s) predicate failed", reason, exc_info=True)
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def click_settle(ctx: Any, sample: Optional[Callable[[], str]] = None) -> float:
    """The pause after a click: long enough for the UI, short enough to feel fast."""
    automation = _automation(ctx)
    base = _float(getattr(automation, "click_settle_s", 0.18), 0.18)
    return settle(ctx, base_s=base, sample=sample, reason="click")


def launch_settle(ctx: Any, sample: Optional[Callable[[], str]] = None) -> float:
    """The pause after launching an application."""
    automation = _automation(ctx)
    base = _float(getattr(automation, "app_launch_wait_s", 2.5), 2.5)
    return settle(ctx, base_s=base, sample=sample, reason="launch",
                  timeout_s=min(max_wait(ctx), base * 4.0))


__all__ = ["STABLE_SAMPLES", "adaptive", "click_settle", "default_sample",
           "generic_wait", "launch_settle", "max_wait", "poll_interval", "settle",
           "wait_until"]
