"""Native Windows audio control (master volume / mute).

Implemented directly against the Core Audio COM interfaces with ``ctypes`` so
there is no extra dependency to package.  If anything goes wrong - no audio
device, an unusual driver, a non-Windows host - the caller falls back to
synthetic media keys, and reports which path it used.
"""

from __future__ import annotations

import ctypes
import logging
import os
from typing import Any, Optional, Tuple

log = logging.getLogger(__name__)

_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAudioEndpointVolume = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
_eRender = 0
_eMultimedia = 1
_CLSCTX_INPROC_SERVER = 0x1


def _guid(text: str) -> Any:
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    g = GUID()
    ctypes.windll.ole32.CLSIDFromString(text, ctypes.byref(g))  # type: ignore[attr-defined]
    return g


class WindowsAudio:
    """Master volume through IAudioEndpointVolume."""

    name = "core-audio"

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Core Audio requires Windows")
        self._ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
        self._ole32.CoInitializeEx(None, 0)
        self._enumerator = self._create_enumerator()
        self._endpoint = None

    # ------------------------------------------------------------------
    def _create_enumerator(self) -> Any:
        clsid = _guid(_CLSID_MMDeviceEnumerator)
        iid = _guid(_IID_IMMDeviceEnumerator)
        ptr = ctypes.c_void_p()
        hr = self._ole32.CoCreateInstance(ctypes.byref(clsid), None,
                                          _CLSCTX_INPROC_SERVER, ctypes.byref(iid),
                                          ctypes.byref(ptr))
        if hr != 0 or not ptr.value:
            raise OSError(f"CoCreateInstance(MMDeviceEnumerator) failed hr={hr:#x}")
        return ptr

    def _vtbl(self, iface: ctypes.c_void_p) -> Any:
        return ctypes.cast(iface, ctypes.POINTER(ctypes.c_void_p))[0]

    def _call(self, iface: ctypes.c_void_p, index: int, restype: Any,
              *argtypes_and_values: Tuple[Any, Any]) -> Any:
        vtbl = self._vtbl(iface)
        fn_ptr = ctypes.cast(
            ctypes.c_void_p(ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]),
            ctypes.CFUNCTYPE(restype, ctypes.c_void_p,
                             *[t for t, _ in argtypes_and_values]))
        return fn_ptr(iface, *[v for _, v in argtypes_and_values])

    def _endpoint_volume(self) -> Optional[ctypes.c_void_p]:
        if self._endpoint is not None:
            return self._endpoint
        try:
            device = ctypes.c_void_p()
            # IMMDeviceEnumerator::GetDefaultAudioEndpoint is vtable index 4
            hr = self._call(self._enumerator, 4, ctypes.c_long,
                            (ctypes.c_int, _eRender), (ctypes.c_int, _eMultimedia),
                            (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(device)))
            if hr != 0 or not device.value:
                return None
            iid = _guid(_IID_IAudioEndpointVolume)
            volume = ctypes.c_void_p()
            # IMMDevice::Activate is vtable index 3
            hr = self._call(device, 3, ctypes.c_long,
                            (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(iid)),
                            (ctypes.c_ulong, _CLSCTX_INPROC_SERVER),
                            (ctypes.c_void_p, None),
                            (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(volume)))
            if hr != 0 or not volume.value:
                return None
            self._endpoint = volume
            return volume
        except Exception as exc:  # noqa: BLE001
            log.debug("Core Audio activation failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    def get_volume(self) -> Optional[float]:
        vol = self._endpoint_volume()
        if vol is None:
            return None
        try:
            value = ctypes.c_float(0.0)
            hr = self._call(vol, 13, ctypes.c_long,
                            (ctypes.POINTER(ctypes.c_float), ctypes.byref(value)))
            return round(value.value * 100.0, 1) if hr == 0 else None
        except Exception as exc:  # noqa: BLE001
            log.debug("get volume failed: %s", exc)
            return None

    def set_volume(self, percent: float) -> Optional[float]:
        vol = self._endpoint_volume()
        if vol is None:
            return None
        percent = max(0.0, min(100.0, float(percent)))
        try:
            scalar = ctypes.c_float(percent / 100.0)
            hr = self._call(vol, 11, ctypes.c_long,
                            (ctypes.c_float, scalar), (ctypes.c_void_p, None))
            return self.get_volume() if hr == 0 else None
        except Exception as exc:  # noqa: BLE001
            log.debug("set volume failed: %s", exc)
            return None

    def get_mute(self) -> Optional[bool]:
        vol = self._endpoint_volume()
        if vol is None:
            return None
        try:
            value = ctypes.c_bool(False)
            hr = self._call(vol, 15, ctypes.c_long,
                            (ctypes.POINTER(ctypes.c_bool), ctypes.byref(value)))
            return bool(value.value) if hr == 0 else None
        except Exception as exc:  # noqa: BLE001
            log.debug("get mute failed: %s", exc)
            return None

    def set_mute(self, mute: bool) -> Optional[bool]:
        vol = self._endpoint_volume()
        if vol is None:
            return None
        try:
            hr = self._call(vol, 14, ctypes.c_long,
                            (ctypes.c_bool, bool(mute)), (ctypes.c_void_p, None))
            return self.get_mute() if hr == 0 else None
        except Exception as exc:  # noqa: BLE001
            log.debug("set mute failed: %s", exc)
            return None

    def step(self, up: bool = True, steps: int = 2) -> Optional[float]:
        vol = self._endpoint_volume()
        if vol is None:
            return None
        try:
            index = 17 if up else 18
            for _ in range(max(1, min(int(steps), 50))):
                self._call(vol, index, ctypes.c_long, (ctypes.c_void_p, None))
            return self.get_volume()
        except Exception as exc:  # noqa: BLE001
            log.debug("volume step failed: %s", exc)
            return None

    def close(self) -> None:
        try:
            if self._endpoint is not None:
                self._call(self._endpoint, 2, ctypes.c_long)   # Release
            self._call(self._enumerator, 2, ctypes.c_long)
        except Exception:  # noqa: BLE001
            pass
        self._endpoint = None


def create_audio_backend() -> Tuple[Any, str]:
    try:
        return WindowsAudio(), "Windows Core Audio (IAudioEndpointVolume)"
    except Exception as exc:  # noqa: BLE001
        return None, f"Core Audio unavailable ({exc}); media keys will be used"
