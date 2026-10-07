"""Visual theme: one place for every colour, radius and spacing.

The interface is styled from these tokens rather than from stylesheets scattered
over the widgets, so the whole application can be re-skinned by editing this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Palette:
    """The colours of one theme.

    These follow the house style of the sibling applications in this folder: a
    violet accent running to rose, on a near black violet background.
    """

    window: str
    surface: str
    surface_raised: str
    border: str
    border_strong: str
    accent: str
    accent_2: str  # second stop of the accent gradient
    accent_hover: str
    accent_hover_2: str
    accent_pressed: str
    accent_text: str
    text: str
    text_muted: str
    text_faint: str
    success: str
    warning: str
    danger: str
    selection: str


DARK = Palette(
    window="#141021",
    surface="#1b1729",
    surface_raised="#211d33",
    border="#2c2742",
    border_strong="#3a3455",
    accent="#7c5cff",
    accent_2="#ff5c8a",
    accent_hover="#8f73ff",
    accent_hover_2="#ff7ba0",
    accent_pressed="#6a4ae6",
    accent_text="#ffffff",
    text="#eceafa",
    text_muted="#a49ec4",
    text_faint="#7d7799",
    success="#4ade80",
    warning="#fbbf24",
    danger="#f87171",
    selection="#2f2750",
)

LIGHT = Palette(
    window="#f2f4fa",
    surface="#ffffff",
    surface_raised="#f7f7fd",
    border="#dde2f1",
    border_strong="#c6cbe0",
    accent="#5b4cf5",
    accent_2="#e8578a",
    accent_hover="#6c5eff",
    accent_hover_2="#f06d9c",
    accent_pressed="#4a3ce0",
    accent_text="#ffffff",
    text="#1c2136",
    text_muted="#6b7394",
    text_faint="#8d94ad",
    success="#2f8a5c",
    warning="#a9761f",
    danger="#d05046",
    selection="#e4e1fb",
)

RADIUS = 16  # the big card
RADIUS_PANEL = 10  # panels and lists
RADIUS_SMALL = 10  # buttons
RADIUS_INPUT = 9
SPACING = 12
CONTROL_HEIGHT = 32

FONT_FAMILY = '"Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", sans-serif'
FONT_SIZE = 13
FONT_SIZE_SMALL = 11
FONT_SIZE_LARGE = 15
FONT_SIZE_TITLE = 26

# Qt draws no tick of its own once an indicator is styled, so the stylesheet
# supplies one. Forward slashes: a stylesheet url is not a Windows path.
CHECK_ICON = (Path(__file__).resolve().parents[3] / "assets" / "check.png").as_posix()


def palette_for(name: str) -> Palette:
    """Return the palette of a theme by name, defaulting to dark."""
    return LIGHT if name == "light" else DARK


def stylesheet(name: str = "dark") -> str:
    """Return the application stylesheet for a theme."""
    p = palette_for(name)
    return f"""
    QWidget {{
        background: {p.window};
        color: {p.text};
        font-family: {FONT_FAMILY};
        font-size: {FONT_SIZE}px;
    }}
    QMainWindow, QDialog {{ background: {p.window}; }}

    QLabel {{ background: transparent; }}
    QLabel[role="title"] {{
        font-size: {FONT_SIZE_TITLE}px;
        font-weight: 600;
        color: {p.text};
    }}
    QLabel[role="subtitle"] {{
        font-size: {FONT_SIZE_LARGE}px;
        color: {p.text_muted};
    }}
    QLabel[role="muted"] {{ color: {p.text_muted}; }}
    QLabel[role="faint"] {{ color: {p.text_faint}; font-size: {FONT_SIZE_SMALL}px; }}
    QLabel[role="section"] {{
        color: {p.text_muted};
        font-size: {FONT_SIZE_SMALL}px;
        font-weight: 600;
        letter-spacing: 1px;
        text-transform: uppercase;
    }}

    QToolBar {{
        background: {p.surface};
        border: none;
        border-bottom: 1px solid {p.border};
        padding: 6px 8px;
        spacing: 4px;
    }}
    QToolBar QToolButton {{
        background: transparent;
        color: {p.text};
        padding: 6px 12px;
        border-radius: {RADIUS_SMALL}px;
    }}
    QToolBar QToolButton:hover {{ background: {p.surface_raised}; }}
    QToolBar QToolButton:disabled {{ color: {p.text_faint}; }}
    QToolBar QToolButton#primary {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                    stop:0 {p.accent}, stop:1 {p.accent_2});
        color: {p.accent_text};
        font-weight: 600;
    }}
    QToolBar QToolButton#primary:hover {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                    stop:0 {p.accent_hover}, stop:1 {p.accent_hover_2});
    }}
    QToolBar QToolButton#primary:disabled {{
        background: {p.surface_raised};
        color: {p.text_faint};
    }}

    QMenuBar {{ background: {p.surface}; border-bottom: 1px solid {p.border}; }}
    QMenuBar::item {{ padding: 6px 10px; background: transparent; }}
    QMenuBar::item:selected {{ background: {p.surface_raised}; border-radius: {RADIUS_SMALL}px; }}
    QMenu {{
        background: {p.surface_raised};
        border: 1px solid {p.border};
        border-radius: {RADIUS_SMALL}px;
        padding: 4px;
    }}
    QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {p.accent}; color: {p.accent_text}; }}

    QPushButton {{
        background: {p.surface_raised};
        color: {p.text};
        border: 1px solid {p.border_strong};
        border-radius: {RADIUS_SMALL}px;
        padding: 6px 14px;
        min-height: {CONTROL_HEIGHT - 14}px;
    }}
    QPushButton:hover {{ border-color: {p.accent}; }}
    QPushButton:pressed {{ background: {p.surface}; }}
    QPushButton:disabled {{ color: {p.text_faint}; border-color: {p.border}; }}
    QPushButton[kind="primary"] {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                    stop:0 {p.accent}, stop:1 {p.accent_2});
        color: {p.accent_text};
        border: none;
        font-weight: 600;
        padding: 9px 22px;
    }}
    QPushButton[kind="primary"]:hover {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                    stop:0 {p.accent_hover}, stop:1 {p.accent_hover_2});
    }}
    QPushButton[kind="primary"]:pressed {{ background: {p.accent_pressed}; }}
    QPushButton[kind="primary"]:disabled {{
        background: {p.surface_raised};
        color: {p.text_faint};
    }}
    QPushButton[kind="danger"] {{
        color: {p.danger};
    }}
    QPushButton[kind="danger"]:hover {{ border-color: {p.danger}; }}
    QPushButton[kind="link"] {{
        background: transparent;
        border: none;
        color: {p.accent};
        padding: 4px 8px;
    }}
    QPushButton[kind="link"]:hover {{ color: {p.accent_hover}; }}

    QComboBox {{
        background: {p.surface_raised};
        border: 1px solid {p.border_strong};
        border-radius: {RADIUS_SMALL}px;
        padding: 5px 10px;
        min-height: {CONTROL_HEIGHT - 14}px;
    }}
    QComboBox:hover {{ border-color: {p.accent}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {p.surface_raised};
        border: 1px solid {p.border};
        selection-background-color: {p.accent};
        selection-color: {p.accent_text};
        outline: none;
    }}

    QLineEdit, QSpinBox, QDoubleSpinBox {{
        background: {p.surface_raised};
        border: 1px solid {p.border_strong};
        border-radius: {RADIUS_INPUT}px;
        padding: 5px 8px;
        selection-background-color: {p.accent};
        selection-color: {p.accent_text};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {p.accent}; }}

    QCheckBox {{ spacing: 8px; background: transparent; }}
    QCheckBox::indicator, QTableView::indicator {{
        width: 17px; height: 17px;
        border: 1px solid {p.border_strong};
        border-radius: 5px;
        background: {p.surface_raised};
    }}
    QCheckBox::indicator:hover, QTableView::indicator:hover {{ border-color: {p.accent}; }}
    QCheckBox::indicator:checked, QTableView::indicator:checked {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                    stop:0 {p.accent}, stop:1 {p.accent_2});
        border-color: {p.accent};
        image: url({CHECK_ICON});
    }}
    QTableView::indicator:disabled {{
        border-color: {p.border};
        background: {p.window};
    }}

    QTableWidget {{
        background: {p.surface};
        alternate-background-color: {p.surface_raised};
        border: 1px solid {p.border};
        border-radius: {RADIUS_PANEL}px;
        gridline-color: transparent;
        outline: none;
    }}
    QTableWidget::item {{ padding: 4px 8px; border: none; }}
    QTableWidget::item:selected {{ background: {p.selection}; color: {p.text}; }}
    QHeaderView::section {{
        background: {p.surface};
        color: {p.text_muted};
        border: none;
        border-bottom: 1px solid {p.border};
        padding: 8px;
        font-size: {FONT_SIZE_SMALL}px;
        font-weight: 600;
    }}

    QProgressBar {{
        background: {p.border};
        border: none;
        border-radius: 3px;
        text-align: center;
    }}
    QProgressBar#batch {{ border-radius: 4px; }}
    QProgressBar::chunk {{
        border-radius: 3px;
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                    stop:0 {p.accent}, stop:1 {p.accent_2});
    }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{
        background: {p.border_strong};
        border-radius: 5px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p.text_faint}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {p.border_strong}; border-radius: 5px; }}

    QStatusBar {{
        background: {p.surface};
        border-top: 1px solid {p.border};
        color: {p.text_muted};
    }}
    QStatusBar::item {{ border: none; }}

    QTabWidget::pane {{
        border: 1px solid {p.border};
        border-radius: {RADIUS}px;
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {p.text_muted};
        padding: 8px 16px;
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {p.accent}; }}
    QTabBar::tab:hover {{ color: {p.text}; }}

    QToolTip {{
        background: {p.surface_raised};
        color: {p.text};
        border: 1px solid {p.border_strong};
        padding: 4px 8px;
    }}
    """
