"""批量指定整理艺术家：选择面板（搜索已有艺术家文件夹 / 输入新名称）。

“整理艺术家”只决定目标目录归属（目标根/艺术家/…），不写入音频标签，
也不修改歌曲自身的艺术家字段。确认前展示选中数量、名称与目标位置预览。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QTextBrowser, QVBoxLayout)


class ArtistPickerDialog(QDialog):
    """选择/输入整理艺术家；accept() 后经 organize_artist 取值。"""

    def __init__(self, service, units, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.units = units
        self.setWindowTitle("指定整理艺术家")
        self.resize(720, 560)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(10)

        albums = sum(1 for u in units if u.is_album)
        tracks = len(units) - albums
        scope = QLabel(f"将应用到 {len(units)} 个条目（专辑整夹 {albums}，散曲 {tracks}）。"
                       "整理艺术家只决定目标目录归属，不写入音频标签。")
        scope.setObjectName("Muted")
        scope.setWordWrap(True)
        layout.addWidget(scope)

        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索目标目录下已有的艺术家文件夹…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._refresh_list)
        search_row.addWidget(self.search_edit, 1)
        layout.addLayout(search_row)

        self.list_widget = QListWidget()
        self.list_widget.itemActivated.connect(lambda _item: self.accept())
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list_widget, 2)

        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("艺术家名称"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("输入名称，或点击上方已有的文件夹名")
        self.name_edit.textChanged.connect(self._update_preview)
        input_row.addWidget(self.name_edit, 1)
        layout.addLayout(input_row)

        self.preview = QTextBrowser()
        self.preview.setMinimumHeight(120)
        layout.addWidget(self.preview, 1)

        buttons = QHBoxLayout()
        hint = QLabel("同名文件夹已存在时放入该文件夹；不存在时预览中注明将新建。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        buttons.addWidget(hint, 1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        confirm = QPushButton("指定整理艺术家")
        confirm.setProperty("kind", "primary")
        confirm.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)

        self._refresh_list()
        # 预填：仅当所选条目的建议完全一致时才预填；
        # 多个不同建议时绝不按出现次数静默覆盖整批（§重构规则九.4）
        distinct: list[str] = []
        for unit in units:
            suggestion = (unit.artist_suggestion or "").strip()
            if suggestion and suggestion not in distinct:
                distinct.append(suggestion)
        if len(distinct) == 1:
            self.name_edit.setText(distinct[0])
        elif len(distinct) > 1:
            self.name_edit.setPlaceholderText(
                "所选条目包含多个不同建议，未预填；可输入统一名称或逐项采用")
            self.preview.setPlainText(
                "⚠ 所选条目包含多个不同的整理艺术家建议：\n· "
                + "\n· ".join(distinct[:8])
                + "\n\n不会按多数建议静默覆盖整批。请输入统一名称，"
                  "或取消后用整理中心的“分别采用各自建议”。")

    # ------------------------------------------------------------------
    def _refresh_list(self) -> None:
        self.list_widget.clear()
        keyword = self.search_edit.text().strip()
        existing = self.service.existing_artist_dirs(keyword)
        if not self.service.settings.target_dir:
            self.list_widget.addItem("（尚未选择目标目录，只能直接输入名称）")
            return
        if not existing:
            self.list_widget.addItem("（目标目录下暂无匹配的艺术家文件夹）")
            return
        for info in existing:
            item = QListWidgetItem("📁 " + info["name"])
            item.setData(Qt.UserRole, info["name"])
            item.setToolTip(info["path"])
            self.list_widget.addItem(item)

    def _on_item_clicked(self, item) -> None:
        name = item.data(Qt.UserRole)
        if name:
            self.name_edit.setText(name)

    # ------------------------------------------------------------------
    def organize_artist(self) -> str:
        return self.name_edit.text().strip()

    def _update_preview(self) -> None:
        try:
            self._render_preview()
        except Exception as exc:  # noqa: BLE001 - 预览异常必须可见，不允许静默失败
            self.preview.setPlainText(
                f"⚠ 生成预览时出错：{type(exc).__name__}: {exc}\n"
                "请截图反馈；输入框内容仍可用于确认。")

    def _render_preview(self) -> None:
        artist = self.organize_artist()
        self.preview.clear()
        if not artist:
            self.preview.setPlainText("输入名称后这里会显示确认前的信息。")
            return
        target_root = self.service.settings.target_dir or "（未选择目标目录）"
        from ..domain import naming
        from pathlib import Path
        base = Path(target_root) / naming.sanitize_artist(artist)
        exists = Path(target_root).is_dir() and base.is_dir()
        lines = [f"整理艺术家：{artist}",
                 f"目标位置：{base}" + ("（已存在）" if exists else "（将新建）"),
                 f"应用数量：{len(self.units)} 个条目"]
        # 冲突候选提示（来自合辑/不一致数据的 low 置信建议）
        candidates: list[str] = []
        for unit in self.units:
            for name in (unit.artist_candidates or []):
                if name not in candidates:
                    candidates.append(name)
        if candidates:
            lines.append("⚠ 该批条目含多个不同艺术家值，疑似合辑，候选："
                         + "、".join(candidates[:6]))
        lines.append("各条目预览：")
        for unit in self.units[:30]:
            target = self.service.preview_artist_target(artist, unit)
            confidence = {"high": "较可靠", "medium": "建议复核",
                          "low": "不确定"}.get(unit.artist_suggestion_confidence, "")
            mark = f"（{confidence}）" if confidence and unit.is_album else ""
            lines.append(f"· {unit.name}{mark} → {target}")
        if len(self.units) > 30:
            lines.append(f"…共 {len(self.units)} 项")
        self.preview.setPlainText("\n".join(lines))
