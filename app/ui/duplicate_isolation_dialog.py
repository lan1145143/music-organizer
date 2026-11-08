"""查重隔离确认弹窗：分组预览「保留 / 移入 a重复歌曲」，确认后执行非破坏性隔离。

极简布局（任务书 §3.2）：
- 头部：标题 + 说明文案 + 重新检测；
- 中部：按歌曲分组的可折叠预览列表，组内每行可点击切换保留项；
- 底部：取消 / 一键移入「a重复歌曲」（主按钮），执行中显示进度条。

实际移动由服务层 isolate_duplicates 完成（Executor 全链路：日志/历史/撤销）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from .theme import tones_for
from .workers import TaskRunner

_ISOLATION_DIR = "a重复歌曲"


def _unit_meta_line(unit) -> str:
    bits = []
    if unit.fmt:
        bits.append(unit.fmt.upper())
    if unit.bitrate:
        bits.append(f"{unit.bitrate // 1000}kbps")
    if unit.sample_rate:
        bits.append(f"{unit.sample_rate / 1000:g}kHz")
    if unit.is_album:
        bits.append(f"{unit.file_count or len(unit.audio_files)} 个文件")
    return " · ".join(bits) or "—"


class MemberRow(QFrame):
    """组内一个版本行：点击即切换为该组的保留版本。"""

    clicked = Signal(str)   # unit_id

    def __init__(self, unit, keep: bool, tones: dict, parent=None) -> None:
        super().__init__(parent)
        self.unit = unit
        self._tones = tones
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("DupRowKeep" if keep else "DupRow")
        if keep:
            # 保留版本行浅绿底色（与徽章同语气色），其余行透明
            self.setStyleSheet(
                "QFrame#DupRowKeep { background: "
                + tones.get("green", ("#ECFDF5",))[0]
                + "; border-radius: 6px; }")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 10, 6)
        row.setSpacing(10)
        self.badge = QLabel("保留" if keep else f"移入 {_ISOLATION_DIR}")
        self.badge.setProperty("tone", "green" if keep else "amber")
        self.badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        row.addWidget(self.badge)
        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        name = QLabel(Path(unit.path).name if not unit.is_album
                      else Path(unit.boundary_path or unit.path).name)
        name.setObjectName("Muted" if not keep else "")
        if keep:
            f = name.font()
            f.setBold(True)
            name.setFont(f)
        name.setWordWrap(False)
        text_col.addWidget(name)
        meta = QLabel(f"{_unit_meta_line(unit)}　·　{unit.path}")
        meta.setObjectName("Muted")
        meta.setWordWrap(False)
        text_col.addWidget(meta)
        row.addLayout(text_col, 1)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.unit.id)
        super().mousePressEvent(event)


class GroupCard(QFrame):
    """一组重复项：头部歌曲名 + 折叠开关，内容为各版本行。"""

    keepChanged = Signal(str, str)   # group_id, keep_unit_id

    def __init__(self, group: dict, tones: dict, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.group = group
        self._tones = tones
        self.keep_id = group["keep"].id
        col = QVBoxLayout(self)
        col.setContentsMargins(10, 8, 10, 8)
        col.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        keep = group["keep"]
        title = keep.title or keep.display_album or keep.name
        self.title_label = QLabel(f"{title}（{len(group['members'])} 个版本）")
        self.title_label.setWordWrap(False)
        head.addWidget(self.title_label, 1)
        self.toggle_btn = QPushButton("收起")
        self.toggle_btn.setProperty("kind", "ghost")
        self.toggle_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_btn.clicked.connect(self._toggle)
        head.addWidget(self.toggle_btn)
        col.addLayout(head)

        self.rows_box = QWidget()
        rows = QVBoxLayout(self.rows_box)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(4)
        for unit in group["members"]:
            row = MemberRow(unit, keep=unit.id == self.keep_id, tones=self._tones)
            row.clicked.connect(self._set_keep)
            rows.addWidget(row)
        col.addWidget(self.rows_box)

    def _toggle(self) -> None:
        collapsed = self.rows_box.isVisible()
        self.rows_box.setVisible(not collapsed)
        self.toggle_btn.setText("展开" if collapsed else "收起")

    def _set_keep(self, unit_id: str) -> None:
        if unit_id == self.keep_id:
            return
        self.keep_id = unit_id
        for row in self.rows_box.findChildren(MemberRow):
            is_keep = row.unit.id == unit_id
            row.setObjectName("DupRowKeep" if is_keep else "DupRow")
            row.badge.setText("保留" if is_keep else f"移入 {_ISOLATION_DIR}")
            row.badge.setProperty("tone", "green" if is_keep else "amber")
            row.badge.style().unpolish(row.badge)
            row.badge.style().polish(row.badge)
        self.keepChanged.emit(self.group["id"], unit_id)


class DuplicateIsolationDialog(QDialog):
    def __init__(self, service, parent=None, runner: TaskRunner | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner or TaskRunner(self)
        self.keep_by_group: dict[str, str] = {}
        self.isolation_result: dict | None = None
        self._tones = tones_for(service.settings.theme)
        self.setWindowTitle("查重隔离")
        self.resize(980, 720)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        self.title = QLabel("")
        self.title.setObjectName("Title")
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        hint = QLabel(
            "系统已自动为您保留音质最佳、标签最完整的版本。其余重复文件将被安全移入"
            "各来源根目录下的「a重复歌曲」文件夹，不会直接删除；"
            "处理记录可在历史中撤销。点击列表中的版本行可手动切换保留项。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.list_box = QWidget()
        self.list_layout = QVBoxLayout(self.list_box)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        scroll.setWidget(self.list_box)
        layout.addWidget(scroll, 1)

        self.empty_label = QLabel("当前没有可隔离的重复项。")
        self.empty_label.setObjectName("Muted")
        self.empty_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.empty_label)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        bottom = QHBoxLayout()
        self.location_label = QLabel("")
        self.location_label.setObjectName("Muted")
        bottom.addWidget(self.location_label, 1)
        self.btn_redetect = QPushButton("重新检测")
        self.btn_redetect.setProperty("kind", "ghost")
        self.btn_redetect.clicked.connect(self._redetect)
        bottom.addWidget(self.btn_redetect)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(btn_cancel)
        self.btn_isolate = QPushButton("")
        self.btn_isolate.setProperty("kind", "primary")
        self.btn_isolate.clicked.connect(self._execute)
        bottom.addWidget(self.btn_isolate)
        layout.addLayout(bottom)

        self._reload()

    # ------------------------------------------------------------------
    def _reload(self) -> None:
        self.groups = self.service.duplicate_isolation_groups(self.keep_by_group)
        self._sync_keep_map()
        # 清空旧卡片（保留末尾 stretch）
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for group in self.groups:
            card = GroupCard(group, self._tones)
            card.keepChanged.connect(self._on_keep_changed)
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)
        count = sum(len(g["isolates"]) for g in self.groups)
        self.empty_label.setVisible(not self.groups)
        self.title.setText(f"发现 {count} 首重复可隔离歌曲" if self.groups
                           else "当前音乐库无重复歌曲")
        self.btn_isolate.setText(f"一键移入「{_ISOLATION_DIR}」（{count} 首）")
        self.btn_isolate.setEnabled(count > 0)
        dirs = sorted({str(Path(s.path) / _ISOLATION_DIR)
                       for s in self.service.state.sources}) if self.groups else []
        self.location_label.setText("隔离位置：" + ("；".join(dirs[:2])
                                                  + (f" 等 {len(dirs)} 个来源根" if len(dirs) > 2 else "")
                                                  if dirs else "—"))

    def _sync_keep_map(self) -> None:
        for group in self.groups:
            current = self.keep_by_group.get(group["id"])
            if current and any(u.id == current for u in group["members"]):
                continue
            self.keep_by_group[group["id"]] = group["keep"].id

    def _on_keep_changed(self, group_id: str, unit_id: str) -> None:
        self.keep_by_group[group_id] = unit_id

    # ------------------------------------------------------------------
    def _redetect(self) -> None:
        """在弹窗内重跑查重（来源变化后可刷新结果），完成后重建列表。"""
        if self.btn_redetect.property("running"):
            return
        self.btn_redetect.setProperty("running", True)
        self.btn_redetect.setText("检测中…")
        self.btn_redetect.setEnabled(False)
        self.btn_isolate.setEnabled(False)

        def task(progress, cancel):
            return self.service.run_duplicate_check(progress, cancel)

        def done(_result) -> None:
            self.btn_redetect.setProperty("running", False)
            self.btn_redetect.setText("重新检测")
            self.btn_redetect.setEnabled(True)
            self.keep_by_group = {}
            self._reload()

        def failed(message: str) -> None:
            self.btn_redetect.setProperty("running", False)
            self.btn_redetect.setText("重新检测")
            self.btn_redetect.setEnabled(True)
            self.btn_isolate.setEnabled(bool(self.groups))
            QMessageBox.critical(self, "检测失败", message)

        self.runner.run(task, on_done=done, on_error=failed,
                        on_progress=self._on_progress)

    def _on_progress(self, message: str, current: int, total: int) -> None:
        if not self.progress.isVisible() and total:
            self.progress.setRange(0, 100)
            self.progress.setVisible(True)
        if total:
            self.progress.setValue(int(current / total * 100))
        self.location_label.setText(message)

    # ------------------------------------------------------------------
    def _execute(self) -> None:
        if not self.groups:
            return
        self.btn_isolate.setEnabled(False)
        self.btn_redetect.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(True)

        overrides = dict(self.keep_by_group)

        def task(progress, cancel):
            return self.service.isolate_duplicates(overrides, progress=progress,
                                                   cancel=cancel)

        def done(result: dict) -> None:
            self.progress.setVisible(False)
            self.isolation_result = result
            self.accept()

        def failed(message: str) -> None:
            self.progress.setVisible(False)
            self.btn_isolate.setEnabled(True)
            self.btn_redetect.setEnabled(True)
            QMessageBox.critical(self, "隔离失败", message)

        self.runner.run(task, on_done=done, on_error=failed,
                        on_progress=self._on_progress)
