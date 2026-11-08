"""歌词预览与关联对话框。

- 预览模式：查看某个歌词文件的内容、编码与结构，可切换编码预览（只读）。
- 待关联模式：列出未确定归属的歌词，显示匹配依据与候选曲目，由用户手动选择。

只读取与显示：不改写歌词、不转码、不写音频标签。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout,
                               QWidget)

from ..domain.models import Unit


class LyricsDialog(QDialog):
    changed = Signal()

    def __init__(self, service, parent=None, *, path: str = "",
                 unit: Unit | None = None, pending_only: bool = True,
                 prefer_unit_id: str = "") -> None:
        super().__init__(parent)
        self.service = service
        self.unit = unit
        self.current_path = path
        self._prefer_unit_id = prefer_unit_id
        self._preview_evidence: list[str] = []
        self._candidate_keys: list[str] = []
        self.setWindowTitle("外置歌词")
        self.resize(980, 640)
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)
        title = QLabel("歌词只看不改：不修改内容、不转码、不写入音频标签")
        title.setObjectName("Title")
        layout.addWidget(title)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, 1)

        # 左侧：待关联歌词列表
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(QLabel("待关联歌词"))
        self.pending_list = QListWidget()
        self.pending_list.setMinimumWidth(260)
        self.pending_list.currentItemChanged.connect(self._on_pending_selected)
        left_layout.addWidget(self.pending_list, 1)
        self.pending_hint = QLabel("")
        self.pending_hint.setObjectName("Muted")
        self.pending_hint.setWordWrap(True)
        left_layout.addWidget(self.pending_hint)
        splitter.addWidget(left)

        # 右侧：预览 + 候选
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        head = QHBoxLayout()
        self.name_label = QLabel("（未选择歌词）")
        self.name_label.setObjectName("Section")
        head.addWidget(self.name_label, 1)
        head.addWidget(QLabel("编码"))
        self.encoding_box = QComboBox()
        self.encoding_box.setMinimumWidth(170)
        self.encoding_box.activated.connect(lambda _i: self._reload_preview())
        head.addWidget(self.encoding_box)
        right_layout.addLayout(head)

        self.structure_label = QLabel("")
        self.structure_label.setObjectName("Muted")
        self.structure_label.setWordWrap(True)
        right_layout.addWidget(self.structure_label)

        self.preview = QTextBrowser()
        self.preview.setMinimumHeight(180)
        right_layout.addWidget(self.preview, 3)

        self.evidence_label = QLabel("")
        self.evidence_label.setObjectName("Muted")
        self.evidence_label.setWordWrap(True)
        right_layout.addWidget(self.evidence_label)

        right_layout.addWidget(QLabel("候选曲目（选择一行后点“关联”）"))
        self.candidates = QTableWidget(0, 4)
        self.candidates.setHorizontalHeaderLabels(["曲目", "艺术家", "匹配度", "匹配依据"])
        self.candidates.verticalHeader().setVisible(False)
        self.candidates.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.candidates.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.candidates.setSelectionMode(QAbstractItemView.SingleSelection)
        self.candidates.horizontalHeader().setStretchLastSection(True)
        self.candidates.setColumnWidth(0, 220)
        self.candidates.setColumnWidth(1, 130)
        self.candidates.setColumnWidth(2, 70)
        self.candidates.setMinimumHeight(120)
        right_layout.addWidget(self.candidates, 2)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        tools = QHBoxLayout()
        self.btn_link = QPushButton("关联到所选曲目")
        self.btn_link.setProperty("kind", "primary")
        self.btn_link.clicked.connect(self._link_selected)
        self.btn_unlink = QPushButton("取消关联")
        self.btn_unlink.setProperty("kind", "ghost")
        self.btn_unlink.clicked.connect(self._unlink)
        self.btn_relink = QPushButton("重新匹配")
        self.btn_relink.setProperty("kind", "ghost")
        self.btn_relink.clicked.connect(self._reload_candidates)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        tools.addWidget(self.btn_link)
        tools.addWidget(self.btn_unlink)
        tools.addWidget(self.btn_relink)
        tools.addStretch(1)
        tools.addWidget(close)
        layout.addLayout(tools)

        self._pending_mode = pending_only
        if pending_only:
            self._fill_pending()
        else:
            self.pending_list.setVisible(False)
            splitter.setSizes([0, 980])
        if path:
            self._show_file(path)
        self._update_buttons()

    # ------------------------------------------------------------------
    def _fill_pending(self) -> None:
        self.pending_list.clear()
        pending = self.service.pending_lyrics()
        for unit in pending:
            att = unit.attachments[0] if unit.attachments else {}
            desc = att.get("lyrics_encoding", "") or ""
            if att.get("has_lrc_timestamps"):
                desc += "｜LRC"
            item = QListWidgetItem(f"{unit.name}\n{desc}".strip("｜"))
            item.setData(Qt.UserRole, unit.path)
            item.setToolTip(unit.path + ("\n" + "；".join(unit.messages)
                                         if unit.messages else ""))
            self.pending_list.addItem(item)
        if pending:
            self.pending_hint.setText(f"共 {len(pending)} 个歌词待关联：多个候选、"
                                      "多个语言版本或依据不足时不会自动移动。")
            self.pending_list.setCurrentRow(0)
        else:
            self.pending_hint.setText("当前没有待关联歌词。")
            self.name_label.setText("（没有待关联歌词）")

    def _on_pending_selected(self, current: QListWidgetItem | None,
                             _previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        self.unit = None
        self._show_file(current.data(Qt.UserRole))

    # ------------------------------------------------------------------
    def _show_file(self, path: str) -> None:
        self.current_path = path
        info = self.service.lyrics_preview(path)
        self.name_label.setText(Path(path).name)
        encodings = info.get("encodings") or []
        self.encoding_box.blockSignals(True)
        self.encoding_box.clear()
        from ..scan.lyrics_recognizer import ENCODING_LABELS
        for enc in encodings:
            self.encoding_box.addItem(ENCODING_LABELS.get(enc, enc), enc)
        current = info.get("encoding", "")
        index = self.encoding_box.findData(current)
        if index >= 0:
            self.encoding_box.setCurrentIndex(index)
        self.encoding_box.blockSignals(False)
        self.preview.setPlainText(info.get("text") or f"（无法预览：{info.get('error', '')}）")
        self.structure_label.setText(info.get("structure", ""))
        self._preview_evidence = list(info.get("evidence") or [])
        self._reload_candidates(keep_evidence=True)
        self._update_buttons()

    def _reload_preview(self) -> None:
        if not self.current_path:
            return
        enc = self.encoding_box.currentData()
        info = self.service.lyrics_preview(self.current_path, encoding=enc)
        self.preview.setPlainText(info.get("text") or f"（无法预览：{info.get('error', '')}）")
        note = f"当前编码：{enc}"
        if enc and enc != info.get("encoding"):
            pass
        self.structure_label.setText((info.get("structure", "") or "") + f"｜{note}")

    def _reload_candidates(self, keep_evidence: bool = False) -> None:
        if not keep_evidence:
            self._preview_evidence = []
        self.candidates.setRowCount(0)
        if not self.current_path:
            return
        rows = self.service.lyrics_candidates(self.current_path)
        self._candidate_keys: list[str] = []
        for row in rows:
            index = self.candidates.rowCount()
            self.candidates.insertRow(index)
            title = row["title"] + ("（CUE 虚拟曲目）" if row.get("virtual") else "")
            self.candidates.setItem(index, 0, QTableWidgetItem(title))
            self.candidates.setItem(index, 1, QTableWidgetItem(row.get("artist", "")))
            self.candidates.setItem(index, 2, QTableWidgetItem(f"{row['score']:.0f}"))
            evidence = "；".join(row.get("evidence") or [])
            cell = QTableWidgetItem(evidence)
            cell.setToolTip(evidence)
            self.candidates.setItem(index, 3, cell)
            self._candidate_keys.append(row["key"])
        if self.candidates.rowCount() and self.candidates.currentRow() < 0:
            self.candidates.selectRow(0)
        if self._prefer_unit_id and self._candidate_keys:
            if self._prefer_unit_id in self._candidate_keys:
                self.candidates.selectRow(
                    self._candidate_keys.index(self._prefer_unit_id))
        evidence_text = "；".join(self._preview_evidence[:4])
        if evidence_text:
            self.evidence_label.setText("识别依据：" + evidence_text)
        elif not rows:
            self.evidence_label.setText(
                "识别依据：" + ("暂无可对应的曲目（可先在主界面添加来源并扫描）"))

    # ------------------------------------------------------------------
    def _selected_key(self) -> str:
        row = self.candidates.currentRow()
        if row < 0 or row >= len(getattr(self, "_candidate_keys", [])):
            return ""
        return self._candidate_keys[row]

    def _link_selected(self) -> None:
        key = self._selected_key()
        if not key or not self.current_path:
            return
        unit, hint = self.service.resolve_lyrics_target(key)
        if unit is None:
            return
        self.service.link_lyrics(unit, self.current_path, selected=True)
        for att in unit.attachments:
            if att.get("path") == self.current_path:
                att["owner_hint"] = hint
                att["selected"] = True
        self.service.save_state()
        self.changed.emit()
        self._fill_pending()
        self._update_buttons()

    def _unlink(self) -> None:
        if not self.current_path:
            return
        for unit in list(self.service.state.units):
            paths = [a.get("path") for a in unit.attachments if a.get("role") == "lyrics"]
            if any(p and Path(p) == Path(self.current_path) for p in paths):
                self.service.unlink_lyrics(unit, self.current_path)
        self.changed.emit()
        self._fill_pending()
        self._update_buttons()

    def _update_buttons(self) -> None:
        has_file = bool(self.current_path)
        self.btn_link.setEnabled(has_file and self.candidates.rowCount() > 0)
        self.btn_unlink.setEnabled(has_file)
        self.btn_relink.setEnabled(has_file)
        self.encoding_box.setEnabled(has_file)
