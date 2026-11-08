"""右侧详情面板：专辑头部卡片、内嵌来源徽章的元数据表单、识别依据卡、随附文件。

对齐 UI 原型（docs/code_artifact.html §3.4）：
- 顶部：封面(96px 圆角) + 状态胶囊 + 音频规格 + 自然横排折行标题 + 艺术家 + 数量/大小；
- 元数据表单：标签在输入框上方，来源徽章（标签/CUE/目录名/用户/未知/整夹）内嵌输入框右侧；
- 识别依据：浅底圆角卡片（盾牌图标 + 章节名 + 边界徽章 + 绿点依据列表）；
- 随附文件：复选框卡片行，右侧类型徽章。

业务信号与 set_unit 契约与旧版完全一致，主窗口无需改动接线。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QScrollArea, QSizePolicy, QSpinBox, QStackedWidget,
                               QStyledItemDelegate, QStyle, QStyleOptionButton,
                               QStyleOptionViewItem, QVBoxLayout, QWidget)

from ..domain.models import FIELD_LABELS, Unit, UnitKind
from .icons import make_icon
from .models import STATUS_TONES
from .theme import MONO_FAMILY, rounded_pixmap, tones_for

FIELD_ORDER = ["title", "artist", "album", "album_artist", "track", "disc", "year", "genre"]

# 来源 → (徽章文字, 语气色)
ORIGIN_BADGES = {
    "tag": ("标签", "blue"),
    "cue": ("CUE", "blue"),
    "filename": ("目录名", "gray"),
    "user": ("用户", "green"),
    "": ("未知", "gray"),
}

ATTACHMENT_ROLE_TONES = {"cover": "green", "lyrics": "blue", "cue": "indigo"}

ROLE_PATH = Qt.UserRole
ROLE_TEXT = Qt.UserRole + 1   # 徽章文字
ROLE_TONE = Qt.UserRole + 2   # 语气色名


def _is_checked(raw) -> bool:
    """CheckStateRole 数据归一化判断。

    坑（附件勾选渲染回归锁）：PySide6 6.12 下 index.data(CheckStateRole)
    返回 int（2）而非 Qt.CheckState 枚举，`== Qt.Checked` 恒为 False；
    且 Qt 枚举不支持 int() 转换（TypeError）。统一经 .value 归一化，
    兼容 int / 枚举两种返回。
    """
    return int(getattr(raw, "value", raw) or 0) == int(Qt.CheckState.Checked.value)


def _set_tone(label: QLabel, tone: str) -> None:
    """切换语气色属性并重刷样式（动态属性需要重新 polish）。"""
    if label.property("tone") != tone:
        label.setProperty("tone", tone)
        label.style().unpolish(label)
        label.style().polish(label)


def _status_tone(unit: Unit) -> str:
    return STATUS_TONES.get(unit.status, "gray")


class BadgeEdit(QFrame):
    """内嵌来源徽章的输入框：徽章位于输入框右侧内部，不占外部列宽。"""

    def __init__(self, numeric: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("BadgeEdit")
        self.numeric = numeric
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 6, 2)
        layout.setSpacing(6)
        self.editor = QSpinBox() if numeric else QLineEdit()
        if numeric:
            self.editor.setRange(0, 999)
            self.editor.setSpecialValueText("—")
            self.editor.setFrame(False)
            self.editor.setButtonSymbols(QSpinBox.NoButtons)
            self.editor.setMinimumHeight(20)
        else:
            self.editor.setFrame(False)
            self.editor.setMinimumHeight(20)
        self.editor.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self.editor, 1)
        self.badge = QLabel("未知")
        self.badge.setObjectName("OriginBadge")
        self.badge.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.badge)
        _set_tone(self.badge, "gray")
        self._loading = False

    # ------------------------------------------------------------------
    def set_value(self, value, origin: str = "") -> None:
        self._loading = True
        if self.numeric:
            self.editor.setValue(int(value or 0))
        else:
            self.editor.setText("" if value is None else str(value))
        self.set_badge(*ORIGIN_BADGES.get(origin, ("未知", "gray")))
        self._loading = False

    def set_badge(self, text: str, tone: str) -> None:
        self.badge.setText(text)
        _set_tone(self.badge, tone)

    def text(self) -> str:
        return self.editor.text()

    def value(self) -> int:
        return self.editor.value() if self.numeric else 0

    def set_locked(self, badge_text: str = "整夹") -> None:
        """整夹模式下禁用编辑（如专辑的曲目号）。"""
        self.editor.setEnabled(False)
        self.setProperty("disabled", True)
        self.style().unpolish(self)
        self.style().polish(self)
        self.set_badge(badge_text, "gray")

    def set_unlocked(self) -> None:
        self.editor.setEnabled(True)
        self.setProperty("disabled", False)
        self.style().unpolish(self)
        self.style().polish(self)


class FieldRow(QWidget):
    """一个可编辑字段：标签在上、内嵌徽章输入框在下。"""

    edited = Signal(str, object)

    def __init__(self, field: str, label: str, numeric: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.field = field
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.caption = QLabel(label)
        self.caption.setObjectName("FieldLabel")
        layout.addWidget(self.caption)
        self.control = BadgeEdit(numeric=numeric)
        layout.addWidget(self.control)
        if numeric:
            self.control.editor.valueChanged.connect(self._emit_numeric)
        else:
            self.control.editor.editingFinished.connect(self._emit_text)
        self._loading = False

    def set_value(self, value, origin: str = "", locked: bool = False) -> None:
        self.control.set_value(value, origin)
        if locked:
            self.control.set_locked("整夹")
        else:
            self.control.set_unlocked()

    def _emit_text(self) -> None:
        if self.control._loading:
            return
        self.edited.emit(self.field, self.control.text().strip())

    def _emit_numeric(self) -> None:
        if self.control._loading:
            return
        value = self.control.value()
        self.edited.emit(self.field, value if value > 0 else None)


class CoverLabel(QLabel):
    """圆角封面：96px，无封面时优雅占位。"""

    def __init__(self, size: int = 96, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self.setObjectName("Cover")
        self._size = size
        self._placeholder()

    def _placeholder(self) -> None:
        self.setText("无封面")
        self.setWordWrap(True)
        self.setStyleSheet(
            "border: 1px solid rgba(128,128,128,0.30); border-radius: 8px;"
            "color: rgb(140,140,140); font-size: 8.5pt; background: rgba(128,128,128,0.08);")

    def set_image(self, path: str | Path | None) -> None:
        if not path:
            self.clear()
            self._placeholder()
            return
        pix = QPixmap(str(path))
        if pix.isNull():
            self.clear()
            self.setText("封面无法解析")
            return
        scaled = pix.scaled(self._size, self._size, Qt.KeepAspectRatioByExpanding,
                            Qt.SmoothTransformation)
        left = max(0, (scaled.width() - self._size) // 2)
        top = max(0, (scaled.height() - self._size) // 2)
        cropped = scaled.copy(left, top, self._size, self._size)
        self.setPixmap(rounded_pixmap(cropped, radius=8))
        self.setText("")
        self.setStyleSheet("")


class AttachmentDelegate(QStyledItemDelegate):
    """随附文件行：复选框 + 等宽文件名 + 右侧类型徽章。"""

    def __init__(self, theme_provider, parent=None) -> None:
        super().__init__(parent)
        self.theme_provider = theme_provider  # () -> theme name

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        tones = tones_for(self.theme_provider())
        rect = option.rect.adjusted(2, 1, -2, -1)
        if option.state & QStyle.State_Selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(tones["blue"][0]))
            painter.drawRoundedRect(rect, 6, 6)
        elif option.state & QStyle.State_MouseOver:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(tones["gray"][0]))
            painter.drawRoundedRect(rect, 6, 6)
        # 复选框
        opt = QStyleOptionButton()
        checked = _is_checked(index.data(Qt.CheckStateRole))
        opt.state = QStyle.State_Enabled
        opt.state |= QStyle.State_On if checked else QStyle.State_Off
        opt.rect = QRect(rect.left() + 4, rect.center().y() - 7, 14, 14)
        option.widget.style().drawControl(QStyle.CE_CheckBox, opt, painter)
        # 文件名（等宽、单行省略）
        name = str(index.data(Qt.DisplayRole) or "")
        font = QFont(MONO_FAMILY, 8)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        badge_text = str(index.data(ROLE_TEXT) or "")
        tone = tones.get(str(index.data(ROLE_TONE) or "gray"), tones["gray"])
        badge_w = metrics.horizontalAdvance(badge_text) + 14
        text_rect = QRect(rect.left() + 24, rect.top(), rect.width() - 24 - badge_w - 10,
                          rect.height())
        elided = metrics.elidedText(name, Qt.ElideMiddle, text_rect.width())
        painter.setPen(option.palette.text().color())
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, elided)
        # 类型徽章
        badge_rect = QRect(rect.right() - badge_w - 2, rect.center().y() - 8, badge_w, 16)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(tone[0]))
        painter.drawRoundedRect(badge_rect, 8, 8)
        painter.setPen(QColor(tone[1]))
        painter.drawText(badge_rect, Qt.AlignCenter, badge_text)
        painter.restore()


class EvidenceCard(QFrame):
    """识别依据卡片：盾牌图标 + 章节名 + 右侧徽章 + 结构化依据列表。"""

    def __init__(self, title: str, chip: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("EvidenceCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 9)
        layout.setSpacing(5)
        head = QHBoxLayout()
        head.setSpacing(6)
        icon = QLabel()
        icon.setPixmap(_tinted_pixmap("shield", 14, "#3B82F6"))
        icon.setFixedSize(16, 16)
        icon.setAlignment(Qt.AlignCenter)
        head.addWidget(icon)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("Section")
        head.addWidget(self.title_label)
        head.addStretch(1)
        self.chip_label = QLabel(chip)
        head.addWidget(self.chip_label)
        _set_tone(self.chip_label, "indigo")
        layout.addLayout(head)
        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.RichText)
        self.body.setObjectName("Muted")
        layout.addWidget(self.body)

    def set_lines(self, lines: list[tuple[str, str]], theme: str) -> None:
        """lines: [(标记, 文字)]，标记 ● green / · normal / ⚠ warn / ○ muted。"""
        tones = tones_for(theme)
        green = tones["green"][1]
        amber = tones["amber"][1]
        muted = tones["gray"][1]
        parts = []
        for mark, text in lines:
            if mark == "●":
                parts.append(f'<span style="color:{green}; font-weight:bold;">●</span> {text}')
            elif mark == "⚠":
                parts.append(f'<span style="color:{amber};">⚠ {text}</span>')
            elif mark == "○":
                parts.append(f'<span style="color:{muted};">○ {text}</span>')
            else:
                parts.append(f'<span style="color:{green};">·</span> {text}')
        self.body.setText("<br>".join(parts))


def _tinted_pixmap(name: str, size: int, color: str) -> QPixmap:
    from .icons import make_icon
    return make_icon(name, color, size=size).pixmap(size, size)


class DetailPanel(QWidget):
    fieldEdited = Signal(str, str, object)        # unit_id, field, value
    confirmRequested = Signal(str)
    unconfirmRequested = Signal(str)
    boundaryChangeRequested = Signal(str)          # unit_id → 让主窗口提供候选
    albumModeRequested = Signal(str, str)          # unit_id, 'album' | 'tracks'
    attachmentToggled = Signal(str, str, bool)     # unit_id, path, selected
    attachmentSetAllRequested = Signal(str, bool)  # unit_id, checked → 批量勾选/取消
    clearAttachmentsRequested = Signal(str)        # unit_id → 解除全部附件关联
    linkAttachmentRequested = Signal(str)
    linkLyricsRequested = Signal(str)              # unit_id → 选择歌词文件并关联
    lyricsPreviewRequested = Signal(str, str)      # unit_id, path → 只读预览
    openPath = Signal(str)
    previewBoundary = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.unit: Unit | None = None
        self.theme = "light"
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)

        # 空状态
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch(1)
        hint = QLabel("选择左侧或中间的条目查看详情")
        hint.setAlignment(Qt.AlignCenter)
        hint.setObjectName("Muted")
        empty_layout.addWidget(hint)
        empty_layout.addStretch(1)
        self.stack.addWidget(empty)

        self._views: dict[str, QWidget] = {}
        for kind in (UnitKind.ALBUM.value, UnitKind.TRACK.value, UnitKind.ARCHIVE.value,
                     UnitKind.ATTACHMENT.value):
            view = self._build_view(kind)
            self._views[kind] = view
            self.stack.addWidget(view)
        self.fields: dict[str, FieldRow] = {}
        self._build_common()
        self.set_unit(None)

    # ------------------------------------------------------------------
    def _scroll(self) -> tuple[QScrollArea, QVBoxLayout]:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(12)
        area.setWidget(body)
        return area, layout

    def _build_view(self, kind: str) -> QWidget:
        area, layout = self._scroll()
        holder = QWidget()
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(area)
        setattr(holder, "_layout", layout)
        return holder

    def _layout_for(self, kind: str) -> QVBoxLayout:
        return getattr(self._views[kind], "_layout")

    # ------------------------------------------------------------------
    def _build_media_header(self, kind: str, layout: QVBoxLayout) -> None:
        """顶部专辑头部卡片：封面 + 状态/规格 + 标题 + 艺术家 + 数量/大小。"""
        header = QHBoxLayout()
        header.setSpacing(12)
        cover = CoverLabel(96)
        header.addWidget(cover, 0, Qt.AlignTop)
        info = QVBoxLayout()
        info.setSpacing(3)
        badge_row = QHBoxLayout()
        badge_row.setSpacing(6)
        status_badge = QLabel("")
        status_badge.setAlignment(Qt.AlignVCenter)
        _set_tone(status_badge, "green")
        format_badge = QLabel("")
        format_badge.setObjectName("Muted")
        format_badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        badge_row.addWidget(status_badge)
        badge_row.addWidget(format_badge)
        badge_row.addStretch(1)
        info.addLayout(badge_row)
        title = QLabel("—")
        title.setObjectName("DetailTitle")
        title.setWordWrap(True)   # 自然横排折行，杜绝窄列挤压竖排
        subtitle = QLabel("")
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        meta = QLabel("")
        meta.setObjectName("Muted")
        meta.setWordWrap(True)
        info.addWidget(title)
        info.addWidget(subtitle)
        info.addWidget(meta)
        info.addStretch(1)
        header.addLayout(info, 1)
        layout.addLayout(header)
        setattr(self._views[kind], "cover", cover)
        setattr(self._views[kind], "status_badge", status_badge)
        setattr(self._views[kind], "format_badge", format_badge)
        setattr(self._views[kind], "title_label", title)
        setattr(self._views[kind], "subtitle_label", subtitle)
        setattr(self._views[kind], "audio_label", meta)

    def _metadata_section(self, kind: str, layout: QVBoxLayout) -> None:
        head = QHBoxLayout()
        head.setSpacing(4)
        caption = QLabel("目标元数据")
        caption.setObjectName("Section")
        head.addWidget(caption)
        head.addStretch(1)
        hint = QLabel("标签来源指示")
        hint.setObjectName("Muted")
        hint.setStyleSheet("font-size: 8pt;")
        head.addWidget(hint)
        layout.addLayout(head)

        rows: dict[str, FieldRow] = {}
        for field in FIELD_ORDER:
            rows[field] = FieldRow(field, FIELD_LABELS[field],
                                   numeric=field in ("track", "disc"))
            rows[field].edited.connect(self._on_field_edited)
            self.fields.setdefault(f"{kind}:{field}", rows[field])
        for field in ("title", "artist", "album", "album_artist"):
            layout.addWidget(rows[field])
        grid1 = QGridLayout()
        grid1.setHorizontalSpacing(10)
        grid1.setContentsMargins(0, 0, 0, 0)
        grid1.addWidget(rows["year"], 0, 0)
        grid1.addWidget(rows["genre"], 0, 1)
        layout.addLayout(grid1)
        grid2 = QGridLayout()
        grid2.setHorizontalSpacing(10)
        grid2.setContentsMargins(0, 0, 0, 0)
        grid2.addWidget(rows["track"], 0, 0)
        grid2.addWidget(rows["disc"], 0, 1)
        layout.addLayout(grid2)
        setattr(self._views[kind], "form_rows", rows)

    def _attachment_section(self, kind: str, layout: QVBoxLayout) -> None:
        """随附文件区：标题栏（计数/提示/全选切换）+ 列表 + 紧贴列表的 Action Bar。

        任务书（docs/code_artdfghasifact.md）§3：
        - 标题栏计数显示 勾选数/总数，右侧提供「全不选/全选」列表级快捷操作；
        - 操作按钮不再是无边界纯文本：+ 关联附件（次级按钮）/+ 关联歌词/
          预览歌词（状态化）/清空关联（危险弱化），顶部分割线紧跟列表。
        """
        # -- 标题行 --
        head = QHBoxLayout()
        head.setSpacing(6)
        caption = QLabel("随附文件 (0/0)")
        caption.setObjectName("Section")
        head.addWidget(caption)
        head.addStretch(1)
        hint = QLabel("选中后随整夹一并移动" if kind == UnitKind.ALBUM.value
                      else "选中后随歌曲一并移动")
        hint.setObjectName("Muted")
        hint.setStyleSheet("font-size: 8pt;")
        head.addWidget(hint)
        toggle_all_btn = QPushButton("全不选")
        toggle_all_btn.setProperty("kind", "ghost")
        toggle_all_btn.setStyleSheet("font-size: 8pt; padding: 2px 6px;")
        toggle_all_btn.setToolTip("批量切换全部随附文件的勾选状态")
        toggle_all_btn.setEnabled(False)
        toggle_all_btn.clicked.connect(self._toggle_all_attachments)
        head.addWidget(toggle_all_btn)
        layout.addLayout(head)
        setattr(self._views[kind], "attachment_caption", caption)
        setattr(self._views[kind], "att_toggle_all_btn", toggle_all_btn)

        att_list = QListWidget()
        att_list.setMinimumHeight(72)
        att_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        att_list.setItemDelegate(AttachmentDelegate(lambda: self.theme, att_list))
        att_list.itemChanged.connect(self._on_attachment_toggled)
        att_list.itemDoubleClicked.connect(self._on_attachment_activated)
        layout.addWidget(att_list)
        setattr(self._views[kind], "attachment_list", att_list)

        # -- Action Bar：顶部分割线 + 图标化按钮组，紧贴列表下方 --
        # 坑：详情面板是横向裁剪的 QScrollArea，四个默认内边距按钮在窄栏
        # （splitter 386px）下总宽溢出会被裁掉右侧 —— bar 内统一紧凑内边距，
        # "+" 语义由图标承担（原型 `[+ 关联附件]` 的 + 即图标，不重复写进文字）。
        bar = QFrame()
        bar.setObjectName("ActionBar")
        bar.setStyleSheet(
            "QPushButton { font-size: 8.5pt; padding: 3px 8px; }")
        buttons = QHBoxLayout(bar)
        buttons.setContentsMargins(0, 7, 0, 0)
        buttons.setSpacing(4)
        if kind == UnitKind.ALBUM.value:
            lyric_tip = ("选择歌词文件，随整夹原样保留（不改写、不转码）；"
                         "同目录 .lrc 扫描时已自动收录")
        else:
            lyric_tip = "选择 .lrc 歌词文件关联到这首歌曲（勾选后随歌曲移动）"
        link_btn = self._action_button("关联附件", "plus", "#2563EB", "soft",
                                       "选择一个或多个文件关联到当前条目（支持多选）")
        lyric_btn = self._action_button("关联歌词", "plus", "#8A8F98", "ghost", lyric_tip)
        preview_btn = self._action_button("预览歌词", "eye", "#8A8F98", "ghost",
                                          "只读预览已关联的歌词（可切换编码）")
        clear_btn = self._action_button("清空关联", "trash", "#C0392B", "danger",
                                        "解除全部附件关联（不删除磁盘文件，"
                                        "重新扫描可恢复自动识别）")
        preview_btn.setEnabled(False)
        clear_btn.setEnabled(False)
        link_btn.clicked.connect(lambda: self.linkAttachmentRequested.emit(self.unit.id))
        lyric_btn.clicked.connect(lambda: self.linkLyricsRequested.emit(self.unit.id))
        preview_btn.clicked.connect(self._preview_selected_lyrics)
        clear_btn.clicked.connect(self._clear_all_attachments)
        buttons.addWidget(link_btn)
        buttons.addWidget(lyric_btn)
        buttons.addWidget(preview_btn)
        buttons.addStretch(1)
        buttons.addWidget(clear_btn)
        layout.addWidget(bar)
        setattr(self._views[kind], "att_link_btn", link_btn)
        setattr(self._views[kind], "att_lyric_btn", lyric_btn)
        setattr(self._views[kind], "att_preview_btn", preview_btn)
        setattr(self._views[kind], "att_clear_btn", clear_btn)

    def _action_button(self, text: str, icon: str, color: str, btn_kind: str,
                       tooltip: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setProperty("kind", btn_kind)
        btn.setIcon(make_icon(icon, color, size=13))
        btn.setToolTip(tooltip)
        return btn

    def _path_card(self, kind: str, layout: QVBoxLayout) -> None:
        box = QFrame()
        box.setObjectName("PathCard")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(10, 8, 10, 8)
        box_layout.setSpacing(4)
        src_row = QHBoxLayout()
        src_caption = QLabel("来源")
        src_caption.setObjectName("FieldLabel")
        src_caption.setFixedWidth(28)
        src_value = QLineEdit()
        src_value.setReadOnly(True)
        src_value.setFrame(False)
        src_value.setObjectName("PathText")
        src_btn = QPushButton("打开")
        src_btn.setProperty("kind", "ghost")
        src_btn.clicked.connect(lambda: self.openPath.emit(src_value.text()))
        src_row.addWidget(src_caption)
        src_row.addWidget(src_value, 1)
        src_row.addWidget(src_btn)
        tgt_row = QHBoxLayout()
        tgt_caption = QLabel("目标")
        tgt_caption.setObjectName("FieldLabel")
        tgt_caption.setFixedWidth(28)
        tgt_value = QLineEdit()
        tgt_value.setReadOnly(True)
        tgt_value.setFrame(False)
        tgt_value.setObjectName("PathText")
        tgt_row.addWidget(tgt_caption)
        tgt_row.addWidget(tgt_value, 1)
        box_layout.addLayout(src_row)
        box_layout.addLayout(tgt_row)
        layout.addWidget(box)
        setattr(self._views[kind], "source_value", src_value)
        setattr(self._views[kind], "target_value", tgt_value)

    # ------------------------------------------------------------------
    def _build_common(self) -> None:
        for kind in (UnitKind.ALBUM.value, UnitKind.TRACK.value):
            layout = self._layout_for(kind)
            self._build_media_header(kind, layout)
            self._metadata_section(kind, layout)

            if kind == UnitKind.ALBUM.value:
                self.evidence_card = EvidenceCard("识别依据", "最小合理边界")
                layout.addWidget(self.evidence_card)
                setattr(self._views[kind], "evidence_card", self.evidence_card)

                self._attachment_section(kind, layout)

                layout.addWidget(self._section_title("CUE 虚拟曲目（不可单独执行）"))
                cue_list = QListWidget()
                cue_list.setMaximumHeight(110)
                layout.addWidget(cue_list)
                setattr(self._views[kind], "virtual_list", cue_list)

                actions = QHBoxLayout()
                actions.setSpacing(4)
                self.btn_boundary = QPushButton("改选上级/子级…")
                self.btn_boundary.setProperty("kind", "ghost")
                self.btn_boundary.clicked.connect(
                    lambda: self.boundaryChangeRequested.emit(self.unit.id))
                self.btn_tracks = QPushButton("按散曲整理")
                self.btn_tracks.setProperty("kind", "ghost")
                self.btn_tracks.clicked.connect(
                    lambda: self.albumModeRequested.emit(self.unit.id, "tracks"))
                for btn in (self.btn_boundary, self.btn_tracks):
                    actions.addWidget(btn)
                actions.addStretch(1)
                layout.addLayout(actions)

            if kind == UnitKind.TRACK.value:
                # 专辑识别（元数据直读）：识别结果、来源与实际依据
                self.album_evidence_card = EvidenceCard("专辑识别", "元数据直读")
                layout.addWidget(self.album_evidence_card)
                setattr(self._views[kind], "evidence_card", self.album_evidence_card)
                self._attachment_section(kind, layout)

            self._path_card(kind, layout)
            getattr(self._views[kind], "_layout").addStretch(1)

        # 压缩包视图（全自动：无解压/归档选择按钮）
        layout = self._layout_for(UnitKind.ARCHIVE.value)
        title = QLabel("压缩包")
        title.setObjectName("Title")
        title.setWordWrap(True)
        layout.addWidget(title)
        self.archive_title = title
        self.archive_info = QLabel("")
        self.archive_info.setWordWrap(True)
        self.archive_info.setObjectName("Muted")
        layout.addWidget(self.archive_info)
        self.archive_note = QLabel("")
        self.archive_note.setWordWrap(True)
        layout.addWidget(self.archive_note)
        self._path_card(UnitKind.ARCHIVE.value, layout)
        layout.addStretch(1)

        # 附件视图
        layout = self._layout_for(UnitKind.ATTACHMENT.value)
        title = QLabel("未关联附件")
        title.setObjectName("Title")
        layout.addWidget(title)
        self.attachment_info = QLabel("")
        self.attachment_info.setWordWrap(True)
        layout.addWidget(self.attachment_info)
        layout.addStretch(1)

    def _section_title(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Section")
        return label

    # ------------------------------------------------------------------
    def _on_field_edited(self, field: str, value) -> None:
        if self.unit is None:
            return
        self.fieldEdited.emit(self.unit.id, field, value)

    def _toggle_all_attachments(self) -> None:
        """标题栏「全不选/全选」：按当前勾选状态整体切换，交由主窗口批量落账。"""
        if self.unit is None:
            return
        any_checked = any(a.get("selected", True) for a in self.unit.attachments)
        self.attachmentSetAllRequested.emit(self.unit.id, not any_checked)

    def _clear_all_attachments(self) -> None:
        if self.unit is None:
            return
        self.clearAttachmentsRequested.emit(self.unit.id)

    def _on_attachment_toggled(self, item: QListWidgetItem) -> None:
        if self.unit is None:
            return
        path = item.data(Qt.UserRole)
        selected = item.checkState() == Qt.Checked
        self.attachmentToggled.emit(self.unit.id, path, selected)

    # ------------------------------------------------------------------
    def set_theme(self, theme: str) -> None:
        self.theme = theme

    def set_unit(self, unit: Unit | None, target_preview: str = "") -> None:
        self.unit = unit
        if unit is None:
            self.stack.setCurrentIndex(0)
            return
        kind = unit.kind
        if kind not in self._views:
            kind = UnitKind.TRACK.value
        self.stack.setCurrentWidget(self._views[kind])
        if kind == UnitKind.ARCHIVE.value:
            self._fill_archive(unit, target_preview)
        elif kind == UnitKind.ATTACHMENT.value:
            self._fill_attachment(unit)
        else:
            self._fill_media(unit, kind, target_preview)

    def _fill_media(self, unit: Unit, kind: str, target_preview: str) -> None:
        view = self._views[kind]
        # 状态胶囊 + 音频规格
        _set_tone(view.status_badge, _status_tone(unit))
        view.status_badge.setText(f"● {unit.status_enum.label}")
        if kind == UnitKind.TRACK.value:
            from ..scan.audio import AudioMeta
            meta = AudioMeta(fmt=unit.fmt, duration=unit.duration, bitrate=unit.bitrate,
                             sample_rate=unit.sample_rate, bits=unit.bits)
            spec = meta.describe_audio() or "音频参数未知"
        else:
            spec = (f"{unit.fmt} {unit.bits}bit" if unit.fmt else "")
            if spec and unit.sample_rate:
                spec += f" {round(unit.sample_rate / 1000, 1)}kHz"
            spec = spec or "音频"
        view.format_badge.setText(spec)
        # 主标题：专辑用专辑名，散曲用歌名（自然横排折行）
        view.title_label.setText(unit.album if kind == UnitKind.ALBUM.value else
                                 (unit.title or unit.name))
        if kind == UnitKind.ALBUM.value:
            view.subtitle_label.setText(unit.album_artist or "未指定专辑艺术家")
        else:
            view.subtitle_label.setText(unit.artist or "未知艺术家")
        # 数量 / 大小行
        if kind == UnitKind.ALBUM.value:
            parts = [f"{len(unit.audio_files)} 个音频文件" if unit.audio_files else ""]
            if unit.total_discs > 1:
                parts.append(f"{unit.total_discs} 碟")
        else:
            parts = []
        if unit.size_hint():
            parts.append(unit.size_hint())
        if unit.duration > 0 and kind == UnitKind.TRACK.value:
            parts.append(self._fmt_duration(unit.duration))
        view.audio_label.setText(" · ".join([b for b in parts if b]))

        cover = self._find_cover(unit)
        view.cover.set_image(cover)
        for field in FIELD_ORDER:
            row = self.fields.get(f"{kind}:{field}")
            if row is None:
                continue
            if kind == UnitKind.ALBUM.value and field in ("title", "artist"):
                row.setVisible(False)
                continue
            row.setVisible(True)
            if kind == UnitKind.ALBUM.value and field == "track":
                # 整夹模式：曲目号不适用，锁定并显示「整夹」徽章
                row.set_value(None, "", locked=True)
                continue
            row.set_value(getattr(unit, field), unit.origins.get(field, ""))

        if kind == UnitKind.ALBUM.value:
            self._fill_album_extras(unit)
        if kind == UnitKind.TRACK.value:
            self._fill_track_album_evidence(unit)
        self._fill_attachment_list(unit)

        view.source_value.setText(unit.boundary_path if unit.is_album else unit.path)
        view.target_value.setText(target_preview or unit.last_target or "（未生成预览）")

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        total = int(seconds)
        return f"{total // 60}:{total % 60:02d}"

    def _fill_track_album_evidence(self, unit: Unit) -> None:
        """散曲的专辑识别结果：识别名/来源/依据/提醒（用户编辑后仍可见）。"""
        lines: list[tuple[str, str]] = []
        origin = (unit.origins or {}).get("album", "")
        if (unit.album or "").strip() and unit.album != "未分类专辑":
            label = {"tag": "标签", "filename": "目录名"}.get(origin, "自动识别")
            lines.append(("●", f"<b>识别专辑</b>：{unit.album}（{label}）"))
            if (unit.year or "").strip():
                year_origin = unit.origins.get("year", "")
                suffix = "（目录名）" if year_origin == "filename" else ""
                lines.append(("·", f"年份：{unit.year}{suffix}"))
            if (unit.album_artist or "").strip():
                lines.append(("·", f"整理艺术家：{unit.album_artist}"))
        else:
            lines.append(("○", "未识别到专辑：目标将使用“未分类专辑”目录"))
        for item in unit.evidence:
            lines.append(("·", item))
        warnings = [m for m in unit.messages
                    if ("专辑" in m or "年份" in m or "曲号" in m)
                    and not m.startswith("缺少")]
        for item in warnings[:3]:
            lines.append(("⚠", item))
        view = self._views[UnitKind.TRACK.value]
        view.evidence_card.set_lines(lines, self.theme)

    def _fill_album_extras(self, unit: Unit) -> None:
        view = self._views[UnitKind.ALBUM.value]
        lines: list[tuple[str, str]] = []
        if unit.boundary_confirmed:
            lines.append(("●", "<b>已确认整夹范围</b>：内部名称与层级保持原样"))
        else:
            lines.append(("○", "尚未确认整夹范围"))
        for item in unit.evidence:
            lines.append(("·", item))
        if unit.manual_note:
            lines.append(("⚠", unit.manual_note))
        view.evidence_card.set_lines(lines, self.theme)
        chip = view.evidence_card.chip_label
        if unit.auto_boundary:
            chip.setText("自动边界")
        else:
            chip.setText("整夹范围")
        _set_tone(chip, "indigo")

        cue_list = view.virtual_list
        cue_list.clear()
        if unit.virtual_tracks:
            for track in unit.virtual_tracks:
                cue_list.addItem(f"{int(track.get('number', 0)):02d}　{track.get('title') or '（无标题）'}")
        else:
            cue_list.addItem("（无整轨 CUE 虚拟曲目）")
        cue_list.setEnabled(bool(unit.virtual_tracks))

        self.btn_tracks.setEnabled(True)

    def _fill_archive(self, unit: Unit, target_preview: str) -> None:
        info = unit.archive or {}
        self.archive_title.setText(unit.name)
        lines = ["状态：待整理（全自动归档）",
                 f"大小：{unit.size_hint() or '未知'}"]
        if info.get("multivolume"):
            lines.append("分卷压缩包：整组按规则自动归档（不拆解）")
        lines.append("处理方式：不解压、不改名；同目录音频有唯一归属时随音频走，"
                     "否则放入目标“原始压缩包”目录")
        self.archive_info.setText("\n".join(lines))
        self.archive_note.setText(unit.name)
        self._views[UnitKind.ARCHIVE.value].target_value.setText(
            target_preview or unit.last_target or "（未生成预览）")
        self._views[UnitKind.ARCHIVE.value].source_value.setText(unit.path)

    def _fill_attachment(self, unit: Unit) -> None:
        att = unit.attachments[0] if unit.attachments else {}
        self.attachment_info.setText(
            f"{unit.path}\n\n类型：{self._role_label(att)}\n说明：{att.get('note', '')}\n\n"
            "未关联附件不会被自动移动。可在专辑或歌曲详情里手动关联。")

    def _role_label(self, att: dict) -> str:
        from ..domain.models import Attachment
        return Attachment.ROLE_LABELS.get(att.get("role", "misc"), "其他附件")

    def _fill_attachment_list(self, unit: Unit) -> None:
        """附件/歌词列表：右侧类型徽章；歌词附编码/待确认提示于 tooltip。

        勾选状态绑定附件对象的 selected 字段（AC1/AC2）：自动识别与手动
        关联的附件默认 selected=True → 复选框默认打勾；标题计数显示
        勾选数/总数。刷新后按路径恢复当前行，避免“当前行丢失”。
        """
        kind = unit.kind if unit.kind in self._views else UnitKind.TRACK.value
        att_list = getattr(self._views[kind], "attachment_list", None)
        if att_list is None:
            return
        caption = getattr(self._views[kind], "attachment_caption", None)
        current = att_list.currentItem()
        current_path = str(current.data(Qt.UserRole) or "") if current is not None else ""
        att_list.blockSignals(True)
        att_list.clear()
        for att in unit.attachments:
            label = Path(att['path']).name
            if att.get("role") == "lyrics":
                enc = att.get("lyrics_encoding") or ""
                if enc:
                    label += f"（{enc}）"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if att.get("selected", True) else Qt.Unchecked)
            item.setData(Qt.UserRole, att["path"])
            item.setData(ROLE_TEXT, self._role_label(att))
            item.setData(ROLE_TONE, ATTACHMENT_ROLE_TONES.get(att.get("role"), "gray"))
            tips = [att["path"]]
            if att.get("note"):
                tips.append(att["note"])
            if att.get("owner_hint"):
                tips.append("对应：" + att["owner_hint"])
            if att.get("role") == "lyrics":
                if att.get("has_lrc_timestamps"):
                    tips.append("LRC 时间轴")
                if att.get("link_status") == "pending":
                    tips.append("待确认")
                elif att.get("match_confidence") == "manual":
                    tips.append("手动关联")
            evidence = att.get("match_evidence") or []
            if evidence:
                tips.append("匹配依据：" + "；".join(evidence[:5]))
            item.setToolTip("\n".join(tips))
            att_list.addItem(item)
        if current_path:
            for i in range(att_list.count()):
                if str(att_list.item(i).data(Qt.UserRole) or "") == current_path:
                    att_list.setCurrentRow(i)
                    break
        att_list.blockSignals(False)
        # 高度随内容收敛（上限 208px）：QListWidget 默认 Expanding 会把
        # 紧随其后的 Action Bar 挤出可视区，形成任务书抱怨的“断层留白”。
        row_h = att_list.sizeHintForRow(0) or 24
        att_list.setFixedHeight(min(max(72, att_list.count() * (row_h + 1) + 10), 208))
        if caption is not None:
            checked = sum(1 for a in unit.attachments if a.get("selected", True))
            caption.setText(f"随附文件 ({checked}/{len(unit.attachments)})")
        self._update_attachment_actions(unit)

    def _update_attachment_actions(self, unit: Unit) -> None:
        """Action Bar 状态化（AC4/AC5）：预览歌词依赖已关联歌词，清空关联
        与全选切换在列表为空时禁用，全不选/全选文案随勾选状态切换。"""
        kind = unit.kind if unit.kind in self._views else UnitKind.TRACK.value
        view = self._views[kind]
        total = len(unit.attachments)
        checked = sum(1 for a in unit.attachments if a.get("selected", True))
        has_lyrics = any(a.get("role") == "lyrics" for a in unit.attachments)
        toggle = getattr(view, "att_toggle_all_btn", None)
        if toggle is not None:
            toggle.setEnabled(total > 0)
            toggle.setText("全不选" if checked else "全选")
        preview = getattr(view, "att_preview_btn", None)
        if preview is not None:
            preview.setEnabled(has_lyrics)
        clear = getattr(view, "att_clear_btn", None)
        if clear is not None:
            clear.setEnabled(total > 0)

    def _selected_attachment_path(self) -> str:
        if self.unit is None:
            return ""
        kind = self.unit.kind if self.unit.kind in self._views else UnitKind.TRACK.value
        att_list = getattr(self._views[kind], "attachment_list", None)
        if att_list is None:
            return ""
        item = att_list.currentItem()
        return "" if item is None else str(item.data(Qt.UserRole) or "")

    def _preview_selected_lyrics(self) -> None:
        if self.unit is None:
            return
        path = self._selected_attachment_path()
        if not path:
            for att in self.unit.attachments:
                if att.get("role") == "lyrics":
                    path = att["path"]
                    break
        if not path:
            return
        self.lyricsPreviewRequested.emit(self.unit.id, path)

    def _on_attachment_activated(self, item: QListWidgetItem) -> None:
        if self.unit is None:
            return
        path = str(item.data(Qt.UserRole) or "")
        for att in self.unit.attachments:
            if att.get("path") == path and att.get("role") == "lyrics":
                self.lyricsPreviewRequested.emit(self.unit.id, path)
                return

    def _find_cover(self, unit: Unit) -> str | None:
        candidates = [a["path"] for a in unit.attachments if a.get("role") == "cover"]
        if not candidates and unit.is_album:
            candidates = [a["path"] for a in unit.attachments if a.get("role") == "scans"]
        for path in candidates:
            if Path(path).suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
                return path
        return None
