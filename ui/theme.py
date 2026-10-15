"""Colours and the Qt stylesheet for the Wordly window."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

from utils.paths import ASSETS

BG = "#111317"
SIDEBAR = "#15181d"
SURFACE = "#1a1e24"
SURFACE_RAISED = "#21262d"
FIELD = "#0d0f12"
BORDER = "#272d35"
BORDER_STRONG = "#363d47"
TEXT = "#e9ecf1"
MUTED = "#98a1ad"
FAINT = "#646d79"
ACCENT = "#5b6cff"
ACCENT_HOVER = "#7080ff"
ACCENT_SOFT = "#252b52"
SUCCESS = "#3fb37f"
SUCCESS_SOFT = "#1b3329"
WARNING = "#e0a43a"
DANGER = "#e5636b"
DANGER_SOFT = "#3a1f23"

_CHECK_ICON = (ASSETS / "ui" / "check.svg").as_posix()


def palette() -> QPalette:
    """Dark base colours, so unstyled widgets and dialogs match the stylesheet."""
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: FIELD,
        QPalette.ColorRole.AlternateBase: SURFACE,
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.Button: SURFACE_RAISED,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: SURFACE_RAISED,
        QPalette.ColorRole.ToolTipText: TEXT,
        QPalette.ColorRole.PlaceholderText: FAINT,
        QPalette.ColorRole.Link: ACCENT_HOVER,
    }
    for role, colour in roles.items():
        pal.setColor(role, QColor(colour))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(FAINT))
    return pal


def stylesheet() -> str:
    return f"""
    QWidget {{ color: {TEXT}; font-size: 13px; }}
    QMainWindow, QWidget#MainArea, QWidget#PageHeader, QWidget#Page,
    QWidget#PageContainer, QStackedWidget {{ background-color: {BG}; }}
    QToolTip {{
        background-color: {SURFACE_RAISED};
        color: {TEXT};
        border: 1px solid {BORDER_STRONG};
        padding: 4px 6px;
    }}

    /* --- WSL title bar ------------------------------------------------- */
    QWidget#WslTitleBar {{ background-color: {SIDEBAR}; border-bottom: 1px solid {BORDER}; }}
    QLabel#WslTitleLabel {{ color: {TEXT}; font-size: 11px; font-weight: 600; }}
    QPushButton#WslTitleButton, QPushButton#WslTitleCloseButton {{
        min-width: 0; padding: 0; border-radius: 6px;
        background-color: transparent; border: 1px solid transparent;
        color: {MUTED}; font-size: 12px;
    }}
    QPushButton#WslTitleButton:hover {{ background-color: {SURFACE_RAISED}; }}
    QPushButton#WslTitleCloseButton:hover {{ background-color: {DANGER}; color: #ffffff; }}

    /* --- Sidebar -------------------------------------------------------- */
    QWidget#Sidebar {{ background-color: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
    QLabel#AppName {{ font-size: 20px; font-weight: 800; color: {TEXT}; }}
    QLabel#AppTagline {{ font-size: 11px; color: {FAINT}; }}
    QFrame#StepItem {{ border-radius: 10px; background-color: transparent; }}
    QFrame#StepItem[state="current"] {{ background-color: {ACCENT_SOFT}; }}
    QFrame#StepItem[clickable="true"]:hover {{ background-color: {SURFACE_RAISED}; }}
    QFrame#StepItem[state="current"]:hover {{ background-color: {ACCENT_SOFT}; }}
    QLabel#StepBadge {{
        min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px;
        border-radius: 12px; font-size: 11px; font-weight: 700;
        qproperty-alignment: AlignCenter;
        background-color: {SURFACE_RAISED}; color: {MUTED}; border: 1px solid {BORDER_STRONG};
    }}
    QFrame#StepItem[state="current"] QLabel#StepBadge {{
        background-color: {ACCENT}; color: #ffffff; border-color: {ACCENT};
    }}
    QFrame#StepItem[state="done"] QLabel#StepBadge {{
        background-color: {SUCCESS_SOFT}; color: {SUCCESS}; border-color: {SUCCESS};
    }}
    QLabel#StepName {{ font-size: 13px; font-weight: 600; color: {MUTED}; }}
    QFrame#StepItem[state="current"] QLabel#StepName,
    QFrame#StepItem[state="done"] QLabel#StepName {{ color: {TEXT}; }}
    QFrame#StepItem[state="locked"] QLabel#StepName {{ color: {FAINT}; }}
    QLabel#StepSummary {{ font-size: 11px; color: {FAINT}; }}
    QFrame#StepItem[state="current"] QLabel#StepSummary,
    QFrame#StepItem[state="done"] QLabel#StepSummary {{ color: {MUTED}; }}
    QLabel#SidebarFooter {{ font-size: 11px; color: {FAINT}; }}

    /* --- Page header ---------------------------------------------------- */
    QLabel#PageEyebrow {{
        font-size: 11px; font-weight: 700; color: {ACCENT_HOVER}; letter-spacing: 1px;
    }}
    QLabel#PageTitle {{ font-size: 22px; font-weight: 700; color: {TEXT}; }}
    QLabel#PageSubtitle {{ font-size: 13px; color: {MUTED}; }}

    /* --- Cards ---------------------------------------------------------- */
    QFrame#Card {{
        background-color: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: 12px;
    }}
    QFrame#Card[accent="true"] {{ border-color: #3a4290; background-color: #191d33; }}
    QFrame#Card[tone="success"] {{ border-color: #2b6b4f; background-color: #15241e; }}
    QLabel#CardTitle {{ font-size: 14px; font-weight: 700; color: {TEXT}; }}
    QLabel#CardSubtitle, QLabel#Muted {{ font-size: 12px; color: {MUTED}; }}
    QLabel#Faint {{ font-size: 11px; color: {FAINT}; }}
    QLabel#FieldLabel {{ font-size: 12px; font-weight: 600; color: {MUTED}; }}
    QLabel#FieldError {{ font-size: 11px; color: {DANGER}; }}
    QLabel#Pill {{
        font-size: 11px; font-weight: 600; color: {MUTED};
        background-color: {SURFACE_RAISED}; border-radius: 9px; padding: 2px 8px;
    }}
    QLabel#Pill[tone="ok"] {{ color: {SUCCESS}; background-color: {SUCCESS_SOFT}; }}
    QLabel#Pill[tone="warn"] {{ color: {WARNING}; background-color: #3a2e17; }}
    QLabel#Pill[tone="accent"] {{ color: {ACCENT_HOVER}; background-color: {ACCENT_SOFT}; }}
    QLabel#Notice {{
        font-size: 12px; color: {MUTED};
        background-color: {SURFACE_RAISED}; border-radius: 8px; padding: 8px 10px;
    }}
    QLabel#Notice[tone="lux"] {{ color: #cfd4ff; background-color: #20264a; }}
    QLabel#Notice[tone="ok"] {{ color: #bfe8d4; background-color: {SUCCESS_SOFT}; }}
    QLabel#Notice[tone="warn"] {{ color: #f3d9a4; background-color: #3a2e17; }}

    /* --- Drop zone ------------------------------------------------------ */
    QFrame#DropZone {{
        border: 2px dashed {BORDER_STRONG}; border-radius: 12px; background-color: {SURFACE};
    }}
    QFrame#DropZone[hover="true"] {{ border-color: {ACCENT}; background-color: #191d33; }}
    QLabel#DropIcon {{ font-size: 28px; color: {FAINT}; }}

    /* --- Inputs --------------------------------------------------------- */
    QLineEdit, QPlainTextEdit, QComboBox {{
        border: 1px solid {BORDER_STRONG}; border-radius: 8px;
        padding: 7px 10px; background-color: {FIELD};
        selection-background-color: {ACCENT};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
    QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {{ color: {FAINT}; }}
    QLineEdit[invalid="true"] {{ border-color: {DANGER}; background-color: #1a1214; }}
    QLineEdit#BigInput {{ font-size: 14px; padding: 10px 12px; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background-color: {SURFACE_RAISED}; border: 1px solid {BORDER_STRONG};
        selection-background-color: {ACCENT_SOFT};
    }}

    /* --- Buttons -------------------------------------------------------- */
    QPushButton, QToolButton {{
        background-color: {SURFACE_RAISED}; border: 1px solid {BORDER_STRONG};
        border-radius: 8px; padding: 7px 14px; color: {TEXT};
    }}
    QPushButton:hover, QToolButton:hover {{ background-color: #2a3039; border-color: #4a525e; }}
    QPushButton:pressed, QToolButton:pressed {{ background-color: #1f242b; }}
    QPushButton:disabled, QToolButton:disabled {{
        color: {FAINT}; background-color: {SURFACE}; border-color: {BORDER};
    }}
    QToolButton::menu-indicator {{ image: none; width: 0; }}
    QPushButton#Primary {{
        background-color: {ACCENT}; border-color: {ACCENT}; color: #ffffff; font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background-color: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
    QPushButton#Primary:disabled {{
        background-color: #2a2f4a; border-color: #2a2f4a; color: #7c84a8;
    }}
    QPushButton#Ghost, QToolButton#Ghost {{
        background-color: transparent; border-color: transparent; color: {MUTED};
    }}
    QPushButton#Ghost:hover, QToolButton#Ghost:hover {{ background-color: {SURFACE_RAISED}; color: {TEXT}; }}
    QPushButton#Ghost:disabled {{ background-color: transparent; color: {FAINT}; }}
    QPushButton#Small, QPushButton#Danger, QPushButton#PrimarySmall {{
        padding: 4px 10px; font-size: 12px; border-radius: 7px;
    }}
    /* The main action in a card header, sized like the small buttons beside it. */
    QPushButton#PrimarySmall {{
        background-color: {ACCENT}; border-color: {ACCENT}; color: #ffffff; font-weight: 600;
    }}
    QPushButton#PrimarySmall:hover {{ background-color: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
    QPushButton#PrimarySmall:disabled {{
        background-color: #2a2f4a; border-color: #2a2f4a; color: #7c84a8;
    }}
    QPushButton#Danger {{ background-color: transparent; color: {MUTED}; }}
    QPushButton#Danger:hover:enabled {{ border-color: {DANGER}; color: {DANGER}; background-color: {DANGER_SOFT}; }}
    QPushButton#Link {{
        background-color: transparent; border: none; padding: 2px 4px;
        color: {ACCENT_HOVER}; font-weight: 600;
    }}
    QPushButton#Link:hover {{ color: #9aa6ff; }}
    QPushButton#Cancel {{ background-color: transparent; color: {MUTED}; padding: 5px 12px; }}
    QPushButton#Cancel:hover {{ border-color: {DANGER}; color: {DANGER}; background-color: {DANGER_SOFT}; }}

    /* --- Lists and tables ----------------------------------------------- */
    QListWidget {{
        border: 1px solid {BORDER}; border-radius: 10px; padding: 4px; outline: none;
        background-color: {FIELD};
    }}
    QListWidget::item {{ border-radius: 8px; margin: 1px 0; }}
    QListWidget::item:selected {{ background-color: {ACCENT_SOFT}; }}
    QListWidget::item:hover:!selected {{ background-color: {SURFACE_RAISED}; }}
    QLabel#ClipIndex {{
        min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;
        border-radius: 6px; font-size: 11px; font-weight: 700; color: {MUTED};
        background-color: {SURFACE_RAISED}; qproperty-alignment: AlignCenter;
    }}
    QLabel#ClipTitle {{ font-size: 13px; font-weight: 600; color: {TEXT}; }}
    QLabel#ClipMeta {{ font-size: 11px; color: {MUTED}; }}
    QTableWidget {{
        background-color: {FIELD}; border: 1px solid {BORDER}; border-radius: 10px; gridline-color: {BORDER};
        selection-background-color: {ACCENT_SOFT}; selection-color: {TEXT};
    }}
    QHeaderView::section {{
        background-color: {SURFACE_RAISED}; color: {MUTED}; border: none;
        border-bottom: 1px solid {BORDER}; padding: 6px 8px; font-size: 11px; font-weight: 700;
    }}
    QTableCornerButton::section {{ background-color: {SURFACE_RAISED}; border: none; }}

    QTabBar#ClipTabs::tab {{
        background-color: transparent; color: {MUTED}; border: 1px solid {BORDER};
        border-radius: 8px; padding: 5px 12px; margin-right: 6px; font-size: 12px; font-weight: 600;
    }}
    QTabBar#ClipTabs::tab:selected {{ background-color: {ACCENT_SOFT}; color: {TEXT}; border-color: {ACCENT}; }}
    QTabBar#ClipTabs::tab:hover:!selected {{ background-color: {SURFACE_RAISED}; color: {TEXT}; }}

    /* --- Summary rows (Export) ------------------------------------------ */
    QFrame#SummaryRow {{ border-bottom: 1px solid {BORDER}; }}
    QLabel#SummaryKey {{ font-size: 12px; font-weight: 600; color: {MUTED}; }}
    QLabel#SummaryValue {{ font-size: 13px; color: {TEXT}; }}
    QLabel#SummaryMark {{ font-size: 13px; font-weight: 700; color: {SUCCESS}; }}
    QLabel#SummaryMark[tone="todo"] {{ color: {WARNING}; }}
    QLabel#SummaryMark[tone="off"] {{ color: {FAINT}; }}

    /* --- Caption preview ------------------------------------------------ */
    QLabel#CaptionStage {{
        border-radius: 10px; padding: 0 12px 22px 12px; font-size: 20px; font-weight: 800;
        background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2d3a46, stop:1 #141a20);
    }}

    /* --- Footer --------------------------------------------------------- */
    QWidget#Footer {{ background-color: {BG}; border-top: 1px solid {BORDER}; }}
    QFrame#JobBar {{ background-color: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}
    QLabel#JobStatusLabel {{ font-size: 12px; color: {TEXT}; }}
    QProgressBar {{
        min-height: 6px; max-height: 6px; border: none; border-radius: 3px;
        background-color: {SURFACE_RAISED}; text-align: center; color: transparent;
    }}
    QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 3px; }}

    /* --- Misc ----------------------------------------------------------- */
    QScrollArea {{ border: none; background-color: {BG}; }}
    QScrollArea > QWidget > QWidget {{ background-color: {BG}; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {BORDER_STRONG}; border-radius: 3px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QCheckBox {{ spacing: 10px; background-color: transparent; }}
    QCheckBox#BigToggle {{ font-size: 14px; font-weight: 600; }}
    QCheckBox::indicator {{
        width: 18px; height: 18px; border-radius: 5px;
        border: 1px solid {BORDER_STRONG}; background-color: {FIELD};
    }}
    QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
    QCheckBox::indicator:checked {{
        background-color: {ACCENT}; border-color: {ACCENT}; image: url("{_CHECK_ICON}");
    }}
    QCheckBox::indicator:disabled {{ background-color: {SURFACE}; border-color: {BORDER}; }}
    QSlider::groove:horizontal {{ height: 4px; background: {SURFACE_RAISED}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        background: #ffffff; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
    }}
    QMenu {{ background-color: {SURFACE_RAISED}; border: 1px solid {BORDER_STRONG}; padding: 4px; }}
    QMenu::item {{ padding: 6px 14px; border-radius: 6px; }}
    QMenu::item:selected {{ background-color: {ACCENT_SOFT}; }}
    QMessageBox QLabel {{ color: {TEXT}; }}
    """
