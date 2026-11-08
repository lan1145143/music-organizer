"""主题：浅色 / 深色 / 纸质三套预设 + 自选强调色 + 自定义背景。

视觉规范（对齐 UI 原型 code_artifact.html）：
- 浅色：主背景 #F3F5F8，卡片 #FFFFFF，边框 #E2E8F0，主色 #2563EB；
- 深色：主背景 #0F141C，卡片 #181F2C，边框 #263042，主色 #3B82F6；
- 语气色（tone）：就绪=翠绿、来源/模式=浅蓝、待处理=琥珀、失联/冲突=红、
  边界提示=靛蓝、中性=灰；以 QLabel[tone=...] 徽章与自绘胶囊两种方式使用。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication

FONT_FAMILY = "Microsoft YaHei UI"
MONO_FAMILY = "Consolas"


@dataclass(frozen=True)
class Palette:
    name: str
    window: str
    surface: str
    surface_alt: str
    border: str
    text: str
    text_muted: str
    header: str
    accent_default: str
    selection_text: str = "#FFFFFF"
    shadow: str = "#00000022"


PALETTES: dict[str, Palette] = {
    "light": Palette(
        name="浅色",
        window="#F3F5F8", surface="#FFFFFF", surface_alt="#F8FAFC",
        border="#E2E8F0", text="#1E293B", text_muted="#64748B",
        header="#F8FAFC", accent_default="#2563EB",
    ),
    "dark": Palette(
        name="深色",
        window="#0F141C", surface="#181F2C", surface_alt="#1F2836",
        border="#263042", text="#E2E8F0", text_muted="#94A3B8",
        header="#1B2433", accent_default="#3B82F6",
    ),
    "paper": Palette(
        name="纸质",
        window="#F3EFE5", surface="#FAF7EF", surface_alt="#F6F1E4",
        border="#D8D1C3", text="#38352F", text_muted="#7A7368",
        header="#EFE9DB", accent_default="#52796F",
    ),
}

THEME_ORDER = ("light", "paper", "dark")

# 语气色：name -> (背景, 文字, 边框)；浅/深色各一套，QSS 与自绘胶囊共用
TONES: dict[str, dict[str, tuple[str, str, str]]] = {
    "light": {
        "blue": ("#EFF6FF", "#2563EB", "#BFDBFE"),
        "green": ("#ECFDF5", "#047857", "#A7F3D0"),
        "amber": ("#FFFBEB", "#B45309", "#FDE68A"),
        "gray": ("#F1F5F9", "#64748B", "#E2E8F0"),
        "red": ("#FEF2F2", "#B91C1C", "#FECACA"),
        "indigo": ("#EEF2FF", "#4F46E5", "#C7D2FE"),
    },
    "dark": {
        "blue": ("#1D2A45", "#93C5FD", "#2C3E63"),
        "green": ("#12301F", "#6EE7B7", "#1D4A33"),
        "amber": ("#3A2A10", "#FCD34D", "#57431C"),
        "gray": ("#263042", "#94A3B8", "#33405A"),
        "red": ("#3B1A1A", "#FCA5A5", "#5A2A2A"),
        "indigo": ("#262A4D", "#A5B4FC", "#3A3E6E"),
    },
    "paper": {
        "blue": ("#E8EEE8", "#3E6259", "#C6D3C8"),
        "green": ("#E4EFE4", "#3D6B4F", "#C2D6C4"),
        "amber": ("#F3ECD8", "#8A6A24", "#DFD2A8"),
        "gray": ("#ECE7DA", "#7A7368", "#D8D1C3"),
        "red": ("#F0DFDA", "#9C3B2E", "#D9B4AA"),
        "indigo": ("#E4E2EE", "#4C4A80", "#C5C2DA"),
    },
}


def palette_for(theme: str) -> Palette:
    return PALETTES.get(theme, PALETTES["light"])


def tones_for(theme: str) -> dict[str, tuple[str, str, str]]:
    return TONES.get(theme, TONES["light"])


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(c * 2 for c in color)
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def rgba(color: str, alpha: float) -> str:
    r, g, b = hex_to_rgb(color)
    return f"rgba({r},{g},{b},{max(0.0, min(1.0, alpha)):.3f})"


def mix(color_a: str, color_b: str, ratio: float) -> str:
    ar, ag, ab = hex_to_rgb(color_a)
    br, bg, bb = hex_to_rgb(color_b)
    r = round(ar + (br - ar) * ratio)
    g = round(ag + (bg - ag) * ratio)
    b = round(ab + (bb - ab) * ratio)
    return f"#{r:02X}{g:02X}{b:02X}"


def is_dark(theme: str) -> bool:
    return theme == "dark"


def accent_set(accent: str, theme: str) -> dict[str, str]:
    """强调色的悬停/按下/浅底变体。"""
    dark = is_dark(theme)
    hover = mix(accent, "#FFFFFF" if not dark else "#000000", 0.12 if not dark else 0.18)
    pressed = mix(accent, "#000000", 0.16)
    soft = mix(accent, PALETTES[theme].surface, 0.86 if not dark else 0.80)
    return {"accent": accent, "hover": hover, "pressed": pressed, "soft": soft,
            "text_on": "#FFFFFF" if _luminance(accent) < 0.62 else "#1B1B1B"}


def _luminance(color: str) -> float:
    r, g, b = hex_to_rgb(color)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def _tone_rules(theme: str) -> str:
    """QLabel[tone=...] 徽章规则：圆角胶囊 + 细边框。"""
    rules = []
    for name, (bg, fg, border) in tones_for(theme).items():
        rules.append(
            f'QLabel[tone="{name}"] {{\n'
            f"    background-color: {bg};\n    color: {fg};\n"
            f"    border: 1px solid {border};\n"
            f"    border-radius: 9px;\n    padding: 1px 8px;\n"
            f"    font-size: 8.5pt;\n    font-weight: 500;\n}}")
    return "\n".join(rules)


def build_stylesheet(theme: str, accent: str, panel_alpha: float = 1.0,
                     solid: bool = False) -> str:
    p = palette_for(theme)
    a = accent_set(accent, theme)
    panel = rgba(p.surface, 1.0 if solid else panel_alpha)
    panel_alt = rgba(p.surface_alt, 1.0 if solid else min(1.0, panel_alpha + 0.04))
    header = rgba(p.header, 1.0 if solid else panel_alpha)
    window = rgba(p.window, 1.0 if solid else max(0.0, panel_alpha - 0.15))
    text = p.text
    muted = p.text_muted
    border = rgba(p.border, 1.0 if solid else max(0.35, panel_alpha))
    radius = 10
    return f"""
* {{
    font-family: "{FONT_FAMILY}", "Segoe UI", "Yu Gothic UI", sans-serif;
    font-size: 9.5pt;
    outline: none;
}}
QWidget {{
    color: {text};
    selection-background-color: {a['soft']};
    selection-color: {text};
}}
QMainWindow, QDialog {{
    background-color: {window};
}}
QToolTip {{
    background-color: {panel};
    color: {text};
    border: 1px solid {border};
    padding: 5px 8px;
    border-radius: 4px;
}}
QFrame#Card, QWidget#Card {{
    background-color: {panel};
    border: 1px solid {border};
    border-radius: {radius}px;
}}
QFrame#HeaderBar, QFrame#FooterBar {{
    background-color: {panel};
    border: 1px solid {border};
    border-radius: {radius}px;
}}
QFrame#Toolbar {{
    background-color: {panel};
    border: 1px solid {border};
    border-radius: {radius}px;
}}
/* 详情面板随附文件 Action Bar：顶部分割线，紧贴附件列表底部 */
QFrame#ActionBar {{
    border: none;
    border-top: 1px solid {border};
    background-color: transparent;
}}
QLabel#Title {{
    font-size: 12.5pt;
    font-weight: 600;
}}
QLabel#BrandTitle {{
    font-size: 10.5pt;
    font-weight: 600;
}}
QLabel#Section {{
    font-size: 10pt;
    font-weight: 600;
    color: {text};
}}
QLabel#DetailTitle {{
    font-size: 12pt;
    font-weight: 700;
}}
QLabel#Muted, QLabel[muted="true"] {{
    color: {muted};
}}
QLabel#Badge {{
    background-color: {a['soft']};
    color: {text};
    border-radius: 8px;
    padding: 2px 8px;
    font-size: 8.5pt;
}}
QLabel#FieldLabel {{
    color: {muted};
    font-size: 8pt;
}}
QLabel#PathText {{
    font-family: "{MONO_FAMILY}", "Courier New", monospace;
    font-size: 9pt;
}}
QLabel#Kbd {{
    font-family: "{MONO_FAMILY}", "Courier New", monospace;
    font-size: 7.5pt;
    color: {muted};
    background-color: {panel_alt};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 0px 4px;
}}
{_tone_rules(theme)}
QPushButton {{
    background-color: {panel_alt};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 5px 12px;
    min-height: 18px;
}}
QPushButton:hover {{
    background-color: {rgba(mix(p.surface, p.text, 0.06), 1.0 if solid else panel_alpha)};
    border-color: {rgba(p.border, 1.0)};
}}
QPushButton:pressed {{
    background-color: {rgba(mix(p.surface, p.text, 0.12), 1.0 if solid else panel_alpha)};
}}
QPushButton:disabled {{
    color: {muted};
    background-color: {rgba(p.surface_alt, 0.6 if not solid else 1.0)};;
}}
QPushButton[kind="primary"] {{
    background-color: {a['accent']};
    color: {a['text_on']};
    border: 1px solid {a['accent']};
    font-weight: 600;
}}
QPushButton[kind="primary"]:hover {{
    background-color: {a['hover']};
    border-color: {a['hover']};
}}
QPushButton[kind="primary"]:pressed {{
    background-color: {a['pressed']};
}}
QPushButton[kind="primary"]:disabled {{
    background-color: {rgba(a['accent'], 0.45)};
    border-color: transparent;
    color: {rgba(p.text, 0.7)};
}}
QPushButton[kind="ghost"] {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {text};
    padding: 4px 8px;
}}
QPushButton[kind="ghost"]:hover {{
    background-color: {rgba(mix(p.surface, p.text, 0.07), 1.0 if solid else panel_alpha)};
    border-color: {rgba(p.border, 1.0 if solid else 0.6)};
}}
QPushButton[kind="soft"] {{
    background-color: {a['soft']};
    color: {a['accent']};
    border: 1px solid {rgba(a['accent'], 0.35)};
    font-weight: 600;
}}
QPushButton[kind="soft"]:hover {{
    background-color: {rgba(a['accent'], 0.22)};
    border-color: {rgba(a['accent'], 0.55)};
}}
QPushButton[kind="soft"]:pressed {{
    background-color: {rgba(a['accent'], 0.32)};
}}
QPushButton[kind="danger"] {{
    color: #C0392B;
    border-color: {rgba('#C0392B', 0.5)};
}}
/* 顶栏/侧栏的纯图标小按钮 */
QPushButton#IconBtn {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 3px;
    min-width: 22px;
    min-height: 22px;
    max-width: 30px;
}}
QPushButton#IconBtn:hover {{
    background-color: {rgba(mix(p.surface, p.text, 0.08), 1.0 if solid else panel_alpha)};
    border-color: {rgba(p.border, 0.8)};
}}
QPushButton#IconBtn:checked {{
    background-color: {a['soft']};
    border-color: {rgba(a['accent'], 0.4)};
}}
QPushButton#IconBtn[accent="true"] {{
    background-color: {a['soft']};
    border-color: {rgba(a['accent'], 0.4)};
}}
/* 分段控制器：胶囊按钮组 */
QPushButton[kind="pill"] {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    color: {muted};
    padding: 3px 10px;
    font-weight: 500;
}}
QPushButton[kind="pill"]:hover {{
    color: {text};
}}
QPushButton[kind="pill"][pillActive="true"] {{
    background-color: {p.surface};
    color: {text};
    border: 1px solid {border};
    font-weight: 600;
    {'' if not is_dark(theme) else ''}
}}
QFrame#Segmented {{
    background-color: {rgba(p.border, 0.45)};
    border: 1px solid {rgba(p.border, 0.7)};
    border-radius: 8px;
}}
QFrame#SearchBox {{
    background-color: {rgba(p.border, 0.35)};
    border: 1px solid transparent;
    border-radius: 8px;
}}
QFrame#SearchBox:focus-within {{ border: 1px solid {rgba(a['accent'], 0.6)}; }}
QFrame#SearchBox QLineEdit {{
    background: transparent;
    border: none;
    padding: 2px 0px;
}}
QPushButton#PathCapsule {{
    background-color: {panel_alt};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 4px 10px;
    text-align: left;
}}
QPushButton#PathCapsule:hover {{
    background-color: {rgba(a['accent'], 0.10)};
    border-color: {rgba(a['accent'], 0.45)};
}}
QFrame#BadgeEdit {{
    background-color: {panel_alt};
    border: 1px solid {border};
    border-radius: 6px;
}}
QFrame#BadgeEdit[disabled="true"] {{
    background-color: {rgba(p.border, 0.30)};
    border-color: {rgba(p.border, 0.6)};
}}
QFrame#BadgeEdit QLineEdit {{
    background: transparent;
    border: none;
    padding: 0px;
}}
QFrame#BadgeEdit QSpinBox {{
    background: transparent;
    border: none;
    padding: 0px;
}}
QFrame#EvidenceCard {{
    background-color: {panel_alt};
    border: 1px solid {border};
    border-radius: 8px;
}}
QFrame#PathCard {{
    background-color: {panel_alt};
    border: 1px solid {border};
    border-radius: 8px;
}}
/* 左侧栏「全部来源目录」固定入口 */
QPushButton#AllSourcesBtn {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    color: {text};
    padding: 4px 8px;
    text-align: left;
    font-weight: 500;
}}
QPushButton#AllSourcesBtn:hover {{
    background-color: {rgba(mix(p.surface, p.text, 0.06), 1.0 if solid else panel_alpha)};
}}
QPushButton#AllSourcesBtn[active="true"] {{
    background-color: {a['soft']};
    color: {a['accent']};
    border-color: {rgba(a['accent'], 0.30)};
    font-weight: 600;
}}
QFrame#ScopeBadge {{
    border-radius: 8px;
}}
QPushButton#CloseBadge {{
    background: transparent;
    border: none;
    color: {muted};
    font-size: 10pt;
    padding: 0px 4px;
    min-height: 14px;
}}
QPushButton#CloseBadge:hover {{
    color: {text};
}}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background-color: {panel_alt};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 4px 8px;
    min-height: 18px;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {a['accent']};
}}
QLineEdit:disabled {{
    color: {muted};
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}
QComboBox QAbstractItemView {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    selection-background-color: {a['soft']};
    selection-color: {text};
    padding: 2px;
}}
QTreeView, QTableView, QListView {{
    background-color: transparent;
    alternate-background-color: transparent;
    border: none;
    gridline-color: transparent;
}}
/* 滚动区内容底色跟随父级（否则深色模式下露出系统浅色调色板） */
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QTreeView::item, QTableView::item, QListView::item {{
    padding: 3px 4px;
    border: none;
}}
QTreeView::item:selected, QListView::item:selected {{
    background-color: {a['accent']};
    color: {a['text_on']};
}}
/* 中间表格选中行：浅底柔和高亮（原型 bg-blue-50），与左树实心选中区分 */
QTableView::item:selected {{
    background-color: {a['soft']};
    color: {text};
}}
QTableView::item {{
    color: {text};
}}
QTreeView::item:hover, QTableView::item:hover {{
    background-color: {rgba(mix(p.surface, p.text, 0.05), 1.0 if solid else panel_alpha)};
}}
QHeaderView::section {{
    background-color: {header};
    color: {muted};
    border: none;
    border-bottom: 1px solid {border};
    border-right: 1px solid {rgba(p.border, 0.5)};
    padding: 6px 6px;
    font-size: 8.5pt;
    font-weight: 600;
}}
QHeaderView::section:last {{
    border-right: none;
}}
QTableCornerButton::section {{
    background-color: {header};
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 1px;
}}
QScrollBar::handle:vertical {{
    background: {rgba(p.text_muted, 0.35)};
    border-radius: 3px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {rgba(p.text_muted, 0.6)};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 6px;
    margin: 1px;
}}
QScrollBar::handle:horizontal {{
    background: {rgba(p.text_muted, 0.35)};
    border-radius: 3px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {rgba(p.text_muted, 0.6)};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0; height: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}
QTabWidget::pane {{
    border: 1px solid {border};
    border-radius: {radius}px;
    background-color: {panel};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {muted};
    padding: 6px 14px;
    border: 1px solid transparent;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{
    color: {text};
    border-bottom: 2px solid {a['accent']};
}}
QTabBar::tab:hover {{
    color: {text};
}}
QGroupBox {{
    border: 1px solid {border};
    border-radius: {radius}px;
    margin-top: 14px;
    padding: 10px 8px 8px 8px;
    background-color: {panel};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {muted};
}}
QCheckBox, QRadioButton {{
    spacing: 6px;
}}
QRadioButton::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {rgba(p.text_muted, 0.8)};
    border-radius: 8px;
    background-color: {panel_alt};
}}
QRadioButton::indicator:checked {{
    background-color: {a['accent']};
    border-color: {a['accent']};
}}
QMenu {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 5px 22px 5px 14px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {a['soft']};
}}
QMenu::separator {{
    height: 1px;
    background: {p.border};
    margin: 4px 8px;
}}
QSplitter::handle {{
    background-color: transparent;
    width: 6px;
}}
QSplitter::handle:hover {{
    background-color: {rgba(a['accent'], 0.35)};
}}
QProgressBar {{
    background-color: {rgba(p.border, 0.6)};
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    color: {muted};
    font-size: 8pt;
}}
QProgressBar::chunk {{
    background-color: {a['accent']};
    border-radius: 3px;
}}
QStatusBar {{
    background-color: transparent;
    color: {muted};
}}
QStatusBar::item {{
    border: none;
}}
QListWidget {{
    background-color: transparent;
    border: none;
}}
QListWidget::item {{
    padding: 4px 6px;
    border-radius: 6px;
}}
QListWidget::item:selected {{
    background-color: {a['soft']};
    color: {text};
}}
QTextBrowser {{
    background-color: {panel_alt};
    border: 1px solid {border};
    border-radius: 6px;
}}
"""


def apply_app_theme(app: QApplication, theme: str, accent: str, panel_alpha: float = 1.0,
                    solid: bool = False) -> None:
    font = QFont(FONT_FAMILY, 9)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)
    app.setStyleSheet(build_stylesheet(theme, accent, panel_alpha, solid))


def paint_background(painter: QPainter, rect, pixmap: QPixmap | None, display: str,
                     align: str, intensity: float, mask_color: str, mask_alpha: float) -> None:
    """在主窗口底层绘制背景图与主题色遮罩。"""
    p = palette_for("light")
    painter.save()
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.fillRect(rect, QColor(mask_color))
    if pixmap is not None and not pixmap.isNull() and intensity > 0.01:
        target = QPixmap(pixmap)
        if display == "fill":
            scaled = target.scaled(rect.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            source_x = max(0, (scaled.width() - rect.width()) // 2)
            source_y = max(0, (scaled.height() - rect.height()) // 2)
            if "left" in align:
                source_x = 0
            elif "right" in align:
                source_x = max(0, scaled.width() - rect.width())
            if "top" in align:
                source_y = 0
            elif "bottom" in align:
                source_y = max(0, scaled.height() - rect.height())
            chunk = scaled.copy(source_x, source_y, rect.width(), rect.height())
            painter.setOpacity(intensity)
            painter.drawPixmap(rect.topLeft(), chunk)
        elif display == "fit":
            scaled = target.scaled(rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = (rect.width() - scaled.width()) // 2
            y = (rect.height() - scaled.height()) // 2
            if "left" in align:
                x = 0
            elif "right" in align:
                x = rect.width() - scaled.width()
            if "top" in align:
                y = 0
            elif "bottom" in align:
                y = rect.height() - scaled.height()
            painter.setOpacity(intensity)
            painter.drawPixmap(rect.topLeft() + type(rect.topLeft())(x, y), scaled)
        else:  # stretch
            scaled = target.scaled(rect.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            painter.setOpacity(intensity)
            painter.drawPixmap(rect.topLeft(), scaled)
    if mask_alpha > 0.01:
        painter.setOpacity(mask_alpha)
        painter.fillRect(rect, QColor(mask_color))
    painter.restore()


def brand_pixmap(size: int = 28, accent: str = "#2563EB") -> QPixmap:
    """顶栏品牌图标：蓝紫渐变圆角方块 + 白色音符。"""
    from .icons import draw_note
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0, QColor(accent))
    gradient.setColorAt(1, QColor(mix(accent, "#7C3AED", 0.55)))
    painter.setBrush(gradient)
    painter.drawRoundedRect(0, 0, size, size, size * 0.28, size * 0.28)
    draw_note(painter, size, "#FFFFFF")
    painter.end()
    return pix


def rounded_pixmap(source: QPixmap, radius: int = 8) -> QPixmap:
    """圆角封面图。"""
    if source.isNull():
        return source
    out = QPixmap(source.size())
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    path = QPainterPath()
    path.addRoundedRect(0, 0, source.width(), source.height(), radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, source)
    painter.end()
    return out
