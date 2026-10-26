"""Resource governance: the 12 GB RAM / 8 GB VRAM budget.

The guard runs on its own daemon thread, samples the process tree and the GPU,
publishes ``METRICS`` events for the status bar and drives a three-level
response when the budget gets tight:

``OK``    everything is fine.
``SOFT``  >= ``soft_threshold`` of the budget.  JARVIS starts shedding load:
          screenshots are downscaled harder, the observation cache is dropped,
          idle Ollama models are evicted from VRAM, background listening is
          paused between tasks.
``HARD``  >= 100% of the budget (or system memory is critically low).  New
          heavy operations are refused outright and the user is notified.

Nothing here is optional and nothing here can be configured above the absolute
caps declared in :mod:`jarvis.config`.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from .. import _proc
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


class Pressure(str, Enum):
    OK = "ok"
    SOFT = "soft"
    HARD = "hard"


@dataclass
class ResourceSnapshot:
    ts: float = field(default_factory=time.time)
    # --- RAM -----------------------------------------------------------
    process_ram_gb: float = 0.0        # JARVIS + its children
    system_ram_total_gb: float = 0.0
    system_ram_used_gb: float = 0.0
    system_ram_available_gb: float = 0.0
    system_ram_percent: float = 0.0
    # --- CPU -----------------------------------------------------------
    cpu_percent: float = 0.0
    process_cpu_percent: float = 0.0
    # --- GPU -----------------------------------------------------------
    gpu_name: str = ""
    gpu_available: bool = False
    vram_used_gb: float = 0.0
    vram_total_gb: float = 0.0
    gpu_util_percent: float = 0.0
    gpu_temperature_c: Optional[float] = None
    # --- derived -------------------------------------------------------
    ram_budget_gb: float = 12.0
    vram_budget_gb: float = 8.0
    ram_headroom_gb: float = 12.0
    vram_headroom_gb: float = 8.0
    pressure: Pressure = Pressure.OK
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "process_ram_gb": round(self.process_ram_gb, 2),
            "ram_budget_gb": self.ram_budget_gb,
            "ram_headroom_gb": round(self.ram_headroom_gb, 2),
            "system_ram_used_gb": round(self.system_ram_used_gb, 2),
            "system_ram_total_gb": round(self.system_ram_total_gb, 2),
            "system_ram_available_gb": round(self.system_ram_available_gb, 2),
            "system_ram_percent": round(self.system_ram_percent, 1),
            "cpu_percent": round(self.cpu_percent, 1),
            "process_cpu_percent": round(self.process_cpu_percent, 1),
            "gpu_available": self.gpu_available,
            "gpu_name": self.gpu_name,
            "vram_used_gb": round(self.vram_used_gb, 2),
            "vram_total_gb": round(self.vram_total_gb, 2),
            "vram_headroom_gb": round(self.vram_headroom_gb, 2),
            "gpu_util_percent": round(self.gpu_util_percent, 1),
            "gpu_temperature_c": self.gpu_temperature_c,
            "pressure": self.pressure.value,
            "reasons": list(self.reasons),
        }


def _bytes_to_gb(n: float) -> float:
    return float(n) / (1024 ** 3)


class GpuProbe:
    """VRAM/utilisation probe.

    Order of preference: ``pynvml`` (no subprocess cost) -> ``nvidia-smi`` ->
    unavailable.  Both are entirely local.
    """

    def __init__(self) -> None:
        self._nvml = None
        self._nvml_handles: List[Any] = []
        self._smi = shutil.which("nvidia-smi")
        if not self._smi:
            for candidate in (
                r"C:\Windows\System32\nvidia-smi.exe",
                r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
            ):
                if os.path.exists(candidate):
                    self._smi = candidate
                    break
        self._last: Dict[str, Any] = {}
        self._last_ts: float = 0.0
        self._init_nvml()

    def _init_nvml(self) -> None:
        try:
            import pynvml  # type: ignore

            pynvml.nvmlInit()
            self._nvml = pynvml
            count = pynvml.nvmlDeviceGetCount()
            self._nvml_handles = [pynvml.nvmlDeviceGetHandleByIndex(i)
                                  for i in range(count)]
        except Exception as exc:  # noqa: BLE001 - nvml is optional
            self._nvml = None
            log.debug("NVML unavailable (%s); falling back to nvidia-smi", exc)

    @property
    def available(self) -> bool:
        return bool(self._nvml or self._smi)

    def sample(self, max_age_s: float = 1.0) -> Dict[str, Any]:
        now = time.time()
        if now - self._last_ts < max_age_s and self._last:
            return self._last
        data: Dict[str, Any] = {"available": False}
        if self._nvml:
            try:
                data = self._sample_nvml()
            except Exception as exc:  # noqa: BLE001
                log.debug("NVML sample failed: %s", exc)
                data = {"available": False}
        if not data.get("available") and self._smi:
            try:
                data = self._sample_smi()
            except Exception as exc:  # noqa: BLE001
                log.debug("nvidia-smi sample failed: %s", exc)
                data = {"available": False}
        self._last = data
        self._last_ts = now
        return data

    def _sample_nvml(self) -> Dict[str, Any]:
        p = self._nvml
        used = total = 0
        util = 0
        temp: Optional[float] = None
        name = ""
        for h in self._nvml_handles:
            mem = p.nvmlDeviceGetMemoryInfo(h)
            used += mem.used
            total += mem.total
            try:
                util = max(util, p.nvmlDeviceGetUtilizationRates(h).gpu)
            except Exception:  # noqa: BLE001
                pass
            try:
                t = p.nvmlDeviceGetTemperature(h, p.NVML_TEMPERATURE_GPU)
                temp = float(t)
            except Exception:  # noqa: BLE001
                pass
            try:
                name = p.nvmlDeviceGetName(h)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
        return {
            "available": True, "name": name or "NVIDIA GPU",
            "vram_used_gb": _bytes_to_gb(used), "vram_total_gb": _bytes_to_gb(total),
            "gpu_util_percent": float(util), "temperature_c": temp, "source": "nvml",
        }

    def _sample_smi(self) -> Dict[str, Any]:
        cmd = [
            self._smi,
            "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
        out = _proc.run(cmd, capture_output=True, text=True, timeout=4)
        if out.returncode != 0:
            return {"available": False}
        line = out.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        name = parts[0]
        used_mb = float(parts[1]) if len(parts) > 1 else 0.0
        total_mb = float(parts[2]) if len(parts) > 2 else 0.0
        util = float(parts[3]) if len(parts) > 3 else 0.0
        temp = float(parts[4]) if len(parts) > 4 and parts[4] not in ("", "[N/A]") else None
        return {
            "available": True, "name": name,
            "vram_used_gb": used_mb / 1024.0, "vram_total_gb": total_mb / 1024.0,
            "gpu_util_percent": util, "temperature_c": temp, "source": "nvidia-smi",
        }


class ResourceGuard:
    """Thread-safe budget monitor with pluggable load-shedding hooks."""

    def __init__(self,
                 settings_provider: Callable[[], Any],
                 on_metrics: Optional[Callable[[ResourceSnapshot], None]] = None,
                 on_pressure: Optional[Callable[[Pressure, ResourceSnapshot], None]] = None,
                 gpu_probe: Optional[GpuProbe] = None) -> None:
        self._settings_provider = settings_provider
        self._on_metrics = on_metrics
        self._on_pressure = on_pressure
        self.gpu = gpu_probe or GpuProbe()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._latest = ResourceSnapshot()
        self._peak_ram_gb = 0.0
        self._peak_vram_gb = 0.0
        self._pressure_since: Dict[Pressure, float] = {}
        self._try_import_psutil()

    # ------------------------------------------------------------------
    def _try_import_psutil(self) -> None:
        try:
            import psutil  # noqa: F401

            self.psutil = psutil
        except Exception as exc:  # noqa: BLE001
            self.psutil = None
            log.warning("psutil unavailable (%s) - resource guard will run "
                        "with degraded accuracy.", exc)

    @property
    def settings(self):
        return self._settings_provider()

    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="jarvis-resource-guard", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            t = self._thread
            self._thread = None
        if t and t.is_alive():
            t.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            interval = 1.5
            try:
                sec = self.settings.security
                interval = max(0.5, sec.resource_poll_ms / 1000.0)
                snap = self.sample()
                self._evaluate(snap)
            except Exception:  # noqa: BLE001 - the guard must never die
                log.exception("Resource guard iteration failed")
            self._stop.wait(interval)

    # ------------------------------------------------------------------
    def sample(self) -> ResourceSnapshot:
        sec = self.settings.security
        snap = ResourceSnapshot(ram_budget_gb=sec.max_ram_gb,
                                vram_budget_gb=sec.max_vram_gb)
        if self.psutil is not None:
            try:
                proc = self.psutil.Process()
                rss = proc.memory_info().rss
                for child in proc.children(recursive=True):
                    try:
                        rss += child.memory_info().rss
                    except Exception:  # noqa: BLE001
                        pass
                snap.process_ram_gb = _bytes_to_gb(rss)
                try:
                    snap.process_cpu_percent = float(proc.cpu_percent(interval=None))
                except Exception:  # noqa: BLE001
                    snap.process_cpu_percent = 0.0
            except Exception as exc:  # noqa: BLE001
                log.debug("Process sampling failed: %s", exc)
            try:
                vm = self.psutil.virtual_memory()
                snap.system_ram_total_gb = _bytes_to_gb(vm.total)
                snap.system_ram_used_gb = _bytes_to_gb(vm.total - vm.available)
                snap.system_ram_available_gb = _bytes_to_gb(vm.available)
                snap.system_ram_percent = float(vm.percent)
                snap.cpu_percent = float(self.psutil.cpu_percent(interval=None))
            except Exception as exc:  # noqa: BLE001
                log.debug("System sampling failed: %s", exc)
        else:
            snap.system_ram_total_gb, snap.system_ram_available_gb = _fallback_memory()
            snap.system_ram_used_gb = max(
                0.0, snap.system_ram_total_gb - snap.system_ram_available_gb)
            if snap.system_ram_total_gb:
                snap.system_ram_percent = 100.0 * snap.system_ram_used_gb / snap.system_ram_total_gb
            snap.process_ram_gb = _fallback_process_rss_gb()
            snap.cpu_percent = _fallback_cpu_percent()

        gpu = self.gpu.sample(max_age_s=1.0)
        if gpu.get("available"):
            snap.gpu_available = True
            snap.gpu_name = gpu.get("name", "")
            snap.vram_used_gb = float(gpu.get("vram_used_gb", 0.0))
            snap.vram_total_gb = float(gpu.get("vram_total_gb", 0.0))
            snap.gpu_util_percent = float(gpu.get("gpu_util_percent", 0.0))
            snap.gpu_temperature_c = gpu.get("temperature_c")

        snap.ram_headroom_gb = max(0.0, snap.ram_budget_gb - snap.process_ram_gb)
        snap.vram_headroom_gb = max(0.0, snap.vram_budget_gb - snap.vram_used_gb)

        with self._lock:
            self._peak_ram_gb = max(self._peak_ram_gb, snap.process_ram_gb)
            self._peak_vram_gb = max(self._peak_vram_gb, snap.vram_used_gb)
            self._latest = snap
        return snap

    def _evaluate(self, snap: ResourceSnapshot) -> None:
        sec = self.settings.security
        soft = float(sec.soft_threshold)
        reasons: List[str] = []
        pressure = Pressure.OK

        if snap.process_ram_gb >= snap.ram_budget_gb:
            pressure = Pressure.HARD
            reasons.append(
                f"JARVIS is using {snap.process_ram_gb:.1f} GB of RAM, at or "
                f"above the {snap.ram_budget_gb:.0f} GB hard ceiling.")
        elif snap.process_ram_gb >= snap.ram_budget_gb * soft:
            pressure = Pressure.SOFT
            reasons.append(
                f"RAM usage {snap.process_ram_gb:.1f} GB is approaching the "
                f"{snap.ram_budget_gb:.0f} GB ceiling.")

        if snap.gpu_available:
            if snap.vram_used_gb >= snap.vram_budget_gb:
                pressure = Pressure.HARD
                reasons.append(
                    f"GPU memory {snap.vram_used_gb:.1f} GB is at or above the "
                    f"{snap.vram_budget_gb:.0f} GB ceiling.")
            elif snap.vram_used_gb >= snap.vram_budget_gb * soft and pressure is not Pressure.HARD:
                pressure = Pressure.SOFT
                reasons.append(
                    f"GPU memory {snap.vram_used_gb:.1f} GB is approaching the "
                    f"{snap.vram_budget_gb:.0f} GB ceiling.")

        # System-wide starvation also counts: never push the user's machine
        # into swapping just to stay inside our own budget.
        if snap.system_ram_available_gb < 1.0:
            pressure = Pressure.HARD
            reasons.append(
                f"Only {snap.system_ram_available_gb:.1f} GB of system RAM is "
                "free; JARVIS is backing off to keep Windows responsive.")
        elif snap.system_ram_available_gb < 2.0 and pressure is Pressure.OK:
            pressure = Pressure.SOFT
            reasons.append("System memory is getting low.")

        snap.pressure = pressure
        snap.reasons = reasons

        with self._lock:
            prev = Pressure.OK
            for p in (Pressure.HARD, Pressure.SOFT):
                if p in self._pressure_since:
                    prev = p
                    break
            if pressure is Pressure.OK:
                self._pressure_since.clear()
            else:
                self._pressure_since.setdefault(pressure, time.time())

        if self._on_metrics:
            try:
                self._on_metrics(snap)
            except Exception:  # noqa: BLE001
                log.exception("metrics callback failed")

        if pressure is not Pressure.OK and self._on_pressure:
            try:
                self._on_pressure(pressure, snap)
            except Exception:  # noqa: BLE001
                log.exception("pressure callback failed")

    # ------------------------------------------------------------------
    # API used by the agent / vision / speech layers
    # ------------------------------------------------------------------
    @property
    def latest(self) -> ResourceSnapshot:
        with self._lock:
            return self._latest

    @property
    def pressure(self) -> Pressure:
        return self.latest.pressure

    @property
    def peaks(self) -> Dict[str, float]:
        with self._lock:
            return {"ram_gb": self._peak_ram_gb, "vram_gb": self._peak_vram_gb}

    def can_allocate(self, estimated_gb: float) -> bool:
        """Gate for heavy operations (loading a whisper model, a new LLM...)."""
        snap = self.latest
        if snap.pressure is Pressure.HARD:
            return False
        if snap.process_ram_gb + estimated_gb > snap.ram_budget_gb:
            return False
        if snap.system_ram_available_gb - estimated_gb < 0.75:
            return False
        return True

    def may_run_vision(self) -> bool:
        """Vision is the most expensive optional step - shed it first."""
        snap = self.latest
        if snap.pressure is Pressure.HARD:
            return False
        return snap.process_ram_gb < snap.ram_budget_gb * 0.9

    def recommended_image_size(self) -> int:
        """Downscale screenshots harder as pressure rises."""
        base = int(self.settings.vision.max_dimension)
        p = self.latest.pressure
        if p is Pressure.HARD:
            return min(base, 512)
        if p is Pressure.SOFT:
            return min(base, 896)
        return base

    def release_memory(self) -> Dict[str, Any]:
        """Best-effort memory release.  Called when pressure rises."""
        import gc

        report: Dict[str, Any] = {"gc_collected": 0, "trimmed_paths": []}
        try:
            report["gc_collected"] = gc.collect()
        except Exception:  # noqa: BLE001
            pass
        try:
            from .. import paths

            removed = paths.prune_scratch(max_files=3, max_age_seconds=0)
            report["trimmed_paths"] = [f"screenshots removed: {removed}"]
        except Exception:  # noqa: BLE001
            log.debug("scratch prune failed", exc_info=True)
        return report


def _fallback_memory() -> tuple:
    """Read MemTotal/MemAvailable from /proc when psutil is missing."""
    total = avail = 0.0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = float(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    avail = float(line.split()[1]) * 1024
                if total and avail:
                    break
    except OSError:
        pass
    return _bytes_to_gb(total), _bytes_to_gb(avail)


def _fallback_process_rss_gb() -> float:
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KB, macOS reports bytes.
        if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
            return _bytes_to_gb(rss)
        return rss / (1024 ** 2)
    except Exception:  # noqa: BLE001
        return 0.0


def _fallback_cpu_percent() -> float:
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as fh:
            load = float(fh.read().split()[0])
        return min(100.0, 100.0 * load / max(1, os.cpu_count() or 1))
    except Exception:  # noqa: BLE001
        return 0.0
