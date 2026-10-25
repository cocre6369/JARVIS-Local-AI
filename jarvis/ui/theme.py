"""Look and feel: colours, metrics and the generated stylesheet.

Deliberately **Qt-free** so it can be imported (and unit tested) on a machine
with no display server.  :func:`stylesheet` returns the QSS text that
``QApplication.setStyleSheet`` consumes.

The visual language is the one the design asks for: near-black backgrounds,
hairline borders, a cyan primary accent, and a single "reactor" gradient used by
the core visualiser and the active navigation item.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
#: Iron Man style palette.  Keys are stable; the UI reads them by name.
PALETTE: Dict[str, str] = {
    "bg": "#05080d",              # window background
    "bg_alt": "#080d14",          # panels
    "bg_raised": "#0d1520",       # cards, inputs
    "bg_hover": "#121c29",
    "border": "#1b2836",
    "border_bright": "#2b4257",
    "text": "#e6f1ff",
    "text_dim": "#8ea3b8",
    "text_muted": "#5d7288",
    "accent": "#22d3ee",          # cyan - primary
    "accent_dim": "#0e7490",
    "accent_secondary": "#3b82f6",
    "success": "#34d399",
    "warning": "#fbbf24",
    "danger": "#f87171",
    "info": "#60a5fa",
    "reactor_core": "#eafcff",
    "reactor_ring": "#22d3ee",
    "reactor_outer": "#0b3b4d",
    "shadow": "#000000",
}

#: What each agent state means for the visualiser and the status bar.
STATE_COLOURS: Dict[str, Tuple[str, str]] = {
    # state -> (colour, human label)
    "idle": ("#22d3ee", "Idle"),
    "listening": ("#60a5fa", "Listening"),
    "thinking": ("#a78bfa", "Thinking"),
    "executing": ("#fbbf24", "Acting"),
    "observing": ("#34d399", "Observing"),
    "speaking": ("#22d3ee", "Speaking"),
    "waiting_for_approval": ("#f87171", "Waiting for your approval"),
    "error": ("#f87171", "Error"),
}

METRICS: Dict[str, float] = {
    "radius": 10.0,
    "padding": 12.0,
    "sidebar_width": 210.0,
    "core_size": 168.0,
    "row_height": 34.0,
    "input_height": 44.0,
    "border_width": 1.0,
}


@dataclass
class Theme:
    """Resolved theme for one run (settings may override the accent)."""

    palette: Dict[str, str]
    metrics: Dict[str, float]
    font_family: str = "Segoe UI"
    mono_font_family: str = "Cascadia Mono"
    font_size: int = 11
    accent: str = "#22d3ee"
    accent_secondary: str = "#3b82f6"
    #: 0 disables animation entirely (the core is drawn once, no timer).
    animation_speed: float = 1.0

    @classmethod
    def from_settings(cls, settings: Any) -> "Theme":
        ui = getattr(settings, "ui", None)
        palette = dict(PALETTE)
        accent = str(getattr(ui, "accent", "") or "").strip() or palette["accent"]
        accent2 = str(getattr(ui, "accent_secondary", "") or "").strip() or \
            palette["accent_secondary"]
        palette["accent"] = accent
        palette["accent_secondary"] = accent2
        palette["reactor_ring"] = accent
        return cls(
            palette=palette,
            metrics=dict(METRICS),
            font_family=str(getattr(ui, "font_family", "Segoe UI") or "Segoe UI"),
            mono_font_family=str(getattr(ui, "mono_font_family", "Cascadia Mono")
                                 or "Cascadia Mono"),
            font_size=int(getattr(ui, "base_font_size", 11) or 11),
            accent=accent, accent_secondary=accent2,
            animation_speed=max(0.0, float(getattr(ui, "animation_speed", 1.0) or 0.0)),
        )

    # ------------------------------------------------------------------
    def colour(self, key: str, fallback: str = "#ffffff") -> str:
        return self.palette.get(key, fallback)

    def state_colour(self, state: str) -> str:
        return STATE_COLOURS.get(state, STATE_COLOURS["idle"])[0]

    def state_label(self, state: str) -> str:
        return STATE_COLOURS.get(state, ("", state.replace("_", " ").title()))[1]

    def pressure_colour(self, pressure: str) -> str:
        return {"ok": self.palette["success"],
                "soft": self.palette["warning"],
                "hard": self.palette["danger"]}.get(str(pressure).lower(),
                                                    self.palette["text_dim"])

    def gradient(self, first: str = "", second: str = "") -> str:
        return f"qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {first or self.accent}," \
               f" stop:1 {second or self.accent_secondary})"


# ---------------------------------------------------------------------------
def stylesheet(theme: Theme) -> str:
    """Render the whole application stylesheet."""
    p = theme.palette
    f = theme.font_family
    mono = theme.mono_font_family
    size = theme.font_size
    radius = int(theme.metrics["radius"])
    return f"""
* {{
    font-family: "{f}", "Segoe UI Variable", "Helvetica Neue", sans-serif;
    font-size: {size}pt;
    color: {p['text']};
    outline: none;
}}
QMainWindow, QDialog, QWidget#root {{
    background-color: {p['bg']};
}}
QWidget {{ background-color: transparent; }}

/* ---------- navigation ---------- */
QFrame#sidebar {{
    background-color: {p['bg_alt']};
    border-right: 1px solid {p['border']};
}}
QLabel#brandTitle {{
    color: {p['accent']};
    font-size: {size + 7}pt;
    font-weight: 700;
    letter-spacing: 3px;
}}
QLabel#brandSubtitle {{ color: {p['text_muted']}; font-size: {size - 2}pt; letter-spacing: 1px; }}
QPushButton#navButton {{
    text-align: left;
    padding: 9px 14px;
    border: none;
    border-left: 3px solid transparent;
    border-radius: 0px;
    color: {p['text_dim']};
    background-color: transparent;
}}
QPushButton#navButton:hover {{ background-color: {p['bg_hover']}; color: {p['text']}; }}
QPushButton#navButton:checked {{
    background-color: {p['bg_raised']};
    border-left: 3px solid {p['accent']};
    color: {p['accent']};
    font-weight: 600;
}}

/* ---------- panels and cards ---------- */
QFrame#card, QGroupBox {{
    background-color: {p['bg_raised']};
    border: 1px solid {p['border']};
    border-radius: {radius}px;
    padding: 10px;
    margin-top: 6px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {p['accent']};
    font-weight: 600;
}}
QLabel#sectionTitle {{ color: {p['accent']}; font-size: {size + 3}pt; font-weight: 700; }}
QLabel#cardTitle {{ color: {p['text']}; font-size: {size + 1}pt; font-weight: 600; }}
QLabel#hint, QLabel#muted {{ color: {p['text_muted']}; font-size: {size - 1}pt; }}
QLabel#valueBig {{ color: {p['text']}; font-size: {size + 9}pt; font-weight: 700; }}
QLabel[role="ok"] {{ color: {p['success']}; }}
QLabel[role="warn"] {{ color: {p['warning']}; }}
QLabel[role="danger"] {{ color: {p['danger']}; }}

/* ---------- inputs ---------- */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QListView {{
    background-color: {p['bg_raised']};
    border: 1px solid {p['border_bright']};
    border-radius: {radius - 2}px;
    padding: 6px 9px;
    selection-background-color: {p['accent_dim']};
    selection-color: {p['text']};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {p['accent']};
}}
QLineEdit:disabled, QComboBox:disabled, QTextEdit:disabled {{ color: {p['text_muted']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: none; border-left: 4px solid transparent;
    border-right: 4px solid transparent; border-top: 5px solid {p['text_dim']};
    margin-right: 8px; }}
QComboBox QAbstractItemView {{
    background-color: {p['bg_raised']};
    border: 1px solid {p['border_bright']};
    selection-background-color: {p['accent_dim']};
}}

/* ---------- buttons ---------- */
QPushButton {{
    background-color: {p['bg_raised']};
    border: 1px solid {p['border_bright']};
    border-radius: {radius - 2}px;
    padding: 7px 16px;
    color: {p['text']};
}}
QPushButton:hover {{ background-color: {p['bg_hover']}; border-color: {p['accent_dim']}; }}
QPushButton:pressed {{ background-color: {p['bg']}; }}
QPushButton:disabled {{ color: {p['text_muted']}; border-color: {p['border']}; }}
QPushButton#primary {{
    background-color: {theme.gradient()};
    border: none;
    color: #04121a;
    font-weight: 700;
}}
QPushButton#primary:hover {{ background-color: {p['accent']}; }}
QPushButton#danger {{
    background-color: transparent;
    border: 1px solid {p['danger']};
    color: {p['danger']};
}}
QPushButton#danger:hover {{ background-color: {p['danger']}; color: #180404; }}
QPushButton#ghost {{ background-color: transparent; border: 1px solid {p['border']}; }}
QPushButton#micButton {{
    border-radius: 20px; border: 1px solid {p['border_bright']};
    background-color: {p['bg_raised']}; padding: 6px;
}}
QPushButton#micButton:checked {{ border: 1px solid {p['danger']}; background-color: #2a0d10; }}

/* ---------- conversation ---------- */
QTextEdit#conversation, QPlainTextEdit#conversation {{
    background-color: {p['bg_alt']};
    border: 1px solid {p['border']};
    border-radius: {radius}px;
    padding: 10px;
    font-size: {size + 1}pt;
}}
QLineEdit#commandInput {{
    background-color: {p['bg_raised']};
    border: 1px solid {p['border_bright']};
    border-radius: {radius}px;
    padding: 10px 12px;
    font-size: {size + 1}pt;
}}
QLineEdit#commandInput:focus {{ border: 1px solid {p['accent']}; }}
QLabel#statusBarLabel {{ color: {p['text_dim']}; font-size: {size - 1}pt; }}
QFrame#statusBar {{ background-color: {p['bg_alt']}; border-top: 1px solid {p['border']}; }}
QFrame#demoBanner {{ background-color: #2b1d05; border: 1px solid {p['warning']};
    border-radius: {radius - 2}px; }}
QLabel#demoBannerText {{ color: {p['warning']}; font-weight: 600; }}

/* ---------- tables, lists, trees ---------- */
QTableWidget, QTreeWidget, QListWidget {{
    background-color: {p['bg_alt']};
    alternate-background-color: {p['bg_raised']};
    border: 1px solid {p['border']};
    border-radius: {radius}px;
    gridline-color: {p['border']};
}}
QHeaderView::section {{
    background-color: {p['bg_raised']};
    color: {p['text_dim']};
    border: none;
    border-bottom: 1px solid {p['border']};
    padding: 6px;
    font-weight: 600;
}}
QTableWidget::item:selected, QTreeWidget::item:selected, QListWidget::item:selected {{
    background-color: {p['accent_dim']};
    color: {p['text']};
}}

/* ---------- indicators ---------- */
QCheckBox, QRadioButton {{ spacing: 8px; color: {p['text_dim']}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px; border: 1px solid {p['border_bright']};
    background-color: {p['bg_raised']};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked {{ background-color: {p['accent']}; border-color: {p['accent']}; }}
QRadioButton::indicator:checked {{ background-color: {p['accent']}; border-color: {p['accent']}; }}
QSlider::groove:horizontal {{ height: 4px; background: {p['border']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 14px; height: 14px; margin: -6px 0; border-radius: 7px;
    background: {p['reactor_core']}; border: 1px solid {p['accent']};
}}
QProgressBar {{
    background-color: {p['bg_raised']}; border: 1px solid {p['border']};
    border-radius: 6px; text-align: center; color: {p['text_dim']}; height: 12px;
}}
QProgressBar::chunk {{ background-color: {p['accent']}; border-radius: 5px; }}
QScrollBar:vertical {{ background: {p['bg_alt']}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {p['border_bright']}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {p['accent_dim']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: {p['bg_alt']}; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {p['border_bright']}; border-radius: 5px; min-width: 28px; }}
QTabWidget::pane {{ border: 1px solid {p['border']}; border-radius: {radius}px; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {p['text_dim']}; padding: 7px 14px;
    border: 1px solid transparent; border-bottom: none;
}}
QTabBar::tab:selected {{ color: {p['accent']}; border-color: {p['border']};
    border-top-left-radius: {radius - 2}px; border-top-right-radius: {radius - 2}px;
    background-color: {p['bg_raised']}; }}
QToolTip {{
    background-color: {p['bg_raised']}; color: {p['text']};
    border: 1px solid {p['border_bright']}; padding: 5px;
}}
QMenu {{ background-color: {p['bg_raised']}; border: 1px solid {p['border_bright']};
    border-radius: {radius - 2}px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background-color: {p['accent_dim']}; }}
QPlainTextEdit#logView, QTextEdit#logView {{
    font-family: "{mono}", "Consolas", monospace;
    font-size: {size - 1}pt; color: {p['text_dim']};
    background-color: {p['bg']}; border: 1px solid {p['border']};
}}
"""


def state_badge_text(state: str) -> str:
    return STATE_COLOURS.get(state, ("", state.replace("_", " ").title()))[1]


def nav_items() -> List[Dict[str, str]]:
    """The eight pages, in sidebar order."""
    return [
        {"id": "dashboard", "title": "Dashboard", "icon": "◈"},
        {"id": "history", "title": "History", "icon": "≡"},
        {"id": "memory", "title": "Memory", "icon": "◍"},
        {"id": "models", "title": "Models", "icon": "▤"},
        {"id": "voice", "title": "Voice / TTS", "icon": "◉"},
        {"id": "permissions", "title": "Permissions", "icon": "⛨"},
        {"id": "automation", "title": "Automation", "icon": "⌗"},
        {"id": "general", "title": "General", "icon": "⚙"},
    ]
