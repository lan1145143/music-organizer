"""检查并整理：统一整理中心（合并“待处理”与“预览整理”入口）。

在同一处查看和处理：本次已勾选项目及其提醒/阻止原因、集中提醒与批量操作、
本次排除项、最终执行预览。修改影响计划的内容后重新生成计划，
预览后来源/目标变化不会被覆盖（执行前再次核对由执行器与计划校验保证）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QLabel,
                               QInputDialog, QMessageBox, QProgressDialog, QPushButton,
                               QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
                               QVBoxLayout, QWidget)

from ..domain import naming
from ..domain.models import PlanStatus, Unit, UnitStatus
from ..planning.plan import Plan
from ..planning.review import evaluate_unit
from .workers import TaskRunner

REVIEW_COLUMNS = ["条目", "分组", "来源", "目标预览", "提醒 / 阻止原因", "处理"]
PLAN_COLUMNS = ["执行", "操作类型", "条目", "来源", "目标", "状态", "提示"]


def preserve_plan_selection(old_items: list, new_items: list) -> None:
    """按 (unit_id, op) 把旧计划的勾选状态回写到新计划。

    PlanItem.id 每次重建都是新 uuid，不能作键；unit_id + op 在重建间稳定。
    用户在最终预览页明确取消勾选的条目，重建后必须保持取消，不得恢复执行。
    """
    if not old_items:
        return
    old = {(item.unit_id, item.op): item.selected for item in old_items}
    for item in new_items:
        key = (item.unit_id, item.op)
        if key in old:
            item.selected = old[key]


class ReviewCenterDialog(QDialog):
    changed = Signal()
    locateRequested = Signal(str)
    rescanRequested = Signal()

    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.plan: Plan | None = None
        self.rescan_requested = False
        self.runner = TaskRunner(self)      # 查重等耗时操作在后台线程执行
        self._dup_running = False
        self.setWindowTitle("检查并整理")
        self.resize(1220, 780)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        self.summary = QLabel("")
        self.summary.setObjectName("Title")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.hint = QLabel("可整理项已默认勾选；提醒在此集中确认，无需逐项弹窗。"
                           "未勾选与未就绪的项目会列在“本次排除”，不会阻塞其他项目。")
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_checked_tab()
        self._build_reminder_tab()
        self._build_excluded_tab()
        self._build_preview_tab()

        bottom = QHBoxLayout()
        self.mode_label = QLabel("")
        self.mode_label.setObjectName("Muted")
        bottom.addWidget(self.mode_label, 1)
        btn_refresh = QPushButton("重新生成计划")
        btn_refresh.setProperty("kind", "ghost")
        btn_refresh.clicked.connect(self._rebuild)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.reject)
        self.btn_start = QPushButton("开始整理")
        self.btn_start.setProperty("kind", "primary")
        self.btn_start.clicked.connect(self._start)
        bottom.addWidget(btn_refresh)
        bottom.addWidget(btn_close)
        bottom.addWidget(self.btn_start)
        layout.addLayout(bottom)

        self._rebuild()

    # ------------------------------------------------------------------
    # 标签页构建
    # ------------------------------------------------------------------
    def _build_checked_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.checked_table = QTableWidget(0, len(REVIEW_COLUMNS))
        self.checked_table.setHorizontalHeaderLabels(REVIEW_COLUMNS)
        self.checked_table.verticalHeader().setVisible(False)
        self.checked_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.checked_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.checked_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.checked_table.setAlternatingRowColors(True)
        self.checked_table.verticalHeader().setDefaultSectionSize(34)
        self.checked_table.horizontalHeader().setStretchLastSection(True)
        self.checked_table.setColumnWidth(0, 210)
        self.checked_table.setColumnWidth(1, 66)
        self.checked_table.setColumnWidth(2, 220)
        self.checked_table.setColumnWidth(3, 220)
        self.checked_table.setColumnWidth(4, 300)
        layout.addWidget(self.checked_table, 1)
        self.action_note = QLabel("")
        self.action_note.setObjectName("Muted")
        self.action_note.setWordWrap(True)
        layout.addWidget(self.action_note)
        row = QHBoxLayout()
        btn_locate = QPushButton("定位到列表")
        btn_locate.setProperty("kind", "ghost")
        btn_locate.clicked.connect(self._locate_checked)
        btn_uncheck = QPushButton("取消勾选所选")
        btn_uncheck.setProperty("kind", "ghost")
        btn_uncheck.clicked.connect(self._uncheck_selected)
        row.addWidget(btn_locate)
        row.addWidget(btn_uncheck)
        row.addStretch(1)
        layout.addLayout(row)
        self.tabs.addTab(page, "已勾选项目")

    def _build_reminder_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.reminder_list = QTextBrowser()
        layout.addWidget(self.reminder_list, 2)
        self.reminder_table = QTableWidget(0, 3)
        self.reminder_table.setHorizontalHeaderLabels(["条目", "提醒", "操作建议"])
        self.reminder_table.verticalHeader().setVisible(False)
        self.reminder_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.reminder_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.reminder_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.reminder_table.horizontalHeader().setStretchLastSection(True)
        self.reminder_table.setColumnWidth(0, 240)
        self.reminder_table.setColumnWidth(1, 420)
        self.reminder_table.setMinimumHeight(160)
        layout.addWidget(self.reminder_table, 2)
        row = QHBoxLayout()
        btn_assign = QPushButton("统一指定整理艺术家…")
        btn_assign.clicked.connect(self._assign_common_artist)
        btn_dup = QPushButton("检测重复")
        btn_dup.setProperty("kind", "ghost")
        btn_dup.clicked.connect(self._run_duplicates)
        self.btn_dup = btn_dup
        for btn in (btn_assign, btn_dup):
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.reminder_note = QLabel("")
        self.reminder_note.setObjectName("Muted")
        self.reminder_note.setWordWrap(True)
        layout.addWidget(self.reminder_note)
        self.tabs.addTab(page, "提醒汇总")

    def _build_excluded_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.excluded_table = QTableWidget(0, 4)
        self.excluded_table.setHorizontalHeaderLabels(
            ["条目", "状态", "原因", "处理"])
        self.excluded_table.verticalHeader().setVisible(False)
        self.excluded_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.excluded_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.excluded_table.verticalHeader().setDefaultSectionSize(34)
        self.excluded_table.horizontalHeader().setStretchLastSection(True)
        self.excluded_table.setColumnWidth(0, 230)
        self.excluded_table.setColumnWidth(1, 96)
        self.excluded_table.setColumnWidth(2, 420)
        layout.addWidget(self.excluded_table, 1)
        tip = QLabel("这些项目本次不会移动/复制/归档。可在此直接处理（确认边界、补信息、"
                     "选择压缩包方式、勾选纳入等），动作完成后计划自动刷新。")
        tip.setObjectName("Muted")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        self.tabs.addTab(page, "本次排除")

    def _build_preview_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.preview_table = QTableWidget(0, len(PLAN_COLUMNS))
        self.preview_table.setHorizontalHeaderLabels(PLAN_COLUMNS)
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        self.preview_table.setColumnWidth(0, 48)
        self.preview_table.setColumnWidth(1, 92)
        self.preview_table.setColumnWidth(2, 200)
        self.preview_table.setColumnWidth(3, 240)
        self.preview_table.setColumnWidth(4, 280)
        self.preview_table.itemChanged.connect(self._on_preview_item_changed)
        self.preview_table.currentCellChanged.connect(
            lambda *_args: self._on_preview_row_changed())
        layout.addWidget(self.preview_table, 3)
        self.preview_detail = QTextBrowser()
        self.preview_detail.setMinimumHeight(96)
        layout.addWidget(self.preview_detail, 1)
        self.policy_view = QTextBrowser()
        self.policy_view.setObjectName("Muted")
        self.policy_view.setMaximumHeight(64)
        layout.addWidget(self.policy_view)
        tools = QHBoxLayout()
        btn_all = QPushButton("全选可执行")
        btn_none = QPushButton("全部取消")
        btn_skip = QPushButton("跳过冲突项")
        btn_rename = QPushButton("自动改名解决冲突")
        for btn in (btn_all, btn_none, btn_skip, btn_rename):
            btn.setProperty("kind", "ghost")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        btn_skip.clicked.connect(self._skip_conflicts)
        btn_rename.clicked.connect(self._rename_conflicts)
        for btn in (btn_all, btn_none, btn_skip, btn_rename):
            tools.addWidget(btn)
        tools.addStretch(1)
        self.preview_counts = QLabel("")
        self.preview_counts.setObjectName("Muted")
        tools.addWidget(self.preview_counts)
        layout.addLayout(tools)
        self.tabs.addTab(page, "最终预览")
        self.tabs.currentChanged.connect(self._on_tab_changed)

    # ------------------------------------------------------------------
    # 数据填充
    # ------------------------------------------------------------------
    def _rebuild(self) -> None:
        # 目标根目录内容可能已变化：艺术家目录解析缓存只对单次填充有效
        self._artist_dir_cache = {}
        self.plan = self.service.build_plan()
        self._fill_checked()
        self._fill_reminders()
        self._fill_excluded()
        self._fill_preview()
        self._fill_mode_note()
        self._update_summary()

    def _fill_mode_note(self) -> None:
        """全自动零干预模式说明（固定策略，无可配置开关）。"""
        self.policy_view.setPlainText(
            "全自动模式：所有扫描项自动流转——边界自动采用、缺信息用兜底值、"
            "压缩包不解压（随音频归档或进原始压缩包）、重复内容仅提示并保留各份；"
            "目标同名/路径超长自动改名或截断，绝不覆盖任何文件。"
            "仅来源失联项跳过并写入报告。")

    def _duplicates_checked(self) -> bool:
        return bool(self.service.state.duplicates_checked_at)

    def _resolve_artist_cached(self, artist: str) -> tuple[str, bool]:
        """单次填充内缓存 resolve_artist_dir：每次调用都全量列目标根目录，
        逐行预览时 100 行就是 100 次 iterdir（网络盘明显卡）。
        缓存只在一次填充内有效，_rebuild/_after_action 时清空。"""
        key = artist.strip()
        cache = getattr(self, "_artist_dir_cache", None)
        if cache is None:
            cache = self._artist_dir_cache = {}
        if key not in cache:
            cache[key] = self.service.resolve_artist_dir(key)
        return cache[key]

    def _target_preview(self, unit: Unit) -> str:
        root = self.service.settings.target_dir
        if not root:
            return "（未选择目标目录）"
        try:
            if unit.is_album:
                artist_dir, _reused = self._resolve_artist_cached(
                    naming.display_artist(unit))
                return str(naming.album_target_dir(root, unit,
                                                   self.service.settings.template,
                                                   artist_dir))
            if unit.is_track:
                if unit.dup_role == "duplicate":
                    return str(Path(root) / "重复文件" / Path(unit.path).name)
                artist_dir, _reused = self._resolve_artist_cached(
                    naming.display_artist(unit))
                return str(naming.track_target_path(root, unit,
                                                    self.service.settings.template,
                                                    self.service.settings.track_digits,
                                                    artist_dir))
            if unit.is_archive:
                return str(Path(root) / "原始压缩包" / Path(unit.path).name)
        except naming.NamingError as exc:
            return f"（缺少信息：{exc}）"
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def _fill_checked(self) -> None:
        rows = [u for u in self.service.state.units if u.checked]
        self.checked_table.setRowCount(len(rows))
        for row, unit in enumerate(rows):
            review = evaluate_unit(unit)
            group_label = "可整理" if review.organizable else "需处理"
            name = f"{unit.kind_enum.label} · {unit.name}"
            source = unit.boundary_path if unit.is_album else unit.path
            note = review.reminder_text or review.reason_text or "—"
            if review.blockers and review.reminders:
                note = review.reminder_text + "｜阻止：" + review.reason_text
            values = [name, group_label, source, self._target_preview(unit), note]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(Qt.UserRole, unit.id)
                item.setToolTip(value if col in (2, 3) else "")
                self.checked_table.setItem(row, col, item)
            if review.blockers:
                from PySide6.QtGui import QColor
                self.checked_table.item(row, 1).setForeground(QColor("#C0392B"))
            self.checked_table.setCellWidget(row, 5, self._action_widget(unit, review))

    def _fill_reminders(self) -> None:
        lines: list[str] = []
        rows: list[tuple[Unit, object]] = []
        units = self.service.state.units
        for unit in units:
            review = evaluate_unit(unit)
            for reminder in review.reminders:
                rows.append((unit, reminder))
        self.reminder_table.setRowCount(len(rows))
        for row, (unit, reminder) in enumerate(rows):
            name_item = QTableWidgetItem(f"{unit.kind_enum.label} · {unit.name}")
            name_item.setData(Qt.UserRole, unit.id)
            self.reminder_table.setItem(row, 0, name_item)
            self.reminder_table.setItem(row, 1, QTableWidgetItem(str(reminder)))
            advice = self._advice_for(unit, str(reminder))
            self.reminder_table.setItem(row, 2, QTableWidgetItem(advice))
        if not self._duplicates_checked():
            lines.append("· 重复检测尚未运行：当前显示“未检测”，不代表无重复。可点“检测重复”。")
        else:
            count = len(self.service.state.duplicate_groups)
            lines.append(f"· 重复检测已于 {self.service.state.duplicates_checked_at} 完成："
                         f"共 {count} 组。重复项默认保留各份继续整理；"
                         "如需移出多余副本，可在主界面点「查重」打开隔离面板"
                         "（安全移入「a重复歌曲」，不删除）。")
        unknown = [u for u in units if u.is_album and not (u.album_artist or "").strip()
                   and u.checked]
        if unknown:
            lines.append(f"· {len(unknown)} 个已勾选专辑未指定整理艺术家，"
                         "将放入“未知艺术家”目录：可在本页批量指定，或在最终预览确认继续。")
        self.reminder_note.setText("\n".join(lines))
        self.reminder_list.setPlainText(
            "\n".join(f"· {unit.name}｜{reminder}" for unit, reminder in rows[:200])
            or "当前没有提醒。")

    @staticmethod
    def _advice_for(unit: Unit, reminder: str) -> str:
        if "未知艺术家" in reminder or "建议" in reminder:
            return "可在本页批量指定，或整批确认继续"
        if "重复" in reminder:
            return "保留各份；如需隔离多余副本，回主界面点「查重」"
        if "未检测" in reminder:
            return "点“检测重复”后复核"
        if "跳过" in reminder:
            return "本次不参与整理"
        return "—"

    def _fill_excluded(self) -> None:
        entries = list(self.plan.excluded) if self.plan else []
        self.excluded_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            name_item = QTableWidgetItem(str(entry.get("name", "")))
            name_item.setData(Qt.UserRole, str(entry.get("unit_id", "")))
            self.excluded_table.setItem(row, 0, name_item)
            self.excluded_table.setItem(row, 1, QTableWidgetItem(str(entry.get("status", ""))))
            reason = str(entry.get("reason", ""))
            reason_item = QTableWidgetItem(reason)
            reason_item.setToolTip(reason)
            self.excluded_table.setItem(row, 2, reason_item)
            unit = self.service.state.unit_by_id(str(entry.get("unit_id", "")))
            if unit is not None:
                self.excluded_table.setCellWidget(
                    row, 3, self._action_widget(unit, None,
                                                from_status=str(entry.get("status", ""))))
        if not entries:
            self.excluded_table.setRowCount(1)
            empty = QTableWidgetItem("本次没有排除项：已勾选项目全部就绪。")
            empty.setFlags(Qt.ItemIsEnabled)
            self.excluded_table.setItem(0, 0, empty)

    def _fill_preview(self) -> None:
        self.preview_table.blockSignals(True)
        plan = self.plan
        self.preview_table.setRowCount(len(plan.items) if plan else 0)
        if plan:
            for row, item in enumerate(plan.items):
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                check.setCheckState(Qt.Checked if item.selected else Qt.Unchecked)
                if item.status in (PlanStatus.BLOCKED.value, PlanStatus.SKIPPED.value):
                    check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    check.setCheckState(Qt.Unchecked)
                    check.setToolTip("跳过/异常项：不参与本次执行，其余项目照常")
                self.preview_table.setItem(row, 0, check)
                self.preview_table.setItem(row, 1, QTableWidgetItem(item.op_enum.label))
                self.preview_table.setItem(row, 2, QTableWidgetItem(item.label))
                self.preview_table.setItem(row, 3, QTableWidgetItem(item.source))
                self.preview_table.setItem(row, 4, QTableWidgetItem(item.target))
                self.preview_table.setItem(row, 5, QTableWidgetItem(item.status_enum.label))
                self.preview_table.setItem(row, 6, QTableWidgetItem("；".join(item.messages)))
        self.preview_table.blockSignals(False)
        self._update_preview_counts()

    def _update_summary(self) -> None:
        summary = self.service.group_summary()
        checked = summary["ready_checked"] + summary["needs_checked"]
        plan = self.plan
        ready = len(plan.ready_items) if plan else 0
        skipped = sum(1 for i in (plan.items if plan else [])
                      if i.status == PlanStatus.SKIPPED.value)
        self.summary.setText(
            f"已勾选 {checked} 项（可整理 {summary['ready_checked']}｜需处理 "
            f"{summary['needs_checked']}）｜计划：可执行 {ready}，跳过 {skipped}"
            + ("" if self._duplicates_checked() else "｜未检测重复"))
        mode = "复制" if self.service.settings.mode == "copy" else "移动"
        self.mode_label.setText(
            f"整理方式：{mode}　目标：{self.service.settings.target_dir or '（未选择）'}　"
            f"开始整理前会再次核对来源与目标。")
        self.btn_start.setEnabled(ready > 0)
        self.btn_start.setText(f"开始整理（{ready} 项）")

    def _update_preview_counts(self) -> None:
        plan = self.plan
        if plan is None:
            self.preview_counts.setText("")
            return
        selected = sum(1 for i in plan.items if i.selected)
        ready = len(plan.ready_items)
        self.preview_counts.setText(
            f"计划共 {len(plan.items)} 项：已选 {selected}，可执行 {ready}")

    # ------------------------------------------------------------------
    # 行内处理：阻止原因 → 对应动作，直接在本界面完成，不必回主窗口
    # ------------------------------------------------------------------
    def _action_widget(self, unit, review=None, from_status: str = ""):
        widget = QWidget()
        bar = QHBoxLayout(widget)
        bar.setContentsMargins(2, 0, 2, 0)
        bar.setSpacing(4)
        for label, handler in self._actions_for(unit, review, from_status):
            btn = QPushButton(label)
            btn.setProperty("kind", "ghost")
            btn.setToolTip("在本界面直接处理，完成后计划自动刷新")
            btn.clicked.connect(handler)
            bar.addWidget(btn)
        bar.addStretch(1)
        return widget

    def _actions_for(self, unit, review=None, from_status: str = ""):
        acts: list[tuple[str, object]] = []
        if review is None:
            review = evaluate_unit(unit)
        text = "；".join(review.blockers)
        if "缺少歌曲名" in text:
            acts.append(("补全歌曲名…", lambda _=False, u=unit: self._edit_field(u, "title", "歌曲名")))
        if "缺少歌曲艺术家" in text:
            acts.append(("补全艺术家…", lambda _=False, u=unit: self._edit_field(u, "artist", "歌曲艺术家")))
        if "来源已失联" in text:
            acts.append(("重新扫描", lambda _=False: self._request_rescan()))
        if not review.blockers:
            if unit.is_album and not (unit.album_artist or "").strip():
                suggestion = (unit.artist_suggestion or "").strip()
                if suggestion:
                    short = suggestion if len(suggestion) <= 10 else suggestion[:10] + "…"
                    acts.append((f"采用建议“{short}”",
                                 lambda _=False, u=unit, a=suggestion: self._adopt_suggestion(u, a)))
                acts.append(("指定艺术家…", lambda _=False, u=unit: self._assign_artist([u])))
            if any(a.get("role") == "lyrics" and not a.get("selected")
                   for a in unit.attachments):
                acts.append(("勾选歌词", lambda _=False, u=unit: self._check_lyrics(u)))
            if from_status in ("未勾选",) and unit.kind != "attachment":
                acts.append(("勾选纳入本次", lambda _=False, u=unit: self._include_unit(u)))
        if from_status == "歌词未勾选":
            acts.append(("勾选歌词", lambda _=False, u=unit: self._check_lyrics(u)))
        if unit.status == UnitStatus.NEEDS_LINK.value and unit.kind == "attachment":
            acts.append(("处理歌词…", lambda _=False: self._open_lyrics()))
        return acts

    def _after_action(self, note: str = "") -> None:
        if note:
            self.action_note.setText(note)
        # 需处理转可整理的项目按规则自动勾选；用户显式选择优先
        self.service.apply_default_selection()
        self.service.save_state()
        self.changed.emit()
        self._rebuild()

    def _change_boundary(self, unit) -> None:
        boundary = Path(unit.boundary_path or unit.path)
        options: list[str] = []
        paths: list[str] = []
        if unit.candidate_parent:
            options.append("上级：" + (Path(unit.candidate_parent).name or unit.candidate_parent))
            paths.append(unit.candidate_parent)
        parent = str(boundary.parent)
        if parent not in paths:
            options.append("上级：" + (Path(parent).name or parent))
            paths.append(parent)
        try:
            children = sorted(p for p in boundary.iterdir() if p.is_dir())
        except OSError:
            children = []
        for child in children[:20]:
            options.append("子级：" + child.name)
            paths.append(str(child))
        if not options:
            return
        choice, ok = QInputDialog.getItem(self, "改选整夹边界",
                                          f"为“{unit.name}”选择新的整夹范围：",
                                          options, 0, False)
        if not ok:
            return
        path = paths[options.index(choice)]
        self.service.confirm_boundary(unit, path)
        self._after_action(f"已改选整夹边界：{unit.name} → {path}")

    def _edit_field(self, unit, field: str, label: str) -> None:
        current = str(getattr(unit, field, "") or "")
        value, ok = QInputDialog.getText(self, f"补全{label}",
                                         f"“{unit.name}”的{label}：", text=current)
        if not ok or not value.strip():
            return
        self.service.update_unit_fields(unit, {field: value.strip()})
        self._after_action(f"已补全{label}：{unit.name} → {value.strip()}")

    def _request_rescan(self) -> None:
        self.rescan_requested = True
        self.changed.emit()
        self.close()

    def _adopt_suggestion(self, unit, artist: str) -> None:
        self.service.assign_organize_artist([unit], artist)
        self._after_action(f"已采用建议：{unit.name} → {artist}")

    def _assign_artist(self, units) -> None:
        from .artist_picker import ArtistPickerDialog
        dialog = ArtistPickerDialog(self.service, units, self)
        if dialog.exec() != QDialog.Accepted:
            return
        artist = dialog.organize_artist()
        if not artist:
            return
        self.service.assign_organize_artist(units, artist)
        self._after_action(f"已指定整理艺术家“{artist}”（{len(units)} 项）")

    def _include_unit(self, unit) -> None:
        self.service.set_checked([unit], True, explicit=True)
        self._after_action(f"已勾选纳入本次：{unit.name}")

    def _check_lyrics(self, unit) -> None:
        count = 0
        for att in unit.attachments:
            if att.get("role") == "lyrics" and not att.get("selected"):
                att["selected"] = True
                count += 1
        self.service.save_state()
        self._after_action(
            f"已勾选 {count} 个歌词，将随歌曲移动" if count else "歌词均已勾选")

    def _open_lyrics(self) -> None:
        from .lyrics_dialog import LyricsDialog
        dialog = LyricsDialog(self.service, self, pending_only=True)
        dialog.changed.connect(lambda: self._after_action("歌词关联已更新"))
        dialog.exec()

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _on_tab_changed(self, index: int) -> None:
        if self.tabs.tabText(index) == "最终预览":
            # 进入最终预览时确保计划反映最新修改；重建后按 (unit_id, op) 恢复
            # 用户在预览页的勾选状态，明确取消的条目不得被恢复执行
            previous = list(self.plan.items) if self.plan is not None else []
            self._rebuild()
            if previous:
                preserve_plan_selection(previous, self.plan.items)
                self._fill_preview()
                self._update_summary()

    def _selected_units(self) -> list[Unit]:
        units: list[Unit] = []
        for index in self.reminder_table.selectionModel().selectedRows() \
                if self.reminder_table.selectionModel() else []:
            item = self.reminder_table.item(index.row(), 0)
            unit = self.service.state.unit_by_id(item.data(Qt.UserRole)) if item else None
            if unit is not None:
                units.append(unit)
        return units

    def _assign_common_artist(self) -> None:
        units = self._selected_units()
        if not units:
            QMessageBox.information(self, "提示", "请先在提醒列表中选择条目（可多选）。")
            return
        from .artist_picker import ArtistPickerDialog
        dialog = ArtistPickerDialog(self.service, units, self)
        if dialog.exec() != QDialog.Accepted:
            return
        artist = dialog.organize_artist()
        if not artist:
            return
        self.service.assign_organize_artist(units, artist)
        self.service.save_state()
        self.changed.emit()
        self._rebuild()

    def _run_duplicates(self) -> None:
        """重复检测放后台线程（逐文件 SHA-256 可达数十秒），带进度与取消。"""
        if getattr(self, "_dup_running", False):
            return
        self._dup_running = True
        btn = getattr(self, "btn_dup", None)
        if btn is not None:
            btn.setEnabled(False)
        progress = QProgressDialog("正在比较重复（逐个校验文件内容）…", "取消", 0, 0, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(300)
        progress.setWindowTitle("重复检测")
        progress.canceled.connect(self._cancel_duplicates)
        self._dup_progress = progress

        def task(progress_fn, cancel):
            return self.service.run_duplicate_check(progress=progress_fn, cancel=cancel)

        def close_progress() -> None:
            progress.canceled.disconnect(self._cancel_duplicates)
            progress.close()

        def done(result: dict) -> None:
            self._dup_running = False
            close_progress()
            if btn is not None:
                btn.setEnabled(True)
            self.reminder_note.setText(
                f"重复检测完成：确定重复 {result['exact']} 组。"
                "重复项默认保留各份继续整理。")
            self.changed.emit()
            self._rebuild()

        def failed(message: str) -> None:
            self._dup_running = False
            close_progress()
            if btn is not None:
                btn.setEnabled(True)
            QMessageBox.critical(self, "重复检测失败", message.splitlines()[0])

        def cancelled() -> None:
            self._dup_running = False
            close_progress()
            if btn is not None:
                btn.setEnabled(True)
            self.reminder_note.setText("重复检测已取消，未改动任何数据。")

        self.runner.run(task, on_done=done, on_error=failed,
                        on_cancelled=cancelled,
                        on_progress=lambda msg, _c, _t: progress.setLabelText(msg))

    def _cancel_duplicates(self) -> None:
        worker = getattr(self.runner, "current", None)
        if worker is not None:
            worker.cancel()

    def _locate_checked(self) -> None:
        row = self.checked_table.currentRow()
        if row < 0:
            return
        item = self.checked_table.item(row, 0)
        if item is not None:
            self.locateRequested.emit(item.data(Qt.UserRole))

    def _checked_units_from_table(self) -> list[Unit]:
        units: list[Unit] = []
        for index in self.checked_table.selectionModel().selectedRows() \
                if self.checked_table.selectionModel() else []:
            item = self.checked_table.item(index.row(), 0)
            unit = self.service.state.unit_by_id(item.data(Qt.UserRole)) if item else None
            if unit is not None:
                units.append(unit)
        return units

    def _uncheck_selected(self) -> None:
        units = self._checked_units_from_table()
        if not units:
            QMessageBox.information(self, "提示", "请先选择要取消勾选的行。")
            return
        self.service.set_checked(units, False, explicit=True)
        self.service.save_state()
        self.changed.emit()
        self._rebuild()

    # ------------------------------------------------------------------
    def _on_preview_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0 or self.plan is None:
            return
        plan_item = self.plan.items[item.row()]
        selected = item.checkState() == Qt.Checked
        if plan_item.status == PlanStatus.BLOCKED.value and selected:
            self.preview_table.blockSignals(True)
            item.setCheckState(Qt.Unchecked)
            self.preview_table.blockSignals(False)
            return
        plan_item.selected = selected
        self._update_preview_counts()

    def _on_preview_row_changed(self) -> None:
        if self.plan is None:
            return
        row = self.preview_table.currentRow()
        if not (0 <= row < len(self.plan.items)):
            self.preview_detail.clear()
            return
        item = self.plan.items[row]
        lines = [f"<b>{item.label}</b>", f"操作类型：{item.op_enum.label}",
                 f"状态：{item.status_enum.label}"]
        if item.messages:
            lines.append("提示：" + "；".join(item.messages))
        lyrics = item.extra.get("lyrics") or []
        if lyrics:
            lines.append(f"<br><b>随歌曲的歌词（{len(lyrics)} 个，只移动不改写）：</b>")
            for rec in lyrics:
                lines.append(f"歌词来源：{rec.get('source', '')}")
                lines.append(f"歌词目标：{rec.get('target', '')}"
                             f"（{rec.get('encoding') or '未知编码'}）")
        if item.details:
            lines.append("<br><b>内容：</b>")
            lines.extend(entry for entry in item.details[:60])
        self.preview_detail.setHtml("<br>".join(lines))

    def _set_all(self, state: bool) -> None:
        if self.plan is None:
            return
        self.preview_table.blockSignals(True)
        for row, item in enumerate(self.plan.items):
            if item.status == PlanStatus.BLOCKED.value:
                continue
            item.selected = state
            self.preview_table.item(row, 0).setCheckState(
                Qt.Checked if state else Qt.Unchecked)
        self.preview_table.blockSignals(False)
        self._update_preview_counts()

    def _skip_conflicts(self) -> None:
        if self.plan is None:
            return
        self.preview_table.blockSignals(True)
        for row, item in enumerate(self.plan.items):
            if item.status in (PlanStatus.CONFLICT.value, PlanStatus.BLOCKED.value):
                item.selected = False
                self.preview_table.item(row, 0).setCheckState(Qt.Unchecked)
        self.preview_table.blockSignals(False)
        self._update_preview_counts()

    def _rename_conflicts(self) -> None:
        if self.plan is None:
            return
        taken: set[str] = set()
        for item in self.plan.items:
            if item.status == PlanStatus.CONFLICT.value:
                target = Path(item.target)
                new_target = naming.unique_name(target, taken)
                item.target = str(new_target)
                item.status = PlanStatus.READY.value
                item.messages = [m for m in item.messages if "同名" not in m]
                item.add_message(f"已自动改名为：{new_target.name}")
            taken.add(naming.normalize_key(item.target))
        self.plan.recalculate_conflicts()
        self._fill_preview()
        self._update_summary()

    def _start(self) -> None:
        plan = self.plan
        if plan is None:
            return
        table = self.preview_table
        table.blockSignals(True)
        for row, item in enumerate(plan.items):
            check = table.item(row, 0)
            if check is not None:
                item.selected = check.checkState() == Qt.Checked
        table.blockSignals(False)
        for item in plan.items:
            if item.selected and item.status != PlanStatus.READY.value:
                item.selected = False
        if not plan.ready_items:
            QMessageBox.information(self, "提示", "没有可执行的项目。")
            return
        self.accept()
