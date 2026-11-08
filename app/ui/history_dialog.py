"""历史与撤销：查看多次操作记录，撤销前再次预览并检查后续依赖。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout,
                               QHeaderView, QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QTextBrowser, QVBoxLayout)

from .workers import TaskRunner

OP_LABELS = {
    "album_move": "整夹", "track_move": "散曲", "attachment_move": "附件",
    "cue_fix": "CUE 引用修正", "archive_archive": "原包归档",
    "duplicate_move": "重复项", "undo": "撤销",
}


class HistoryDialog(QDialog):
    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("操作历史与撤销")
        self.resize(1080, 640)
        self.setModal(True)
        self.current_undo_plan = None
        self.runner = TaskRunner(self)      # 撤销等文件操作在后台线程执行
        self._undo_running = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(10)

        title = QLabel("操作历史")
        title.setObjectName("Title")
        layout.addWidget(title)
        hint = QLabel("撤销前会再次校验：目标是否被后续修改、原位置是否已被占用、是否有后续操作依赖。")
        hint.setObjectName("Muted")
        layout.addWidget(hint)

        body = QHBoxLayout()
        self.run_list = QListWidget()
        self.run_list.setMinimumWidth(320)
        self.run_list.currentRowChanged.connect(self._on_run_changed)
        body.addWidget(self.run_list, 1)

        right = QVBoxLayout()
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["操作", "状态", "来源", "目标", "说明"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 260)
        self.table.setColumnWidth(3, 300)
        right.addWidget(self.table, 2)

        self.undo_preview = QTextBrowser()
        self.undo_preview.setMinimumHeight(140)
        right.addWidget(self.undo_preview, 1)

        actions = QHBoxLayout()
        self.btn_preview_undo = QPushButton("检查撤销可行性")
        self.btn_preview_undo.clicked.connect(self._preview_undo)
        self.btn_undo = QPushButton("执行撤销")
        self.btn_undo.setProperty("kind", "primary")
        self.btn_undo.setEnabled(False)
        self.btn_undo.clicked.connect(self._apply_undo)
        self.btn_open_dir = QPushButton("打开记录目录")
        self.btn_open_dir.setProperty("kind", "ghost")
        self.btn_open_dir.clicked.connect(self._open_dir)
        actions.addWidget(self.btn_preview_undo)
        actions.addWidget(self.btn_undo)
        actions.addWidget(self.btn_open_dir)
        actions.addStretch(1)
        right.addLayout(actions)
        body.addLayout(right, 3)
        layout.addLayout(body, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.runs = []
        self._load_runs()

    # ------------------------------------------------------------------
    def _load_runs(self) -> None:
        self.runs = self.service.history_runs()
        self.run_list.clear()
        if not self.runs:
            self.run_list.addItem("（还没有操作记录）")
            self.btn_preview_undo.setEnabled(False)
            return
        for run in self.runs:
            item = QListWidgetItem(run.label)
            if run.status in ("running", "interrupted"):
                item.setForeground(Qt.darkYellow)
            if run.undone_by:
                item.setForeground(Qt.gray)
                item.setText(run.label + "　已撤销")
            self.run_list.addItem(item)
        self.run_list.setCurrentRow(0)

    def _current_run(self):
        row = self.run_list.currentRow()
        if 0 <= row < len(self.runs):
            return self.runs[row]
        return None

    def _on_run_changed(self, _row: int) -> None:
        run = self._current_run()
        self.table.setRowCount(0)
        self.undo_preview.clear()
        self.btn_undo.setEnabled(False)
        self.current_undo_plan = None
        if run is None:
            return
        self.table.setRowCount(len(run.ops))
        for row, op in enumerate(run.ops):
            self.table.setItem(row, 0, QTableWidgetItem(OP_LABELS.get(op.op, op.op)))
            self.table.setItem(row, 1, QTableWidgetItem(op.status))
            src = QTableWidgetItem(op.source)
            src.setToolTip(op.source)
            self.table.setItem(row, 2, src)
            tgt = QTableWidgetItem(op.target)
            tgt.setToolTip(op.target)
            self.table.setItem(row, 3, tgt)
            self.table.setItem(row, 4, QTableWidgetItem(op.message))
        info = [f"记录：{run.run_id}", f"时间：{run.started_at} → {run.finished_at or '未完成'}",
                f"方式：{'复制' if run.mode == 'copy' else '移动'}",
                f"状态：{run.status}", f"统计：{run.counts}"]
        lyrics_rows = [(op, rec) for op in run.ops for rec in (op.lyrics or [])]
        if lyrics_rows:
            info.append("")
            info.append("歌词（随歌曲移动/复制，内容与编码未改动）：")
            for _op, rec in lyrics_rows[:20]:
                info.append(
                    f"· {Path(rec.get('source', '')).name} → {rec.get('target', '')}"
                    f"（{rec.get('mode', '')}｜{rec.get('encoding') or '编码未识别'}）")
            info.append("撤销该记录时，歌词会一并回写到原来源位置。")
        notes = list(getattr(run, "notes", []) or [])
        if notes:
            info.append("")
            info.append("未随本次执行移动的歌词：")
            info.extend("· " + note for note in notes[:10])
        policy_notes = []
        if False:
            info.append("")
            info.append("本次实际采用的整理策略（快照）：")
            info.extend("· " + note for note in policy_notes[:6])
        if run.status in ("running", "interrupted"):
            info.append("")
            info.append("该记录未正常结束，下面是按日志核对的实际状态：")
            for row in self.service.history.reconcile(run)[:30]:
                info.append(f"· {row.get('label')}｜来源存在={row.get('source_exists')}"
                            f"｜目标存在={row.get('target_exists')}")
        self.undo_preview.setPlainText("\n".join(info))

    def _preview_undo(self) -> None:
        run = self._current_run()
        if run is None:
            return
        plan = self.service.build_undo(run)
        self.current_undo_plan = plan
        counts = plan.counts()
        lines = [f"可安全撤销 {counts.get('ready', 0)} 项｜冲突 {counts.get('conflict', 0)} 项"
                 f"｜阻止 {counts.get('blocked', 0)} 项｜跳过 {counts.get('skipped', 0)} 项"]
        if plan.messages:
            lines.append("")
            lines.extend("提示：" + m for m in plan.messages)
        lines.append("")
        for action in plan.actions[:80]:
            mark = {"ready": "✓", "conflict": "!", "blocked": "×", "skipped": "–"}.get(
                action.status, "·")
            lines.append(f"{mark} {action.message}")
            if action.keep_files:
                lines.append("    保留：" + "、".join(Path(p).name for p in action.keep_files[:4]))
        # 将清理的空目录：按操作日志分类（依据记录，不做推测性删除）
        will_try: list[str] = []
        wont: list[str] = []
        for action in plan.actions:
            for path in action.dirs_delete:
                will_try.append(path)
            for info in action.dirs_kept:
                wont.append(f"{info.get('path', '')}（{info.get('reason', '')}）")
        if will_try or wont:
            lines.append("")
            lines.append("将清理的空目录（本次操作创建；执行时为空才删除）：")
            lines.extend("· " + p for p in will_try[:40])
            if len(will_try) > 40:
                lines.append(f"· …共 {len(will_try)} 个")
            if wont:
                lines.append(f"不会删除的目录（{len(wont)} 个）：")
                lines.extend("· " + w for w in wont[:20])
                if len(wont) > 20:
                    lines.append(f"· …共 {len(wont)} 个")
        self.undo_preview.setPlainText("\n".join(lines))
        self.btn_undo.setEnabled(counts.get("ready", 0) > 0)

    def _apply_undo(self) -> None:
        if self.current_undo_plan is None or getattr(self, "_undo_running", False):
            return
        counts = self.current_undo_plan.counts()
        confirm = QMessageBox.question(
            self, "确认撤销",
            f"将撤销 {counts.get('ready', 0)} 项操作，冲突项会自动跳过。是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        # 文件级移动/删除可达分钟级：放后台线程执行，带进度与取消
        self._undo_running = True
        self.btn_undo.setEnabled(False)
        plan = self.current_undo_plan
        self.undo_preview.setPlainText("撤销执行中…（可在下方进度查看；目标未就绪项自动跳过）")

        def task(progress_fn, cancel):
            return self.service.apply_undo(plan, progress=progress_fn, cancel=cancel)

        def done(record) -> None:
            self._undo_running = False
            self.btn_undo.setEnabled(True)
            lines = [f"撤销记录：{record.run_id}", f"统计：{record.counts}"]
            if record.notes:
                lines.append("")
                lines.append("目录清理结果：")
                lines.extend("· " + note for note in record.notes[:30])
                if len(record.notes) > 30:
                    lines.append(f"· …共 {len(record.notes)} 条")
            QMessageBox.information(self, "撤销完成", "\n".join(lines))
            self.current_undo_plan = None
            self._load_runs()

        def failed(message: str) -> None:
            self._undo_running = False
            self.btn_undo.setEnabled(True)
            self.undo_preview.setPlainText(f"撤销失败：{message}\n\n"
                                           "记录保持原样，可重试或手动处理。")
            QMessageBox.critical(self, "撤销失败", message.splitlines()[0])

        def cancelled() -> None:
            self._undo_running = False
            self.btn_undo.setEnabled(True)
            self.undo_preview.setPlainText(
                "撤销已取消：已完成的安全点之前的项目保留，未执行的部分未做任何改动。")
            self._load_runs()

        def on_progress(message, current, total) -> None:
            self.undo_preview.setPlainText(f"撤销执行中… [{current}/{total}]\n{message}")

        self.runner.run(task, on_done=done, on_error=failed,
                        on_cancelled=cancelled, on_progress=on_progress)

    def _open_dir(self) -> None:
        run = self._current_run()
        if run is None:
            return
        import os
        os.startfile(run.run_dir)  # noqa: S606 - 打开本地记录目录
