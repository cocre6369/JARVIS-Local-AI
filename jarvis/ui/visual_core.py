"""Geometry and animation maths for the circular AI core visualiser.

Kept completely free of Qt so the animation can be unit tested headlessly and so
the widget that paints it stays a thin shell.  Everything here is a pure
function of ``(state, time, level)`` - no hidden globals, no threading - which
means the same numbers drive the QPainter widget and the tests.

The look: three counter-rotating segmented rings, a breathing reactor core, an
audio-reactive outer halo, and a small swarm of sparks that speeds up while
JARVIS is working.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

TWO_PI = math.pi * 2.0

#: Per-state animation parameters: ring speeds (rad/s), pulse rate (Hz),
#: base brightness (0..1) and how strongly the audio level pushes the halo.
STATE_ANIMATION: Dict[str, Dict[str, float]] = {
    "idle":                 {"speed": 0.20, "pulse_hz": 0.35, "brightness": 0.55, "level_gain": 0.25, "sparks": 6},
    "listening":            {"speed": 0.55, "pulse_hz": 0.90, "brightness": 0.75, "level_gain": 1.30, "sparks": 10},
    "thinking":             {"speed": 1.10, "pulse_hz": 1.60, "brightness": 0.85, "level_gain": 0.35, "sparks": 16},
    "executing":            {"speed": 1.70, "pulse_hz": 2.10, "brightness": 0.95, "level_gain": 0.45, "sparks": 22},
    "observing":            {"speed": 0.90, "pulse_hz": 1.20, "brightness": 0.80, "level_gain": 0.55, "sparks": 12},
    "speaking":             {"speed": 0.60, "pulse_hz": 1.10, "brightness": 0.88, "level_gain": 1.60, "sparks": 12},
    "waiting_for_approval": {"speed": 0.30, "pulse_hz": 0.70, "brightness": 0.70, "level_gain": 0.20, "sparks": 4},
    "error":                {"speed": 2.40, "pulse_hz": 3.00, "brightness": 1.00, "level_gain": 0.30, "sparks": 26},
}
DEFAULT_STATE = "idle"


@dataclass
class Ring:
    """One segmented ring of the reactor."""

    radius: float
    width: float
    colour: str
    alpha: float
    rotation: float            # radians
    segments: int = 3
    gap_deg: float = 26.0
    dash: bool = False

    def arcs(self) -> List[Tuple[float, float]]:
        """Start/end angles in degrees for each visible segment."""
        if self.segments <= 0:
            return [(0.0, 360.0)]
        step = 360.0 / self.segments
        base = math.degrees(self.rotation) % 360.0
        out = []
        for i in range(self.segments):
            start = base + i * step + self.gap_deg / 2.0
            end = base + (i + 1) * step - self.gap_deg / 2.0
            out.append((start % 360.0, end % 360.0))
        return out


@dataclass
class Spark:
    index: int
    angle: float
    radius: float
    size: float
    alpha: float


@dataclass
class CoreFrame:
    """Everything needed to paint one frame."""

    size: float
    centre: Tuple[float, float]
    state: str
    colour: str
    brightness: float
    rings: List[Ring] = field(default_factory=list)
    core_radius: float = 0.0
    glow_radius: float = 0.0
    halo_radius: float = 0.0
    halo_alpha: float = 0.0
    sweep_start: float = 0.0
    sweep_span: float = 0.0
    sparks: List[Spark] = field(default_factory=list)
    level: float = 0.0
    label: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"size": self.size, "state": self.state, "colour": self.colour,
                "brightness": round(self.brightness, 3),
                "core_radius": round(self.core_radius, 2),
                "glow_radius": round(self.glow_radius, 2),
                "halo_radius": round(self.halo_radius, 2),
                "rings": len(self.rings), "sparks": len(self.sparks),
                "level": round(self.level, 3), "label": self.label}


def clamp01(value: float) -> float:
    return 0.0 if value <= 0 else (1.0 if value >= 1.0 else value)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * clamp01(t)


def mix_hex(first: str, second: str, t: float) -> str:
    """Blend two ``#rrggbb`` colours.  Used for the core-to-halo gradient."""
    def parse(value: str) -> Tuple[int, int, int]:
        v = (value or "").lstrip("#")
        if len(v) == 3:
            v = "".join(c * 2 for c in v)
        if len(v) != 6:
            return (255, 255, 255)
        try:
            return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
        except ValueError:
            return (255, 255, 255)

    r1, g1, b1 = parse(first)
    r2, g2, b2 = parse(second)
    k = clamp01(t)
    return "#{:02x}{:02x}{:02x}".format(int(lerp(r1, r2, k)), int(lerp(g1, g2, k)),
                                        int(lerp(b1, b2, k)))


def state_params(state: str) -> Dict[str, float]:
    return STATE_ANIMATION.get(state or DEFAULT_STATE, STATE_ANIMATION[DEFAULT_STATE])


def build_frame(state: str,
                t: float,
                size: float,
                colour: str,
                level: float = 0.0,
                accent_secondary: str = "#3b82f6",
                label: str = "",
                animation_speed: float = 1.0) -> CoreFrame:
    """Compute one frame.  ``t`` is seconds since start; ``level`` is 0..1 audio.

    ``animation_speed`` comes from the UI settings and lets the user turn
    animation off entirely (0) for a static, cheap-to-draw core.
    """
    size = max(24.0, float(size))
    params = state_params(state)
    speed = max(0.0, float(animation_speed))
    tt = float(t) * speed
    level = clamp01(float(level))
    centre = (size / 2.0, size / 2.0)
    radius = size / 2.0

    brightness = clamp01(params["brightness"] * (0.85 + 0.15 * math.sin(tt * 1.7)))
    pulse = 0.5 + 0.5 * math.sin(TWO_PI * params["pulse_hz"] * tt)

    core_radius = radius * lerp(0.18, 0.26, pulse) * (1.0 + 0.18 * level)
    glow_radius = radius * lerp(0.30, 0.42, pulse) * (1.0 + 0.35 * level)
    halo_radius = radius * lerp(0.86, 0.97, level * params["level_gain"])
    halo_alpha = clamp01(0.10 + 0.35 * level * params["level_gain"] + 0.08 * pulse)

    rings = [
        Ring(radius=radius * 0.92, width=max(1.5, radius * 0.030),
             colour=mix_hex(colour, accent_secondary, 0.35),
             alpha=clamp01(0.35 + 0.30 * brightness),
             rotation=tt * params["speed"], segments=3, gap_deg=26.0),
        Ring(radius=radius * 0.74, width=max(1.2, radius * 0.022),
             colour=colour, alpha=clamp01(0.30 + 0.35 * brightness),
             rotation=-tt * params["speed"] * 1.6, segments=4, gap_deg=18.0),
        Ring(radius=radius * 0.56, width=max(1.0, radius * 0.016),
             colour=mix_hex(colour, "#ffffff", 0.25),
             alpha=clamp01(0.22 + 0.30 * brightness),
             rotation=tt * params["speed"] * 2.3, segments=6, gap_deg=12.0,
             dash=True),
    ]

    # Radar-style sweep: one arc that keeps rotating, wider while working.
    sweep_start = (math.degrees(tt * params["speed"] * 1.15)) % 360.0
    sweep_span = lerp(38.0, 96.0, clamp01(params["speed"] / 2.4)) * (0.7 + 0.3 * pulse)

    spark_count = int(params["sparks"])
    sparks: List[Spark] = []
    for i in range(spark_count):
        seed = i * 2.399963          # golden-angle spread, deterministic
        angle = (seed + tt * (params["speed"] * (0.6 + (i % 5) * 0.25))) % TWO_PI
        radial = 0.42 + 0.5 * abs(math.sin(seed * 1.7 + tt * 0.9))
        alpha = clamp01(0.25 + 0.6 * abs(math.sin(seed + tt * 2.1)) * brightness)
        sparks.append(Spark(index=i, angle=angle, radius=radius * radial,
                            size=max(1.0, radius * 0.018), alpha=alpha))

    return CoreFrame(size=size, centre=centre, state=state, colour=colour,
                     brightness=brightness, rings=rings, core_radius=core_radius,
                     glow_radius=glow_radius, halo_radius=halo_radius,
                     halo_alpha=halo_alpha, sweep_start=sweep_start,
                     sweep_span=sweep_span, sparks=sparks, level=level,
                     label=label)


def ring_path_points(ring: Ring, centre: Tuple[float, float],
                     samples: int = 64) -> List[Tuple[float, float]]:
    """Sample a ring's visible arcs into points (used by tests and by the
    Qt-free preview renderer)."""
    cx, cy = centre
    points: List[Tuple[float, float]] = []
    for start, end in ring.arcs():
        if end < start:
            end += 360.0
        steps = max(2, int(samples * (end - start) / 360.0))
        for i in range(steps + 1):
            angle = math.radians(start + (end - start) * i / steps)
            points.append((cx + ring.radius * math.cos(angle),
                           cy + ring.radius * math.sin(angle)))
    return points


def spark_positions(frame: CoreFrame) -> List[Tuple[float, float]]:
    cx, cy = frame.centre
    return [(cx + s.radius * math.cos(s.angle), cy + s.radius * math.sin(s.angle))
            for s in frame.sparks]


def frame_rate_for(state: str, animation_speed: float) -> int:
    """Timer interval in ms for the widget.  Slower states repaint less often,
    which keeps the UI cheap while JARVIS is idle."""
    if animation_speed <= 0:
        return 0                       # static: no timer at all
    params = state_params(state)
    hz = 24.0 + 26.0 * clamp01(params["speed"] / 2.4)
    return max(16, int(1000.0 / hz))
