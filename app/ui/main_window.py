"""主窗口：现代三栏工作台布局。

五段式结构（对齐 UI 原型 docs/code_artifact.html）：
- 顶部全局导航栏：品牌 + 模式徽章 + 全局搜索 + 查重/歌词/历史 + 主题/面板/设置；
- 左侧来源目录树（含「全部来源目录」入口、悬停移除、底部路径提示）；
- 中间状态胶囊筛选 + 自适应无横向滚动条表格（表头复选框=全选当前视图）；
- 右侧详情与元数据检视面板（可折叠）；
- 底部黄金执行栏：输出路径胶囊 + 整理方式分段控制 + 全局汇总 + 唯一主执行按钮。

业务接线（服务层调用、信号槽、任务管理）与旧版完全一致。
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QEventLoop, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
                               QSplitter, QTableView, QTreeView, QVBoxLayout, QWidget)

from ..domain import naming
from ..domain.models import Unit
from ..planning.plan import Plan
from ..planning.review import group_of
from ..services import AppService
from .detail import DetailPanel
from .history_dialog import HistoryDialog
from .icons import make_icon, multi_color_icon
from .models import (CheckDelegate, CheckHeaderView, SourceTreeModel,
                     StatusDelegate, TreeHoverDelegate, UnitTableModel, human_size)
from .settings_dialog import SettingsDialog
from .theme import (THEME_ORDER, apply_app_theme, brand_pixmap, build_stylesheet,
                    palette_for, paint_background, tones_for)
from .workers import TaskRunner


def _set_tone(label: QLabel, tone: str) -> None:
    """切换 QLabel 语气色属性并重刷样式。"""
    if label.property("tone") != tone:
        label.setProperty("tone", tone)
        label.style().unpolish(label)
        label.style().polish(label)


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _sync_pill(btn: QPushButton) -> None:
    """同步分段胶囊按钮的激活态样式属性。"""
    btn.setProperty("pillActive", bool(btn.isChecked()))
    _repolish(btn)


class ClickLabel(QLabel):
    """可点击徽标（查重状态）：点击触发与按钮一致的处理流程。"""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class Toast(QFrame):
    """轻提示：悬浮主窗口顶部，自动消失，可携带动作按钮（如「打开文件夹」）。"""

    def __init__(self, parent, text: str, actions=None,
                 duration_ms: int = 12000, theme: str = "light") -> None:
        super().__init__(parent)
        pal = palette_for(theme)
        self.setObjectName("Toast")
        self.setStyleSheet(
            f"QFrame#Toast {{ background: {pal.surface}; border: 1px solid {pal.border};"
            f" border-radius: 10px; }}")
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 9, 10, 9)
        row.setSpacing(10)
        label = QLabel(text)
        label.setWordWrap(True)
        row.addWidget(label, 1)
        for action_text, callback in (actions or []):
            btn = QPushButton(action_text)
            btn.setProperty("kind", "soft")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(callback)
            btn.clicked.connect(self._dismiss)
            row.addWidget(btn)
        close_btn = QPushButton("×")
        close_btn.setFixedSize(22, 22)
        close_btn.setProperty("kind", "ghost")
        close_btn.clicked.connect(self._dismiss)
        row.addWidget(close_btn)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._dismiss)
        self._timer.start(duration_ms)

    def _dismiss(self) -> None:
        self.close()
        self.deleteLater()


class MainWindow(QMainWindow):
    def __init__(self, service: AppService) -> None:
        super().__init__()
        self.service = service
        self.runner = TaskRunner(self)
        self._bg_pixmap: QPixmap | None = None
        self._current_plan: Plan | None = None
        self._busy = False
        self._last_summary = None
        self._dup_running = False
        self._tone_colors = tones_for(service.settings.theme)
        # 勾选/编辑等高频操作合并写盘：状态文件随单元数增长，每次点击全量
        # 序列化+fsync 在千级条目时可感知卡顿；400ms 内的连续改动只落盘一次
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.service.save_state)

        self.setWindowTitle("音乐整理工具")
        self.setWindowIcon(multi_color_icon())
        self.resize(1440, 880)
        self.setMinimumSize(1100, 660)

        self._build_ui()
        self._build_shortcuts()
        self._apply_theme()
        self._load_background()
        self._restore_geometry()
        self._refresh_all()
        QTimer.singleShot(300, self._startup_notes)
    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("Central")
        central.setAttribute(Qt.WA_TranslucentBackground, True)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)

        root.addWidget(self._build_header())
        root.addWidget(self._build_body(), 1)
        root.addWidget(self._build_footer())
        self.setCentralWidget(central)

    # ------------------------------ 顶部导航栏 ------------------------------
    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("HeaderBar")
        bar.setFixedHeight(52)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 8, 10, 8)
        h.setSpacing(8)

        # 左侧品牌与工作流模式
        self.brand_icon = QLabel()
        self.brand_icon.setFixedSize(28, 28)
        h.addWidget(self.brand_icon)
        brand_title = QLabel("音乐整理工具")
        brand_title.setObjectName("BrandTitle")
        h.addWidget(brand_title)
        self.mode_badge = QLabel("智能最小边界模式")
        _set_tone(self.mode_badge, "blue")
        self.mode_badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        h.addWidget(self.mode_badge)
        h.addSpacing(6)

        # 中间全局搜索框（两侧弹性居中，宽度上限对齐原型 max-w-md）
        h.addStretch(1)
        search_box = QFrame()
        search_box.setObjectName("SearchBox")
        sh = QHBoxLayout(search_box)
        sh.setContentsMargins(9, 3, 9, 3)
        sh.setSpacing(6)
        search_icon = QLabel()
        search_icon.setPixmap(make_icon("search", "#94A3B8", size=14).pixmap(14, 14))
        sh.addWidget(search_icon)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索名称 / 艺术家 / 专辑 / 路径… (Ctrl+F)")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFrame(False)
        self.search_edit.textChanged.connect(self._on_search)
        sh.addWidget(self.search_edit, 1)
        kbd = QLabel("Ctrl K")
        kbd.setObjectName("Kbd")
        sh.addWidget(kbd)
        search_box.setMaximumWidth(460)
        search_box.setMinimumWidth(260)
        h.addWidget(search_box)
        h.addStretch(1)

        # 右侧低频辅助工具组
        self.btn_dup = QPushButton("查重")
        self.btn_dup.setProperty("kind", "ghost")
        self.btn_dup.setIcon(make_icon("grid", "#D97706"))
        self.btn_dup.setToolTip("查重与隔离：未检测时先检测；已有结果时打开隔离面板"
                                "（把重复副本安全移入「a重复歌曲」）")
        self.btn_dup.clicked.connect(self._duplicate_entry)
        h.addWidget(self.btn_dup)
        self.dup_badge = ClickLabel("未检")
        _set_tone(self.dup_badge, "gray")
        self.dup_badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.dup_badge.setCursor(Qt.PointingHandCursor)
        self.dup_badge.setToolTip("重复检测结果；有重复时点击可打开隔离面板")
        self.dup_badge.clicked.connect(self._duplicate_entry)
        h.addWidget(self.dup_badge)

        self.btn_lyrics = QPushButton("歌词")
        self.btn_lyrics.setProperty("kind", "ghost")
        self.btn_lyrics.setIcon(make_icon("note", "#8A8F98"))
        self.btn_lyrics.clicked.connect(self._open_lyrics)
        h.addWidget(self.btn_lyrics)
        self.lyrics_badge = QLabel("0")
        _set_tone(self.lyrics_badge, "amber")
        self.lyrics_badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.lyrics_badge.setVisible(False)
        h.addWidget(self.lyrics_badge)

        self.btn_history = QPushButton("历史")
        self.btn_history.setProperty("kind", "ghost")
        self.btn_history.setIcon(make_icon("history", "#8A8F98"))
        self.btn_history.clicked.connect(self._open_history)
        h.addWidget(self.btn_history)

        sep = QFrame()
        sep.setFixedSize(1, 18)
        sep.setStyleSheet("background: rgba(128,128,128,0.35); border: none;")
        h.addWidget(sep)

        self.btn_theme = QPushButton()
        self.btn_theme.setObjectName("IconBtn")
        self.btn_theme.setToolTip("切换主题：浅色 → 纸质 → 深色")
        self.btn_theme.clicked.connect(self._cycle_theme)
        h.addWidget(self.btn_theme)

        self.btn_inspector = QPushButton()
        self.btn_inspector.setObjectName("IconBtn")
        self.btn_inspector.setCheckable(True)
        self.btn_inspector.setChecked(True)
        self.btn_inspector.setIcon(make_icon("panel", "#2563EB"))
        self.btn_inspector.setToolTip("展开/收起右侧详情面板")
        self.btn_inspector.toggled.connect(lambda on: self.right_panel.setVisible(on))
        h.addWidget(self.btn_inspector)

        self.btn_settings = QPushButton()
        self.btn_settings.setObjectName("IconBtn")
        self.btn_settings.setIcon(make_icon("settings", "#8A8F98"))
        self.btn_settings.setToolTip("设置（模板 / 曲号 / 校验 / 主题）")
        self.btn_settings.clicked.connect(self._open_settings)
        h.addWidget(self.btn_settings)
        return bar

    # ------------------------------ 主体三栏 ------------------------------
    def _build_body(self) -> QWidget:
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)

        # ---------- 2.1 左侧：来源目录树 ----------
        left = QFrame()
        left.setObjectName("Card")
        left.setMinimumWidth(216)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(10, 10, 8, 8)
        left_layout.setSpacing(6)

        left_header = QHBoxLayout()
        left_header.setSpacing(4)
        left_title = QLabel("来源目录")
        left_title.setObjectName("Section")
        left_header.addWidget(left_title)
        self.source_count_label = QLabel("0 组")
        _set_tone(self.source_count_label, "gray")
        self.source_count_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        left_header.addWidget(self.source_count_label)
        left_header.addStretch(1)
        self.btn_add = QPushButton("添加")
        self.btn_add.setProperty("kind", "soft")
        self.btn_add.setIcon(make_icon("plus", "#2563EB", size=13))
        self.btn_add.setToolTip("添加来源文件夹（Ctrl+O）")
        self.btn_add.clicked.connect(self._add_source)
        left_header.addWidget(self.btn_add)
        self.btn_rescan = QPushButton()
        self.btn_rescan.setObjectName("IconBtn")
        self.btn_rescan.setIcon(make_icon("refresh", "#8A8F98"))
        self.btn_rescan.setToolTip("重新扫描全部来源（F5）")
        self.btn_rescan.clicked.connect(lambda: self._scan(initial=False))
        left_header.addWidget(self.btn_rescan)
        self.btn_clear_list = QPushButton()
        self.btn_clear_list.setObjectName("IconBtn")
        self.btn_clear_list.setProperty("kind", "danger")
        self.btn_clear_list.setIcon(make_icon("trash", "#8A8F98"))
        self.btn_clear_list.setToolTip("清空列表（不删除磁盘上的音乐文件）")
        self.btn_clear_list.clicked.connect(self._clear_list)
        left_header.addWidget(self.btn_clear_list)
        left_layout.addLayout(left_header)

        # 「全部来源目录」固定入口
        self.btn_all_sources = QPushButton()
        self.btn_all_sources.setObjectName("AllSourcesBtn")
        all_layout = QHBoxLayout(self.btn_all_sources)
        all_layout.setContentsMargins(6, 2, 6, 2)
        all_layout.setSpacing(8)
        self.all_sources_icon = QLabel()
        self.all_sources_icon.setPixmap(make_icon("grid", "#8A8F98", size=15).pixmap(15, 15))
        all_layout.addWidget(self.all_sources_icon)
        all_caption = QLabel("全部来源目录")
        all_layout.addWidget(all_caption, 1)
        self.all_count_label = QLabel("0")
        self.all_count_label.setObjectName("Kbd")
        all_layout.addWidget(self.all_count_label)
        self.btn_all_sources.clicked.connect(self._reset_scope)
        left_layout.addWidget(self.btn_all_sources)

        self.tree_model = SourceTreeModel(self)
        self.tree_model.checkToggled.connect(self._on_tree_check)
        self.tree = QTreeView()
        self.tree.setModel(self.tree_model)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.clicked.connect(self._on_tree_clicked)
        self.tree.setIconSize(QSize(16, 16))
        self.tree_delegate = TreeHoverDelegate(self.tree)
        self.tree_delegate.removeRequested.connect(self._remove_source)
        self.tree.setItemDelegateForColumn(0, self.tree_delegate)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        left_layout.addWidget(self.tree, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(4)
        self.scope_label = QLabel("当前范围：全部")
        self.scope_label.setObjectName("Muted")
        bottom.addWidget(self.scope_label, 1)
        self.btn_show_all = QPushButton("看全部")
        self.btn_show_all.setProperty("kind", "ghost")
        self.btn_show_all.setToolTip("显示全部来源的项目")
        self.btn_show_all.clicked.connect(self._reset_scope)
        self.btn_show_all.setVisible(False)
        bottom.addWidget(self.btn_show_all)
        left_layout.addLayout(bottom)
        self.splitter.addWidget(left)

        # ---------- 2.2 中间：筛选胶囊 + 数据表格 ----------
        center = QFrame()
        center.setObjectName("Card")
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)

        top_bar = QWidget()
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 8, 12, 8)
        top_layout.setSpacing(8)
        seg = QFrame()
        seg.setObjectName("Segmented")
        seg_layout = QHBoxLayout(seg)
        seg_layout.setContentsMargins(3, 2, 3, 2)
        seg_layout.setSpacing(2)
        self.btn_group_all = QPushButton("全部 0")
        self.btn_group_ready = QPushButton("可整理 0")
        self.btn_group_needs = QPushButton("需处理 0")
        self.btn_group_side = QPushButton("附属 0")
        for btn in (self.btn_group_all, self.btn_group_ready, self.btn_group_needs,
                    self.btn_group_side):
            btn.setCheckable(True)
            btn.setProperty("kind", "pill")
            btn.toggled.connect(self._on_group_toggled)
            seg_layout.addWidget(btn)
        self.btn_group_all.setToolTip("全部条目（含附属项）")
        self.btn_group_ready.setToolTip("能生成合理目标路径、没有已知阻止问题；新项目默认勾选")
        self.btn_group_needs.setToolTip("来源失联等需要关注的条目（执行时跳过并写入报告）")
        self.btn_group_side.setToolTip("已完成项与随整夹处理的子项（不计入待整理数量）")
        self.btn_group_all.blockSignals(True)
        self.btn_group_all.setChecked(True)
        self.btn_group_all.blockSignals(False)
        for btn in (self.btn_group_all, self.btn_group_ready, self.btn_group_needs,
                    self.btn_group_side):
            _sync_pill(btn)
        top_layout.addWidget(seg)
        top_layout.addStretch(1)

        # 当前目录筛选胶囊（有目录范围时显示）
        self.scope_badge = QFrame()
        self.scope_badge.setObjectName("ScopeBadge")
        badge_layout = QHBoxLayout(self.scope_badge)
        badge_layout.setContentsMargins(9, 2, 3, 2)
        badge_layout.setSpacing(2)
        self.scope_badge_text = QLabel("")
        badge_layout.addWidget(self.scope_badge_text)
        self.scope_badge_close = QPushButton("×")
        self.scope_badge_close.setObjectName("CloseBadge")
        self.scope_badge_close.setFixedWidth(18)
        self.scope_badge_close.setToolTip("显示全部")
        self.scope_badge_close.clicked.connect(self._reset_scope)
        badge_layout.addWidget(self.scope_badge_close)
        self.scope_badge.setVisible(False)
        top_layout.addWidget(self.scope_badge)

        self.view_select_label = QLabel("当前视图已选 0 / 0 项")
        self.view_select_label.setObjectName("Muted")
        top_layout.addWidget(self.view_select_label)
        center_layout.addWidget(top_bar)

        table_wrap = QWidget()
        table_layout = QVBoxLayout(table_wrap)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(0)
        self.table_model = UnitTableModel(self)
        self.table_model.checkChanged.connect(self._on_model_check)
        self.table_model.target_provider = self._preview_target
        self.table_model.group_provider = self._group_of
        self.table = QTableView()
        self.table.setModel(self.table_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(-1, Qt.AscendingOrder)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.check_delegate = CheckDelegate(lambda: self.service.settings.accent, self.table)
        self.table.setItemDelegateForColumn(0, self.check_delegate)
        self.status_delegate = StatusDelegate(
            lambda: tones_for(self.service.settings.theme), self.table)
        self.table.setItemDelegateForColumn(8, self.status_delegate)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.doubleClicked.connect(self._on_table_double_click)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)

        # 表头复选框：直接承担「全选 / 取消当前视图」（替代旧版文字按钮）
        self.table_header = CheckHeaderView(self._header_check_state, self.table)
        self.table.setHorizontalHeader(self.table_header)
        self.table_header.setStretchLastSection(False)
        self.table_header.setSectionsMovable(False)
        self.table_header.setSortIndicatorShown(False)
        self.table_header.sortIndicatorChanged.connect(
            lambda *_: self.table_header.setSortIndicatorShown(True))
        self.table_header.toggled.connect(self._bulk_check)
        header = self.table_header
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        header.setSectionResizeMode(6, QHeaderView.Fixed)
        header.setSectionResizeMode(7, QHeaderView.Fixed)
        header.setSectionResizeMode(8, QHeaderView.Fixed)
        header.setSectionResizeMode(9, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(5, 58)
        self.table.setColumnWidth(6, 58)
        self.table.setColumnWidth(7, 84)
        self.table.setColumnWidth(8, 96)
        self.table.setColumnWidth(9, 120)
        self.table.hideColumn(9)  # 目标预览移入右侧详情与悬停提示，保证无横向滚动条
        for sig in (self.table_model.modelReset, self.table_model.layoutChanged):
            sig.connect(self._update_header_check)
        self.table_model.dataChanged.connect(lambda *_: self._update_header_check())
        table_layout.addWidget(self.table)
        center_layout.addWidget(table_wrap, 1)
        self.splitter.addWidget(center)

        # ---------- 2.3 右侧：详情与元数据检视面板 ----------
        right = QFrame()
        right.setObjectName("Card")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 6, 6, 6)
        self.detail = DetailPanel()
        self.detail.fieldEdited.connect(self._on_field_edited)
        self.detail.confirmRequested.connect(self._confirm_boundary)
        self.detail.unconfirmRequested.connect(self._unconfirm_boundary)
        self.detail.boundaryChangeRequested.connect(self._change_boundary)
        self.detail.albumModeRequested.connect(self._set_album_mode)
        self.detail.attachmentToggled.connect(self._on_attachment_toggled)
        self.detail.attachmentSetAllRequested.connect(self._on_attachments_set_all)
        self.detail.clearAttachmentsRequested.connect(self._clear_attachments)
        self.detail.linkAttachmentRequested.connect(self._link_attachment)
        self.detail.linkLyricsRequested.connect(self._link_lyrics)
        self.detail.lyricsPreviewRequested.connect(self._preview_lyrics)
        self.detail.openPath.connect(self._open_path)
        right_layout.addWidget(self.detail)
        right.setMinimumWidth(320)
        self.right_panel = right
        self.splitter.addWidget(right)

        sizes = self.service.settings.splitter_sizes or [264, 760, 386]
        self.splitter.setSizes(sizes)
        self.splitter.splitterMoved.connect(self._on_splitter_moved)
        return self.splitter

    # ------------------------------ 底部执行栏 ------------------------------
    def _build_footer(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("FooterBar")
        bar.setFixedHeight(58)
        f = QHBoxLayout(bar)
        f.setContentsMargins(14, 8, 14, 8)
        f.setSpacing(10)

        # 左侧：输出配置
        self.target_label = QLabel("输出至")
        self.target_label.setObjectName("Muted")
        f.addWidget(self.target_label)
        arrow = QLabel()
        arrow.setPixmap(make_icon("arrow", "#94A3B8", size=13).pixmap(13, 13))
        f.addWidget(arrow)

        self.btn_target = QPushButton()
        self.btn_target.setObjectName("PathCapsule")
        self.btn_target.setToolTip("点击更换整理后的音乐库位置")
        self.btn_target.setMinimumWidth(280)
        self.btn_target.setMaximumWidth(430)
        self.btn_target.clicked.connect(self._browse_target)
        capsule = QHBoxLayout(self.btn_target)
        capsule.setContentsMargins(10, 3, 10, 3)
        capsule.setSpacing(6)
        folder_icon = QLabel()
        folder_icon.setPixmap(make_icon("folder", "#2563EB", size=15).pixmap(15, 15))
        capsule.addWidget(folder_icon)
        self.target_text = QLabel(self.service.settings.target_dir or "未选择目标目录")
        self.target_text.setObjectName("PathText")
        capsule.addWidget(self.target_text, 1)
        self.target_hint = QLabel("更改...")
        self.target_hint.setStyleSheet("color: #2563EB; font-size: 8pt; font-weight: 600;")
        capsule.addWidget(self.target_hint)
        f.addWidget(self.btn_target)

        # 整理方式分段控制器（移动 / 复制）
        mode_seg = QFrame()
        mode_seg.setObjectName("Segmented")
        mode_layout = QHBoxLayout(mode_seg)
        mode_layout.setContentsMargins(3, 2, 3, 2)
        mode_layout.setSpacing(2)
        self.btn_mode_move = QPushButton("移动")
        self.btn_mode_copy = QPushButton("复制")
        for btn, mode in ((self.btn_mode_move, "move"), (self.btn_mode_copy, "copy")):
            btn.setCheckable(True)
            btn.setProperty("kind", "pill")
            btn.blockSignals(True)
            btn.setChecked(self.service.settings.mode == mode)
            btn.blockSignals(False)
            btn.toggled.connect(self._on_mode_toggled)
            _sync_pill(btn)
            mode_layout.addWidget(btn)
        self.btn_mode_move.setToolTip("整理后清理来源空目录")
        self.btn_mode_copy.setToolTip("保留来源文件不动")
        f.addWidget(mode_seg)

        # 中部：提示 / 进度 / 取消 / 继续扫描
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("Muted")
        f.addWidget(self.hint_label, 1)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(160)
        self.progress.setVisible(False)
        f.addWidget(self.progress)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setProperty("kind", "ghost")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._cancel_task)
        f.addWidget(self.btn_cancel)

        self.btn_resume = QPushButton("确认目标并扫描")
        self.btn_resume.setProperty("kind", "primary")
        self.btn_resume.setToolTip("有已登记但未扫描的来源：确认目标后开始扫描")
        self.btn_resume.clicked.connect(self._resume_pending)
        self.btn_resume.setVisible(False)
        f.addWidget(self.btn_resume)

        # 右侧：全局汇总 + 唯一主执行按钮
        stats = QWidget()
        stats_layout = QVBoxLayout(stats)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(1)
        self.checked_label = QLabel("全局已勾选 0 项")
        self.checked_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.pending_label = QLabel("")
        self.pending_label.setObjectName("Muted")
        self.pending_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        pending_font = self.pending_label.font()
        pending_font.setPointSizeF(8)
        self.pending_label.setFont(pending_font)
        stats_layout.addWidget(self.checked_label)
        stats_layout.addWidget(self.pending_label)
        f.addWidget(stats)

        self.btn_review = QPushButton("检查并整理 (0)")
        self.btn_review.setProperty("kind", "primary")
        self.btn_review.setIcon(make_icon("play", "#FFFFFF"))
        self.btn_review.setFixedHeight(36)
        self.btn_review.setMinimumWidth(156)
        self.btn_review.setToolTip("统一查看勾选项目、提醒、排除项与最终预览（Ctrl+P）")
        self.btn_review.clicked.connect(self._open_review_center)
        f.addWidget(self.btn_review)
        return bar

    def _build_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+O"), self, self._add_source)
        QShortcut(QKeySequence("F5"), self, lambda: self._scan(initial=False))
        QShortcut(QKeySequence("Ctrl+F"), self, self.search_edit.setFocus)
        QShortcut(QKeySequence("Ctrl+K"), self, self.search_edit.setFocus)
        QShortcut(QKeySequence("Ctrl+P"), self, self._open_review_center)
        QShortcut(QKeySequence("Ctrl+H"), self, self._open_history)
        QShortcut(QKeySequence("Ctrl+,"), self, self._open_settings)

    # ------------------------------------------------------------------
    # 主题与背景
    # ------------------------------------------------------------------
    def _apply_theme(self, settings=None) -> None:
        settings = settings or self.service.settings
        panel_alpha = settings.background.panel_opacity if settings.background.image else 1.0
        apply_app_theme(self.app(), settings.theme, settings.accent, panel_alpha)
        self.setStyleSheet(build_stylesheet(settings.theme, settings.accent, panel_alpha))
        theme = settings.theme
        self._tone_colors = tones_for(theme)
        self.detail.set_theme(theme)
        self.brand_icon.setPixmap(brand_pixmap(28, settings.accent))
        self.btn_theme.setIcon(make_icon(
            {"light": "sun", "paper": "paper", "dark": "moon"}.get(theme, "sun"), "#8A8F98"))
        self.btn_theme.setToolTip(
            f"切换主题（当前：{palette_for(theme).name}；浅色 → 纸质 → 深色）")
        bg, fg, bd = self._tone_colors["blue"]
        self.scope_badge.setStyleSheet(
            f"QFrame#ScopeBadge {{ background: {bg}; border: 1px solid {bd};"
            f" border-radius: 8px; }}"
            f"QFrame#ScopeBadge QLabel {{ background: transparent; border: none;"
            f" color: {fg}; }}"
            f"QPushButton#CloseBadge {{ background: transparent; border: none;"
            f" color: {fg}; font-size: 10pt; padding: 0px 3px; }}"
            f"QPushButton#CloseBadge:hover {{ color: {bg}; }}")
        self._refresh_detail()
        self.update()

    def app(self):
        from PySide6.QtWidgets import QApplication
        return QApplication.instance()

    def _load_background(self) -> None:
        bg = self.service.settings.background
        if not bg.image:
            self._bg_pixmap = None
            return
        path = self.service.paths.background_dir / bg.image
        pix = QPixmap(str(path)) if path.is_file() else None
        self._bg_pixmap = pix if pix is not None and not pix.isNull() else None
        self.update()

    def paintEvent(self, event) -> None:
        if self._bg_pixmap is None:
            super().paintEvent(event)
            return
        from PySide6.QtGui import QPainter
        painter = QPainter(self)
        bg = self.service.settings.background
        palette = palette_for(self.service.settings.theme)
        paint_background(painter, self.rect(), self._bg_pixmap, bg.display, bg.align,
                         bg.intensity, palette.window, bg.mask)
        painter.end()

    def _cycle_theme(self) -> None:
        order = list(THEME_ORDER)
        try:
            index = order.index(self.service.settings.theme)
        except ValueError:
            index = 0
        theme = order[(index + 1) % len(order)]
        self.service.settings.theme = theme
        accent = self.service.settings.accent
        if theme == "paper":
            self.service.settings.accent = "#52796F"
        elif theme == "light":
            self.service.settings.accent = "#2563EB"
        else:
            self.service.settings.accent = "#3B82F6"
        self.service.save_settings()
        self._apply_theme()
        self._load_background()
        self.status_hint(f"已切换到{palette_for(theme).name}主题")

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------
    def _refresh_all(self) -> None:
        self.table_model.set_units(self.service.state.units)
        self.tree_model.rebuild(self.service.state.sources, self.service.state.units,
                                self.service.state.loose)
        self._expand_tree()
        self._update_status()
        self._refresh_detail()

    def _expand_tree(self) -> None:
        self.tree.expandToDepth(0)

    def _group_of(self, unit: Unit) -> str:
        return group_of(unit)

    def _reload_preserving(self) -> None:
        """刷新表格并尽量保留选择与滚动位置（按稳定 unit id，不依赖行号）。"""
        bar = self.table.verticalScrollBar().value()
        selected_ids: set[str] = set()
        sel_model = self.table.selectionModel()
        if sel_model is not None:
            for index in sel_model.selectedRows():
                selected_ids.add(str(index.data(Qt.UserRole) or ""))
        self.table_model.reload()
        sel_model = self.table.selectionModel()
        if sel_model is not None and selected_ids:
            from PySide6.QtCore import QItemSelectionModel
            for row in range(self.table_model.rowCount()):
                uid = str(self.table_model.index(row, 0).data(Qt.UserRole) or "")
                if uid in selected_ids:
                    sel_model.select(self.table_model.index(row, 0),
                                     QItemSelectionModel.SelectionFlag.Select |
                                     QItemSelectionModel.SelectionFlag.Rows)
        self.table.verticalScrollBar().setValue(bar)

    def _update_status(self) -> None:
        stats = self.service.stats()
        summary = stats["groups"]
        checked_total = summary["ready_checked"] + summary["needs_checked"]
        units_total = stats["total"]

        # 顶部筛选胶囊计数
        self.btn_group_all.setText(f"全部 {units_total}")
        self.btn_group_ready.setText(f"可整理 {summary['ready']}")
        self.btn_group_needs.setText(f"需处理 {summary['needs']}")
        self.btn_group_side.setText(f"附属 {summary['side']}")

        # 视图勾选汇总
        visible = self.table_model.visible_units()
        checked_visible = sum(1 for u in visible if u.checked)
        self.view_select_label.setText(f"当前视图已选 {checked_visible} / {len(visible)} 项")

        # 目录筛选胶囊 + 左栏范围提示
        scope = self.table_model.scope_path
        self.scope_badge.setVisible(bool(scope))
        if scope:
            self.scope_badge_text.setText(f"当前目录筛选：{Path(scope).name or scope}")
            self.scope_badge.setToolTip(scope)
        self.btn_all_sources.setProperty("active", not scope)
        _repolish(self.btn_all_sources)
        self.all_count_label.setText(str(units_total))
        self.scope_label.setText(scope or "全部来源目录")
        self.scope_label.setToolTip(scope or "未指定目录范围")
        self.btn_show_all.setVisible(bool(scope))

        # 底部全局汇总
        accent = self.service.settings.accent
        self.checked_label.setTextFormat(Qt.RichText)
        self.checked_label.setText(
            f'全局已勾选 <span style="color:{accent}; font-weight:700;">{checked_total}</span> 项')
        migrate = sum(u.size for u in self.service.state.units
                      if u.checked and group_of(u) in ("ready", "needs"))
        self.pending_label.setText(
            f"可整理 {summary['ready']} · 需处理 {summary['needs']} · 附属 {summary['side']}"
            f" · 预计迁移 {human_size(migrate) or '0 MB'}")

        # 歌词待关联徽章
        pending_lyrics = stats.get("lyrics_pending", 0)
        self.lyrics_badge.setVisible(bool(pending_lyrics))
        self.lyrics_badge.setText(str(pending_lyrics))
        self.btn_lyrics.setToolTip("歌词工具" +
                                   (f"（{pending_lyrics} 首待关联）" if pending_lyrics else ""))

        # 查重徽章（扫描中文案由 _check_duplicates 维护）
        if not self._dup_running:
            if not stats["duplicates_checked"]:
                _set_tone(self.dup_badge, "gray")
                self.dup_badge.setText("未检")
            else:
                dup_count = stats["duplicates"]
                _set_tone(self.dup_badge, "green" if dup_count == 0 else "red")
                self.dup_badge.setText(f"{dup_count} 重复")

        # 来源 → 目标：主界面持续显示，来源数量与完整路径放 tooltip
        sources = self.service.state.sources
        pending_scan = [s for s in sources if not s.scanned]
        self.target_label.setText(f"来源 {len(sources)} → 目标" if sources else "输出至")
        if sources:
            lines = [f"来源（{len(sources)} 个）→ 目标："]
            lines.extend(f"· {s.path}" + ("（待扫描）" if not s.scanned else "")
                         for s in sources[:20])
            if len(sources) > 20:
                lines.append(f"…共 {len(sources)} 个")
            lines.append(f"目标：{self.service.settings.target_dir or '（未确认）'}")
            self.target_label.setToolTip("\n".join(lines))
        else:
            self.target_label.setToolTip("")
        target = self.service.settings.target_dir
        fm = self.target_text.fontMetrics()
        self.target_text.setText(fm.elidedText(target or "未选择目标目录",
                                               Qt.ElideMiddle, 240))
        self.btn_target.setToolTip(target or "点击选择整理后的音乐库位置")

        self.btn_resume.setVisible(bool(pending_scan))
        if pending_scan:
            self.btn_resume.setText(f"确认目标并扫描（{len(pending_scan)}）")

        # 主执行按钮与提示
        self.btn_review.setText(f"检查并整理 ({checked_total})")
        can_review = bool(checked_total) and bool(target)
        self.btn_review.setEnabled(can_review and not self._busy)
        if not target:
            self.hint_label.setText("请先选择目标目录")
        elif not checked_total:
            self.hint_label.setText("勾选要整理的条目后即可检查并整理（可整理项已默认勾选）")
        elif not self._busy:
            self.hint_label.setText("")
        self._update_header_check()

    def _header_check_state(self) -> Qt.CheckState:
        """表头复选框状态：按当前视图可见条目的勾选情况计算三态。"""
        visible = self.table_model.visible_units()
        if not visible:
            return Qt.Unchecked
        checked = sum(1 for u in visible if u.checked)
        if checked == 0:
            return Qt.Unchecked
        return Qt.Checked if checked == len(visible) else Qt.PartiallyChecked

    def _update_header_check(self) -> None:
        self.table_header.viewport().update()

    def status_hint(self, text: str, timeout: int = 6000) -> None:
        self.hint_label.setText(text)
        if timeout:
            QTimer.singleShot(timeout, lambda: self.hint_label.setText(""))

    def _refresh_detail(self) -> None:
        unit = self._current_unit()
        preview = self._preview_target(unit) if unit is not None else ""
        self.detail.set_unit(unit, preview)

    def _current_unit(self) -> Unit | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        return self.table_model.unit_at(rows[0].row())

    def _preview_target(self, unit: Unit) -> str:
        root = self.service.settings.target_dir
        if not root:
            return ""
        settings = self.service.settings
        try:
            if unit.is_album:
                return str(naming.album_target_dir(root, unit, settings.template))
            if unit.is_track:
                if unit.dup_role == "duplicate":
                    return str(Path(root) / "重复文件" / Path(unit.path).name)
                return str(naming.track_target_path(root, unit, settings.template,
                                                   settings.track_digits))
            if unit.is_archive:
                return str(Path(root) / "原始压缩包" / Path(unit.path).name)
        except naming.NamingError:
            return "（缺少信息，暂无法生成）"
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def _restore_geometry(self) -> None:
        geometry = self.service.settings.window_geometry
        if geometry:
            try:
                from PySide6.QtCore import QByteArray
                self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
            except (ValueError, TypeError):
                pass

    def _save_geometry(self) -> None:
        try:
            self.service.settings.window_geometry = bytes(
                self.saveGeometry().toBase64()).decode("ascii")
            self.service.settings.splitter_sizes = self.splitter.sizes()
            self.service.save_settings()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _add_source(self) -> None:
        if self._busy:
            return
        path = QFileDialog.getExistingDirectory(self, "选择要整理的来源文件夹")
        if not path:
            return  # 取消来源选择：直接返回，不弹目标引导
        result = self.service.register_sources([path])
        if not result["added"]:
            self._show_notes("来源未添加", result.get("skipped") or ["该目录已在列表中"])
            return
        self._refresh_all()
        self._guide_target(added=result["added"],
                           skipped=result.get("skipped") or [])

    def _guide_target(self, added: int, skipped: list[str] | None = None) -> None:
        """添加来源后的目标引导：登记已完成，确认目标后才开始本批扫描。

        - 尚无目标：说明“来源是待整理音乐的位置，目标是整理后的存放位置”并打开选择器；
        - 已有目标：展示完整路径与来源数量，使用此目标 / 更改目标 / 暂不扫描，
          不经确认不沿用旧目标；
        - 验证失败给出具体原因并允许重新选择；引导本身不做任何移动/复制/解压/归档。
        """
        if self._busy:
            return
        skipped = skipped or []
        while True:
            target = (self.service.settings.target_dir or "").strip()
            count = len(self.service.state.sources)
            if not target:
                box = QMessageBox(self)
                box.setWindowTitle("选择目标目录")
                box.setText("来源是待整理音乐的位置，目标是整理后的存放位置。\n\n"
                            f"本批已登记 {added} 个来源（共 {count} 个待整理来源）。\n"
                            "选择目标目录并确认后开始扫描。")
                box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
                box.setDefaultButton(QMessageBox.Ok)
                if box.exec() != QMessageBox.Ok:
                    self._defer_scan()
                    return
                chosen = QFileDialog.getExistingDirectory(self, "选择目标音乐库目录")
                if not chosen:
                    self._defer_scan()
                    return
            else:
                choice = QMessageBox.question(
                    self, "确认目标目录",
                    f"本批已登记 {added} 个来源（共 {count} 个待整理来源）。\n\n"
                    f"目标目录：\n{target}\n\n"
                    "确认后开始扫描（本批只扫描一次）。",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                    QMessageBox.Yes)
                if choice == QMessageBox.Cancel:
                    self._defer_scan()
                    return
                if choice == QMessageBox.Yes:
                    chosen = target
                else:
                    chosen = QFileDialog.getExistingDirectory(
                        self, "更改目标音乐库目录", target)
                    if not chosen:
                        continue  # 取消更改：回到当前目标确认步骤，不静默沿用
            result = self.service.set_target(chosen)
            if not result.get("ok"):
                QMessageBox.warning(self, "目标不可用",
                                    result.get("reason", "") + "\n\n请重新选择目标目录。")
                self.service.settings.target_dir = ""
                continue
            if not result.get("exists"):
                # 不存在的新目标：用户已在引导中明确确认，这里创建
                try:
                    Path(chosen).mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    QMessageBox.warning(self, "无法创建目标目录", f"{exc.strerror or exc}")
                    continue
            self._refresh_all()
            self._scan(initial=False)
            return

    def _defer_scan(self) -> None:
        """暂不扫描：保留已登记来源，主界面提供继续入口。"""
        self._refresh_all()
        self.status_hint("来源已登记、暂未扫描；点击状态栏「确认目标并扫描」继续")

    def _resume_pending(self) -> None:
        """继续未完成的引导：已登记未扫描的来源确认目标后扫描。"""
        pending = [s for s in self.service.state.sources if not s.scanned]
        if not pending:
            self.status_hint("没有待扫描的来源")
            return
        self._guide_target(added=len(pending))

    def _scan(self, initial: bool, paths: list[str] | None = None) -> None:
        if self._busy:
            return
        self._set_busy(True, "正在扫描…")
        if initial and paths:
            result = {"value": None}

            def task(progress, cancel):
                return self.service.add_sources(paths, progress)

        else:
            def task(progress, cancel):
                return self.service.rescan(progress, cancel)

        self.runner.run(task, on_done=self._on_scan_done, on_error=self._on_task_error,
                        on_progress=self._on_progress,
                        on_cancelled=self._on_task_cancelled)

    def _on_scan_done(self, result) -> None:
        self._set_busy(False)
        self._refresh_all()
        if isinstance(result, dict):
            parts = []
            if "albums" in result:
                parts.append(f"专辑 {result['albums']}")
            if "tracks" in result:
                parts.append(f"散曲 {result['tracks']}")
            if "archives" in result:
                parts.append(f"压缩包 {result['archives']}")
            if "added" in result:
                parts.append(f"新增来源 {result['added']}")
            self.status_hint("扫描完成：" + "｜".join(parts))
            errors = result.get("errors") or []
            if errors:
                self._show_notes("扫描提示", errors)

    def _remove_source(self, path: str | None = None) -> None:
        if not path:
            indexes = self.tree.selectionModel().selectedIndexes() if \
                self.tree.selectionModel() else []
            if not indexes:
                QMessageBox.information(self, "提示", "请先在来源树里选择要移除的来源。")
                return
            path = indexes[0].data(SourceTreeModel.ROLE_PATH) or ""
        source_path = path
        source = next((s for s in self.service.state.sources
                       if os.path.normcase(s.path) == os.path.normcase(source_path)), None)
        if source is None:
            QMessageBox.information(self, "提示", "请选择来源根节点。")
            return
        confirm = QMessageBox.question(self, "移除来源",
                                       f"从列表中移除来源（不会删除磁盘文件）：\n{source.path}",
                                       QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        self.service.remove_source(source.id)
        self._refresh_all()
        self.status_hint("已移除来源")

    def _clear_list(self) -> None:
        stats = self.service.stats()
        total = stats["total"] + len(self.service.state.loose)
        if not self.service.state.sources and not total:
            QMessageBox.information(self, "提示", "列表已经是空的。")
            return
        confirm = QMessageBox.question(
            self, "清空列表",
            f"将移除全部来源与整理条目（共 {total} 项）：\n\n"
            "· 不会删除磁盘上的音乐文件\n"
            "· 历史与撤销记录保留\n"
            "· 手动编辑过的信息会一并清掉\n\n确定继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        removed = self.service.clear_all()
        self._refresh_all()
        self.status_hint(f"列表已清空（{removed} 项）；文件未删除，重新添加来源即可再次扫描")

    def _browse_target(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择目标音乐库目录",
                                               self.service.settings.target_dir or "")
        if not path:
            return
        result = self.service.set_target(path)
        if not result.get("ok"):
            QMessageBox.warning(self, "目标不可用", result.get("reason", ""))
            return
        if not result.get("exists"):
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(self, "无法创建目标目录", f"{exc.strerror or exc}")
                return
        self.target_text.setText(self.service.settings.target_dir)
        self._after_target_change(changed=result.get("changed", False))

    def _after_target_change(self, changed: bool) -> None:
        """目标变化后：预览/分组/计划已在服务层重算，这里刷新视图并失效旧计划。"""
        if changed:
            self._current_plan = None  # 旧计划失效，不得按旧路径执行
        self.table_model.refresh_all()
        self._refresh_detail()
        self._update_status()

    def _on_mode_toggled(self, checked: bool) -> None:
        """整理方式分段控制：始终恰好一项激活，切换即保存。"""
        btn = self.sender()
        if not isinstance(btn, QPushButton):
            return
        if not checked:
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)
            return
        for other in (self.btn_mode_move, self.btn_mode_copy):
            if other is not btn and other.isChecked():
                other.blockSignals(True)
                other.setChecked(False)
                other.blockSignals(False)
            _sync_pill(other)
        _sync_pill(btn)
        mode = "move" if btn is self.btn_mode_move else "copy"
        self.service.settings.mode = mode
        self.service.save_settings()
        self.status_hint("整理方式：" + ("移动（整理后清理原空目录）" if mode == "move"
                                    else "复制（保留源文件不动）"))

    def _on_search(self, text: str) -> None:
        self.table_model.search = text.strip()
        self._reload_preserving()
        self._update_status()

    def _on_group_toggled(self, checked: bool) -> None:
        """分组胶囊切换：全部 / 可整理 / 需处理 / 附属 互斥，始终恰好一项激活。"""
        btn = self.sender()
        buttons = (self.btn_group_all, self.btn_group_ready, self.btn_group_needs,
                   self.btn_group_side)
        if isinstance(btn, QPushButton):
            if checked:
                for other in buttons:
                    if other is not btn and other.isChecked():
                        other.blockSignals(True)
                        other.setChecked(False)
                        other.blockSignals(False)
                    _sync_pill(other)
                _sync_pill(btn)
            else:
                # 取消当前激活项时回到「全部」，保证始终有一个激活胶囊
                btn.blockSignals(True)
                btn.setChecked(True)
                btn.blockSignals(False)
                btn = self.btn_group_all
                _sync_pill(btn)
            self.table_model.group_filter = {
                id(self.btn_group_all): "",
                id(self.btn_group_ready): "ready",
                id(self.btn_group_needs): "needs",
                id(self.btn_group_side): "side",
            }[id(btn)]
        self._reload_preserving()
        self._update_status()

    def _on_tree_clicked(self, index) -> None:
        path = index.data(SourceTreeModel.ROLE_PATH) or ""
        if not path:
            return
        self.table_model.scope_path = path
        self._reload_preserving()
        self._update_status()

    def _reset_scope(self) -> None:
        self.table_model.scope_path = ""
        self._reload_preserving()
        self._update_status()

    def _tree_menu(self, pos) -> None:
        indexes = self.tree.selectionModel().selectedIndexes() if \
            self.tree.selectionModel() else []
        menu = QMenu(self)
        if indexes:
            path = indexes[0].data(SourceTreeModel.ROLE_PATH) or ""
            if path:
                act_scope = QAction("筛选此目录范围", self)
                act_scope.triggered.connect(lambda: self._on_tree_clicked(indexes[0]))
                act_remove = QAction("移除此来源…", self)
                act_remove.triggered.connect(lambda: self._remove_source(path))
                menu.addAction(act_scope)
                menu.addAction(act_remove)
                menu.addSeparator()
        act_rescan = QAction("重新扫描", self)
        act_rescan.triggered.connect(lambda: self._scan(initial=False))
        menu.addAction(act_rescan)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _schedule_save(self) -> None:
        """高频交互（勾选/编辑）合并写盘：400ms 内的连续改动只落盘一次。
        关闭窗口与执行/扫描收尾仍直接 save_state 兜底。"""
        self._save_timer.start()

    def _on_tree_check(self, unit_ids: list[str], checked: bool) -> None:
        units = [u for u in (self.service.state.unit_by_id(i) for i in unit_ids) if u]
        self.service.set_checked(units, checked, explicit=True)
        self._schedule_save()
        self._reload_preserving()
        self._update_status()
        self._refresh_detail()

    def _on_model_check(self, unit_id: str, checked: bool) -> None:
        unit = self.service.state.unit_by_id(unit_id)
        if unit is not None:
            # 整夹取消时子曲目随服务层级联取消；用户操作记为显式选择
            self.service.set_checked([unit], checked, explicit=True)
        self._schedule_save()
        # 定向刷新：_reload_preserving 按稳定 unit id 恢复选择与滚动位置——
        # 勾选后当前行与右侧详情不丢失；树重建同步勾选状态
        self._reload_preserving()
        self.tree_model.rebuild(self.service.state.sources, self.service.state.units,
                                self.service.state.loose)
        self._update_status()
        self._refresh_detail()

    def _bulk_check(self, checked: bool) -> None:
        """组内全选/取消：只作用于当前视图可见条目（分组+搜索+目录范围）。"""
        units = self.table_model.visible_units()
        self.service.set_checked(units, checked, explicit=True)
        self._schedule_save()
        self._reload_preserving()
        self.tree_model.rebuild(self.service.state.sources, self.service.state.units,
                                self.service.state.loose)
        self._update_status()
        self._refresh_detail()
        scope = {"ready": "可整理", "needs": "需处理", "side": "附属"}.get(
            self.table_model.group_filter, "全部")
        self.status_hint(
            f"已{'勾选' if checked else '取消勾选'}当前视图 {len(units)} 项（范围：{scope}"
            + ("＋搜索/目录筛选" if self.table_model.search or self.table_model.scope_path else "")
            + "）")

    def _on_selection_changed(self, *_args) -> None:
        self._refresh_detail()

    def _on_table_double_click(self, index) -> None:
        unit = self.table_model.unit_at(index.row())
        if unit is not None:
            self._open_path(unit.boundary_path if unit.is_album else unit.path)

    def _table_menu(self, pos) -> None:
        unit = self._current_unit()
        menu = QMenu(self)
        act_check = QAction("勾选所选", self)
        act_check.triggered.connect(lambda: self._check_selected(True))
        act_uncheck = QAction("取消勾选所选", self)
        act_uncheck.triggered.connect(lambda: self._check_selected(False))
        menu.addAction(act_check)
        menu.addAction(act_uncheck)
        menu.addSeparator()
        if unit is not None:
            if unit.is_album:
                act_boundary = QAction("改选上级 / 子级边界…", self)
                act_boundary.triggered.connect(lambda: self._change_boundary(unit.id))
                act_tracks = QAction("改为按散曲整理", self)
                act_tracks.triggered.connect(lambda: self._set_album_mode(unit.id, "tracks"))
                for act in (act_boundary, act_tracks):
                    menu.addAction(act)
                menu.addSeparator()
            act_link = QAction("关联附件…", self)
            act_link.triggered.connect(lambda: self._link_attachment(unit.id))
            act_open_tgt = QAction("打开目标位置", self)
            act_open_tgt.triggered.connect(
                lambda: self._open_path(self._preview_target(unit) or unit.path))
            act_open_src = QAction("打开来源位置", self)
            act_open_src.triggered.connect(
                lambda: self._open_path(unit.boundary_path if unit.is_album else unit.path))
            for act in (act_link, act_open_src, act_open_tgt):
                menu.addAction(act)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _check_selected(self, checked: bool) -> None:
        rows = self.table.selectionModel().selectedRows()
        units = [u for u in (self.table_model.unit_at(i.row()) for i in rows) if u]
        self.service.set_checked(units, checked, explicit=True)
        self._schedule_save()
        self._reload_preserving()
        self.tree_model.rebuild(self.service.state.sources, self.service.state.units,
                                self.service.state.loose)
        self._update_status()
        self._refresh_detail()

    # ------------------------------------------------------------------
    # 详情编辑
    # ------------------------------------------------------------------
    def _on_field_edited(self, unit_id: str, field: str, value) -> None:
        unit = self.service.state.unit_by_id(unit_id)
        if unit is None:
            return
        self.service.update_unit_fields(unit, {field: value})
        self._schedule_save()
        # 保留选择：连续编辑多个字段时不要求每改一项重新选中
        self._reload_preserving()
        self._update_status()
        self._refresh_detail()

    def _confirm_boundary(self, unit_id: str) -> None:
        unit = self.service.state.unit_by_id(unit_id)
        if unit is None:
            return
        self.service.confirm_boundary(unit)
        self._schedule_save()
        self._reload_preserving()
        self.tree_model.rebuild(self.service.state.sources, self.service.state.units,
                                self.service.state.loose)
        self._update_status()
        self._refresh_detail()
        self.status_hint(f"已确认整夹范围：{unit.name}")

    def _unconfirm_boundary(self, unit_id: str) -> None:
        unit = self.service.state.unit_by_id(unit_id)
        if unit is None:
            return
        self.service.unconfirm_boundary(unit)
        self._schedule_save()
        self._reload_preserving()
        self._update_status()
        self._refresh_detail()

    def _change_boundary(self, unit_id: str) -> None:
        unit = self.service.state.unit_by_id(unit_id)
        if unit is None:
            return
        options: list[Path] = []
        boundary = Path(unit.boundary_path or unit.path)
        if unit.candidate_parent:
            options.append(Path(unit.candidate_parent))
        options.append(boundary.parent)
        for child in sorted([p for p in boundary.iterdir() if p.is_dir()]) if boundary.is_dir() else []:
            options.append(child)
        titles = [f"{p}　（{'上级' if p == boundary.parent else '子级/同级'}）" for p in options]
        from PySide6.QtWidgets import QInputDialog
        choice, ok = QInputDialog.getItem(self, "改选专辑边界", "选择整夹整理的范围：",
                                         titles, 0, False)
        if not ok:
            return
        selected = options[titles.index(choice)]
        self.service.confirm_boundary(unit, str(selected))
        self._schedule_save()
        self._reload_preserving()
        self.tree_model.rebuild(self.service.state.sources, self.service.state.units,
                                self.service.state.loose)
        self._update_status()
        self._refresh_detail()
        self.status_hint(f"边界已改为：{selected}")

    def _set_album_mode(self, unit_id: str, mode: str) -> None:
        unit = self.service.state.unit_by_id(unit_id)
        if unit is None:
            return
        created = self.service.ensure_track_units(unit)
        self.service.set_album_mode(unit, mode)
        self._schedule_save()
        self._reload_preserving()
        self.tree_model.rebuild(self.service.state.sources, self.service.state.units,
                                self.service.state.loose)
        self._update_status()
        self._refresh_detail()
        self.status_hint(f"已改为按散曲整理，生成 {created} 个散曲条目")

    def _on_attachment_toggled(self, unit_id: str, path: str, selected: bool) -> None:
        unit = self.service.state.unit_by_id(unit_id)
        if unit is None:
            return
        for att in unit.attachments:
            if os.path.normcase(att["path"]) == os.path.normcase(path):
                att["selected"] = selected
        self._schedule_save()
        self._refresh_detail()

    def _on_attachments_set_all(self, unit_id: str, checked: bool) -> None:
        """标题栏「全不选/全选」：一次落账、一次刷新（逐项发信号会 N 次保存）。"""
        unit = self.service.state.unit_by_id(unit_id)
        if unit is None:
            return
        for att in unit.attachments:
            att["selected"] = bool(checked)
        self._schedule_save()
        self._refresh_detail()
        self.status_hint("已全选随附文件" if checked else "已取消勾选全部随附文件")

    def _clear_attachments(self, unit_id: str) -> None:
        unit = self.service.state.unit_by_id(unit_id)
        if unit is None:
            return
        removed = self.service.clear_attachments(unit)
        if not removed:
            return
        self._reload_preserving()
        self.tree_model.rebuild(self.service.state.sources, self.service.state.units,
                                self.service.state.loose)
        self._update_status()
        self._refresh_detail()
        self.status_hint(f"已解除 {removed} 项附件关联（重新扫描可恢复自动识别）")

    def _link_attachment(self, unit_id: str) -> None:
        unit = self.service.state.unit_by_id(unit_id)
        if unit is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要关联的附件（可多选）", str(Path(unit.path).parent),
            "常用附件 (*.jpg *.jpeg *.png *.bmp *.webp *.gif *.tif *.tiff "
            "*.log *.accurip *.txt *.pdf *.cue);;所有文件 (*)")
        if not paths:
            return
        for path in paths:
            self.service.link_attachment(unit, path)
        self._reload_preserving()
        self.tree_model.rebuild(self.service.state.sources, self.service.state.units,
                                self.service.state.loose)
        self._update_status()
        self._refresh_detail()
        name = Path(paths[0]).name
        more = f" 等 {len(paths)} 项" if len(paths) > 1 else ""
        self.status_hint(f"已关联附件：{name}{more}")

    # ------------------------------------------------------------------
    # 外置歌词
    # ------------------------------------------------------------------
    def _preview_lyrics(self, unit_id: str, path: str) -> None:
        from .lyrics_dialog import LyricsDialog
        dialog = LyricsDialog(self.service, self, path=path, pending_only=False)
        dialog.changed.connect(self._on_lyrics_changed)
        dialog.exec()

    def _link_lyrics(self, unit_id: str) -> None:
        """手动为当前歌曲选择歌词文件：先预览、确认编码与依据，再关联。"""
        from .lyrics_dialog import LyricsDialog
        unit = self.service.state.unit_by_id(unit_id)
        if unit is None:
            return
        start_dir = str(Path(unit.path).parent) if unit.path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择歌词文件", start_dir, "歌词文件 (*.lrc *.txt);;所有文件 (*)")
        if not path:
            return
        dialog = LyricsDialog(self.service, self, path=path, pending_only=False,
                              prefer_unit_id=unit_id)
        dialog.changed.connect(self._on_lyrics_changed)
        dialog.exec()

    def _open_lyrics(self) -> None:
        from .lyrics_dialog import LyricsDialog
        dialog = LyricsDialog(self.service, self, pending_only=True)
        dialog.changed.connect(self._on_lyrics_changed)
        dialog.exec()

    def _on_lyrics_changed(self) -> None:
        # 保留选择：歌词窗口关闭后不把列表/详情清空
        self._reload_preserving()
        self.tree_model.rebuild(self.service.state.sources, self.service.state.units,
                                self.service.state.loose)
        self._update_status()
        self._refresh_detail()
        self.status_hint("歌词关联已更新")

    # ------------------------------------------------------------------
    # 重复
    # ------------------------------------------------------------------
    def _check_duplicates(self) -> None:
        if self._busy:
            return
        if not self.service.state.units:
            QMessageBox.information(self, "提示", "请先添加来源并扫描。")
            return
        self._set_busy(True, "正在比较重复…")
        self._dup_running = True
        _set_tone(self.dup_badge, "blue")
        self.dup_badge.setText("扫描中…")

        def task(progress, cancel):
            return self.service.run_duplicate_check(progress, cancel)

        def done(result) -> None:
            self._dup_running = False
            self._set_busy(False)
            self._refresh_all()
            self.status_hint(f"重复检测完成：确定重复 {result['exact']} 组")
            if result.get("failures"):
                self._show_notes("部分文件无法读取", result["failures"])

        self.runner.run(task, on_done=done, on_error=self._on_task_error,
                        on_progress=self._on_progress,
                        on_cancelled=self._on_task_cancelled)

    def _duplicate_entry(self) -> None:
        """查重按钮 / 徽标统一入口：未检 → 检测；0 重复 → 提示；>0 → 隔离弹窗。"""
        if self._busy:
            return
        if not self.service.state.units:
            QMessageBox.information(self, "提示", "请先添加来源并扫描。")
            return
        stats = self.service.stats()
        if not stats["duplicates_checked"]:
            self._check_duplicates()
            return
        if stats["duplicates"] == 0:
            self._show_toast("当前音乐库无重复歌曲", theme=self.service.settings.theme)
            return
        self._open_isolation_dialog()

    def _open_isolation_dialog(self) -> None:
        """打开「查重隔离」确认弹窗；执行结果经弹窗回传后轻提示。"""
        from .duplicate_isolation_dialog import DuplicateIsolationDialog
        dialog = DuplicateIsolationDialog(self.service, self, runner=self.runner)
        dialog.exec()
        result = dialog.isolation_result
        if result:
            self._refresh_all()
            self._show_isolation_toast(result)

    def _show_isolation_toast(self, result: dict) -> None:
        moved = int(result.get("moved", 0))
        failures = list(result.get("failures") or [])
        dirs = list(result.get("isolation_dirs") or [])
        if moved == 0:
            detail = "\n".join(failures[:6]) or "没有文件被移动"
            QMessageBox.warning(self, "隔离未完成",
                                "以下重复文件未能移动（可能正被播放器占用，"
                                "关闭相关程序后可重试）：\n" + detail)
            return
        text = f"成功隔离 {moved} 首重复歌曲至「a重复歌曲」文件夹"
        if failures:
            text += f"；{len(failures)} 项因被占用或来源异常未能移动"
        if result.get("log_errors"):
            text += f"；{len(result['log_errors'])} 条隔离日志写入失败"
        actions = []
        if dirs:
            actions.append(("打开文件夹", lambda: self._open_isolation_dirs(dirs)))
        self._show_toast(text, actions, theme=self.service.settings.theme)

    def _open_isolation_dirs(self, dirs: list[str]) -> None:
        """在系统资源管理器中打开「a重复歌曲」隔离目录。"""
        errors: list[str] = []
        for directory in dirs:
            try:
                if Path(directory).is_dir():
                    os.startfile(directory)  # noqa: S606 - Windows 资源管理器打开
                else:
                    errors.append(f"{directory}：目录不存在")
            except OSError as exc:
                errors.append(f"{directory}：{exc.strerror or exc}")
        if errors:
            QMessageBox.warning(self, "无法打开文件夹", "\n".join(errors[:3]))

    def _show_toast(self, text: str, actions=None, theme: str = "light") -> None:
        old = getattr(self, "_toast", None)
        if old is not None:
            old.close()
            old.deleteLater()
        toast = Toast(self, text, actions=actions, theme=theme)
        self._toast = toast
        toast.adjustSize()
        toast.move(max((self.width() - toast.width()) // 2, 12), 64)
        toast.show()
        toast.raise_()

    # ------------------------------------------------------------------
    # 计划与执行
    # ------------------------------------------------------------------
    def _open_review_center(self) -> None:
        """检查并整理：统一整理中心（提醒 + 排除项 + 最终预览，可就地处理）。"""
        if self._busy:
            return
        if not self.service.settings.target_dir:
            QMessageBox.information(self, "提示", "请先选择目标目录。")
            return
        from .review_center import ReviewCenterDialog
        dialog = ReviewCenterDialog(self.service, self)
        dialog.locateRequested.connect(self._locate_unit)
        dialog.changed.connect(self._refresh_all)
        dialog.exec()
        self._refresh_all()
        if dialog.rescan_requested:
            self._scan(initial=False)
            return
        plan = dialog.plan
        if dialog.result() == QDialog.Accepted and plan is not None and plan.ready_items:
            self._current_plan = plan
            self._execute_plan(plan)

    def _execute_plan(self, plan: Plan) -> None:
        selected = [i for i in plan.items if i.selected]
        if not selected:
            return
        mode = "复制" if plan.options.mode == "copy" else "移动"
        confirm = QMessageBox.question(
            self, "开始整理",
            f"将以“{mode}”方式执行 {len(selected)} 项操作。\n"
            f"目标目录：{plan.options.target_root}\n\n操作会写入历史记录，可在“历史”中撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if confirm != QMessageBox.Yes:
            return
        self._set_busy(True, "正在整理…")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        def task(progress, cancel):
            return self.service.execute_plan(plan, progress=progress, cancel=cancel)

        def on_progress(message: str, current: int, total: int) -> None:
            if total > 0:
                self.progress.setValue(int(current / total * 100))
            self.hint_label.setText(message)

        self.runner.run(task, on_done=self._on_execute_done, on_error=self._on_execute_error,
                        on_progress=on_progress, on_cancelled=self._on_task_cancelled)

    def _on_execute_done(self, summary) -> None:
        self._set_busy(False)
        self._last_summary = summary
        self._drop_completed(summary)
        self._refresh_all()
        self._show_run_result(summary)

    def _drop_completed(self, summary) -> None:
        """移动成功的条目自动从列表移除（文件已离开来源目录，留在列表里
        只会变成“来源失联”噪音）。复制模式文件仍在来源，保留条目。"""
        if self._current_plan is None or self._current_plan.options.mode != "move":
            return
        done_ids = {r.item_id for r in summary.results if r.status == "success"}
        if not done_ids:
            return
        unit_ids = [i.unit_id for i in self._current_plan.items
                    if i.id in done_ids and i.unit_id]
        if not unit_ids:
            return
        removed = self.service.drop_units(unit_ids)
        if removed:
            self.status_hint(f"已从列表移除 {removed} 个完成条目")

    def _on_execute_error(self, message: str) -> None:
        self._set_busy(False)
        QMessageBox.critical(self, "执行出错", message)

    def _show_run_result(self, summary) -> None:
        dialog = RunResultDialog(summary, self)
        dialog.exec()
        if dialog.undo_requested:
            self._open_history()

    def _on_task_error(self, message: str) -> None:
        self._dup_running = False
        self._set_busy(False)
        QMessageBox.critical(self, "任务失败", message)

    def _on_task_cancelled(self) -> None:
        """后台任务在安全点被取消：必须恢复界面，不得停留在 busy 状态。

        扫描/查重在检查点抛 TaskCancelled，此时未合并任何结果、未写状态；
        未完成的项目保持原样，可以随时重新开始。"""
        self._dup_running = False
        self._set_busy(False)
        self._refresh_all()
        self.status_hint("任务已取消；未完成的部分未做任何改动，可随时重新开始")

    def _on_progress(self, message: str, current: int, total: int) -> None:
        self.hint_label.setText(message)
        if total > 0:
            self.progress.setVisible(True)
            self.progress.setRange(0, 100)
            self.progress.setValue(int(current / max(total, 1) * 100))

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.btn_add.setEnabled(not busy)
        self.btn_rescan.setEnabled(not busy)
        self.btn_review.setEnabled(not busy and bool(
            self.service.stats()["groups"].get("ready_checked")
            or self.service.stats()["groups"].get("needs_checked")))
        self.btn_dup.setEnabled(not busy)
        self.progress.setVisible(busy)
        self.btn_cancel.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
            self.hint_label.setText(message)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self.progress.setVisible(False)
            self.btn_cancel.setVisible(False)
            self.hint_label.setText("")

    def _cancel_task(self) -> None:
        self.runner.cancel_current()
        self.status_hint("正在安全停止…")

    # ------------------------------------------------------------------
    # 历史、检查并整理、设置
    # ------------------------------------------------------------------
    def _open_history(self) -> None:
        dialog = HistoryDialog(self.service, self)
        dialog.exec()
        self._refresh_all()

    def _locate_unit(self, unit_id: str) -> None:
        unit = self.service.state.unit_by_id(unit_id)
        if unit is None:
            return
        row = self.table_model.row_of(unit)
        if row < 0:
            self.table_model.scope_path = ""
            self.table_model.search = ""
            self.table_model.group_filter = ""
            for btn in (self.btn_group_all, self.btn_group_ready, self.btn_group_needs,
                        self.btn_group_side):
                btn.blockSignals(True)
                btn.setChecked(btn is self.btn_group_all)
                _sync_pill(btn)
                btn.blockSignals(False)
            self.search_edit.setText("")
            self.table_model.reload()
            row = self.table_model.row_of(unit)
        if row >= 0:
            from PySide6.QtCore import QItemSelectionModel
            index = self.table_model.index(row, 0)
            self.table.selectionModel().setCurrentIndex(
                index, QItemSelectionModel.SelectionFlag.ClearAndSelect |
                QItemSelectionModel.SelectionFlag.Rows)
            self.table.scrollTo(index, QAbstractItemView.PositionAtCenter)
            self._refresh_detail()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.service, self)
        dialog.previewChanged.connect(self._on_settings_preview)
        dialog.exec()
        self._apply_theme()
        self._load_background()
        self.table_model.refresh_all()
        self._update_status()

    def _on_settings_preview(self, settings) -> None:
        self._apply_theme(settings)
        bg = settings.background
        if bg.image:
            path = self.service.paths.background_dir / bg.image
            pix = QPixmap(str(path))
            self._bg_pixmap = pix if not pix.isNull() else None
        else:
            self._bg_pixmap = None
        self.update()

    def _open_path(self, path: str) -> None:
        if not path:
            return
        target = Path(path)
        if target.is_file():
            target = target.parent
        if not target.exists():
            self.status_hint("路径不存在：" + str(target))
            return
        try:
            os.startfile(str(target))  # noqa: S606 - 打开资源管理器
        except OSError as exc:
            self.status_hint(f"无法打开：{exc.strerror or exc}")

    def _show_notes(self, title: str, notes: list[str]) -> None:
        if not notes:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(720, 420)
        layout = QVBoxLayout(dialog)
        from PySide6.QtWidgets import QTextBrowser
        view = QTextBrowser()
        view.setPlainText("\n".join(notes))
        layout.addWidget(view)
        close = QPushButton("知道了")
        close.setProperty("kind", "primary")
        close.clicked.connect(dialog.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)
        dialog.exec()

    def _startup_notes(self) -> None:
        """打开后的来源变化检查放到后台线程，不阻塞界面（大量专辑时逐个
        校验文件签名可达数十秒）。完成后如有提示再弹出。"""
        if self.service.startup_notes:
            self._show_notes("打开前的检查", self.service.startup_notes)
            return

        def task():
            return self.service.startup_check()

        def done(notes) -> None:
            if notes:
                self._show_notes("打开前的检查", notes)

        def failed(message: str) -> None:
            # 不静默吞错：至少在状态栏留下一条可见提示
            self.status_hint("打开前的检查未完成：" + message.splitlines()[0])

        self.runner.run(task, on_done=done, on_error=failed,
                        on_cancelled=lambda: None, pass_progress=False)

    # ------------------------------------------------------------------
    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Space:
            rows = self.table.selectionModel().selectedRows()
            if rows:
                unit = self.table_model.unit_at(rows[0].row())
                if unit is not None:
                    self._check_selected(not unit.checked)
                    return
        super().keyPressEvent(event)

    def _on_splitter_moved(self, *_args) -> None:
        self.service.settings.splitter_sizes = self.splitter.sizes()

    def closeEvent(self, event) -> None:
        if self._busy:
            answer = QMessageBox.question(
                self, "任务进行中",
                "后台任务正在进行（可能是文件移动/复制）。\n"
                "退出将请求停止任务，并等待当前文件操作安全落地。确定退出吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._wait_worker_shutdown()
        self._save_geometry()
        self.service.save_state()
        super().closeEvent(event)

    def _wait_worker_shutdown(self, timeout_ms: int = 30000) -> None:
        """请求取消并等待后台任务落地，避免进程退出时工作线程被硬杀
        （半写入文件、journal 停在 running、QThread 销毁崩溃）。"""
        worker = self.runner.current
        if worker is None:
            self.runner.cancel_current()
            return
        loop = QEventLoop()
        # 先接线再取消，消除"任务在接线前已结束"的竞态
        worker.signals.finished.connect(loop.quit)
        worker.signals.failed.connect(loop.quit)
        worker.signals.cancelled.connect(loop.quit)
        self.runner.cancel_current()
        QTimer.singleShot(timeout_ms, loop.quit)   # 兜底：极端情况下也保证能退出
        loop.exec()


class RunResultDialog(QDialog):
    """执行结果：成功/失败/跳过分别列出，可重试、查看历史或撤销。"""

    def __init__(self, summary, parent=None) -> None:
        super().__init__(parent)
        self.summary = summary
        self.undo_requested = False
        self.setWindowTitle("整理结果")
        self.resize(860, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        title = QLabel(summary.summary_text())
        title.setObjectName("Title")
        layout.addWidget(title)
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QTextBrowser
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["操作", "状态", "来源", "目标", "说明"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setColumnWidth(0, 96)
        table.setColumnWidth(1, 72)
        table.setColumnWidth(2, 240)
        table.setColumnWidth(3, 260)
        table.horizontalHeader().setStretchLastSection(True)
        table.setRowCount(len(summary.results))
        for row, result in enumerate(summary.results):
            from .history_dialog import OP_LABELS
            table.setItem(row, 0, QTableWidgetItem(OP_LABELS.get(result.op, result.op)))
            table.setItem(row, 1, QTableWidgetItem(
                {"success": "成功", "failed": "失败", "partial": "部分完成",
                 "skipped": "跳过", "cancelled": "已取消"}.get(result.status, result.status)))
            table.setItem(row, 2, QTableWidgetItem(result.source))
            table.setItem(row, 3, QTableWidgetItem(result.target))
            table.setItem(row, 4, QTableWidgetItem(result.message))
        layout.addWidget(table, 2)
        # 来源文件夹处理汇总：已移除 / 因剩余内容保留 / 因任务未完成保留
        dir_removed: list[str] = []
        dir_kept_remaining: list[str] = []
        incomplete = any(r.status in ("failed", "partial", "cancelled")
                         for r in summary.results)
        for result in summary.results:
            dirs = (result.extra or {}).get("source_dirs") or {}
            dir_removed.extend(dirs.get("removed") or [])
            dir_kept_remaining.extend(dirs.get("kept_remaining") or [])
        summary_lines = [f"操作记录：{summary.run_dir}",
                         f"开始：{summary.started_at}　结束：{summary.finished_at}"]
        if summary.mode == "move":
            parts = []
            if dir_removed:
                parts.append(f"来源空文件夹已移除 {len(dir_removed)} 个")
            if dir_kept_remaining:
                parts.append(f"{len(dir_kept_remaining)} 个来源文件夹因仍有未整理内容而保留")
            if incomplete:
                parts.append("存在未完成任务，相关来源文件夹未清理")
            if parts:
                summary_lines.append("来源文件夹：" + "；".join(parts))
            elif not any(r.status == "success" for r in summary.results):
                summary_lines.append("来源文件夹：本次没有成功移动的项目，未做清理")
        summary_lines.append("可在“历史”中选择本次记录进行撤销；撤销时将恢复执行中移除的来源目录。")
        notes = list(getattr(summary, "notes", []) or [])
        if notes:
            summary_lines.append("")
            summary_lines.append("歌词提醒（未随本次执行移动，仍保留在来源目录）：")
            summary_lines.extend("· " + note for note in notes[:20])
        lyrics_done = [rec for r in summary.results
                       for rec in ((r.extra or {}).get("lyrics") or [])]
        if lyrics_done:
            summary_lines.append("")
            summary_lines.append(
                f"随歌曲处理的歌词：{len(lyrics_done)} 个（只移动/复制，内容与编码未改动）")
            for rec in lyrics_done[:10]:
                summary_lines.append(
                    f"· {Path(rec.get('source', '')).name} → {rec.get('target', '')}"
                    f"（{rec.get('encoding') or '编码未识别'}）")
            summary_lines.append("歌词关联与去向已写入操作历史，可随本次记录一并撤销。")
        record = QTextBrowser()
        record.setMinimumHeight(90)
        record.setPlainText("\n".join(summary_lines))
        layout.addWidget(record, 1)
        row = QHBoxLayout()
        open_dir = QPushButton("打开记录目录")
        open_dir.setProperty("kind", "ghost")
        open_dir.clicked.connect(self._open_dir)
        history_btn = QPushButton("去历史撤销…")
        history_btn.setProperty("kind", "ghost")
        history_btn.clicked.connect(self._go_history)
        close = QPushButton("完成")
        close.setProperty("kind", "primary")
        close.clicked.connect(self.accept)
        row.addWidget(open_dir)
        row.addWidget(history_btn)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

    def _open_dir(self) -> None:
        try:
            os.startfile(self.summary.run_dir)  # noqa: S606
        except OSError:
            pass

    def _go_history(self) -> None:
        self.undo_requested = True
        self.accept()
