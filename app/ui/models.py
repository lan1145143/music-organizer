"""表格与树模型：整理单元列表、来源目录树。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRect, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPen, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QHeaderView, QStyle, QStyleOptionButton, QStyleOptionViewItem,
                               QStyledItemDelegate)

from ..domain.models import Unit, UnitStatus

COL_CHECK = 0
COL_KIND = 1
COL_NAME = 2
COL_ARTIST = 3
COL_ALBUM = 4
COL_YEAR = 5
COL_TRACKS = 6
COL_SIZE = 7
COL_STATUS = 8
COL_TARGET = 9

HEADERS = ["", "类型", "名称 / 源目录", "艺术家", "专辑", "年份", "曲目", "大小", "状态", "目标预览"]

STATUS_ROLE = Qt.UserRole + 2   # 状态胶囊委托读取原始状态值

# 状态 → 语气色（与 theme.TONES 对应；胶囊委托据此取色）
STATUS_TONES = {
    UnitStatus.READY.value: "green",
    UnitStatus.MISSING.value: "red",
    UnitStatus.BLOCKED.value: "red",
    UnitStatus.NEEDS_BOUNDARY.value: "amber",
    UnitStatus.NEEDS_INFO.value: "amber",
    UnitStatus.INCOMPLETE.value: "amber",
    UnitStatus.NEEDS_LINK.value: "amber",
    UnitStatus.MANUAL.value: "indigo",
    UnitStatus.CHILD.value: "gray",
    UnitStatus.DONE.value: "gray",
}

MONO_FONT = None  # 惰性初始化的等宽字体（年份/曲目/大小列）


def mono_font() -> QFont:
    global MONO_FONT
    if MONO_FONT is None:
        MONO_FONT = QFont("Consolas", 8.5)
    return MONO_FONT


def human_size(size: int) -> str:
    if size <= 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


class CheckDelegate(QStyledItemDelegate):
    """自绘勾选框：统一 Win11 风格，不依赖平台样式。"""

    def __init__(self, accent_provider, parent=None) -> None:
        super().__init__(parent)
        self.accent_provider = accent_provider

    @staticmethod
    def _check_rect(option: QStyleOptionViewItem) -> QRect:
        """勾选框的绘制矩形（单元格正中 16px）——同时作为点击命中区。"""
        size = 16
        return QRect(option.rect.center().x() - size // 2,
                     option.rect.center().y() - size // 2, size, size)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        state = index.data(Qt.CheckStateRole)
        if state is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        checked = Qt.CheckState(state) == Qt.Checked
        rect = self._check_rect(option)
        accent = QColor(self.accent_provider() or "#3B6EA5")
        if option.state & QStyle.State_MouseOver:
            painter.setPen(QPen(QColor(150, 150, 150), 1))
        else:
            painter.setPen(QPen(QColor(170, 170, 170), 1))
        if checked:
            painter.setBrush(accent)
            painter.setPen(Qt.NoPen)
        else:
            painter.setBrush(option.palette.base())
        painter.drawRoundedRect(rect, 4, 4)
        if checked:
            painter.setPen(QPen(QColor("#FFFFFF"), 2))
            painter.drawLine(rect.left() + 3, rect.top() + 8, rect.left() + 7, rect.top() + 12)
            painter.drawLine(rect.left() + 7, rect.top() + 12, rect.left() + 13, rect.top() + 4)
        painter.restore()

    def editorEvent(self, event, model, option: QStyleOptionViewItem, index: QModelIndex) -> bool:
        """点击命中以自绘矩形为准。

        样式默认的勾选切换只认左对齐的命中矩形，与单元格正中的绘制
        位置错位——瞄准可见勾选框点击约半数无效。落在绘制框内的左键
        释放直接翻转勾选，其余事件交回默认处理。
        """
        from PySide6.QtCore import QEvent
        if index.data(Qt.CheckStateRole) is None:
            return super().editorEvent(event, model, option, index)
        if (event.type() == QEvent.MouseButtonRelease
                and getattr(event, "button", lambda: None)() == Qt.LeftButton):
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            if self._check_rect(option).contains(pos):
                current = Qt.CheckState(index.data(Qt.CheckStateRole)) == Qt.Checked
                return model.setData(index, Qt.Unchecked if current else Qt.Checked,
                                     Qt.CheckStateRole)
        return super().editorEvent(event, model, option, index)


class CheckHeaderView(QHeaderView):
    """表头复选框：第一列直接承担「全选 / 取消当前视图」，点击不触发排序。"""

    toggled = Signal(bool)

    def __init__(self, state_provider, parent=None) -> None:
        super().__init__(Qt.Horizontal, parent)
        self.state_provider = state_provider  # () -> Qt.CheckState
        self.setSectionsClickable(True)

    def _hit_check(self, pos: int) -> bool:
        return self.logicalIndexAt(pos) == COL_CHECK

    def paintSection(self, painter: QPainter, rect: QRect, logicalIndex: int) -> None:
        super().paintSection(painter, rect, logicalIndex)
        if logicalIndex != COL_CHECK:
            return
        painter.save()
        state = self.state_provider()
        opt = QStyleOptionButton()
        opt.state = QStyle.State_Enabled | QStyle.State_Raised
        if state == Qt.Checked:
            opt.state |= QStyle.State_On
        elif state == Qt.PartiallyChecked:
            opt.state |= QStyle.State_NoChange
        else:
            opt.state |= QStyle.State_Off
        size = 15
        opt.rect = QRect(rect.center().x() - size // 2, rect.center().y() - size // 2,
                         size, size)
        self.style().drawControl(QStyle.CE_CheckBox, opt, painter)
        painter.restore()

    def mousePressEvent(self, event) -> None:
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        if self._hit_check(pos.x()) and event.button() == Qt.LeftButton:
            self.toggled.emit(self.state_provider() != Qt.Checked)
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        if self._hit_check(pos.x()):
            return  # 点击已在 press 阶段消费，避免触发排序
        super().mouseReleaseEvent(event)


class StatusDelegate(QStyledItemDelegate):
    """状态胶囊委托：圆点 + 圆角底色徽章（就绪=翠绿，失联/冲突=红…）。"""

    def __init__(self, tones_provider, parent=None) -> None:
        super().__init__(parent)
        self.tones_provider = tones_provider  # () -> dict[tone -> (bg, fg, border)]

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        status = index.data(STATUS_ROLE)
        if not status:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        tones = self.tones_provider()
        bg, fg, _border = tones.get(STATUS_TONES.get(status, "gray"), tones["gray"])
        text = str(index.data(Qt.DisplayRole) or "")
        font = QFont(option.font)
        font.setPointSizeF(8.0)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        dot = 6
        gap = 5
        pad_x = 9
        pill_w = pad_x * 2 + dot + gap + metrics.horizontalAdvance(text)
        pill_h = 19
        rect = QRect(option.rect.center().x() - pill_w // 2,
                     option.rect.center().y() - pill_h // 2, pill_w, pill_h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(rect, pill_h // 2, pill_h // 2)
        dot_rect = QRect(rect.left() + pad_x, rect.center().y() - dot // 2, dot, dot)
        painter.setBrush(QColor(fg))
        painter.drawEllipse(dot_rect)
        painter.setPen(QColor(fg))
        text_rect = QRect(dot_rect.right() + gap + 1, rect.top(),
                          rect.right() - dot_rect.right() - gap, rect.height())
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, text)
        painter.restore()


class UnitTableModel(QAbstractTableModel):
    checkChanged = Signal(str, bool)          # unit_id, checked
    sortRequested = Signal(int, Qt.SortOrder)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._all: list[Unit] = []
        self._rows: list[Unit] = []
        self.search = ""
        self.scope_path = ""
        # 分组视图："" 全部 | ready 可整理 | needs 需处理 | side 附属（已完成/子项）
        # group_provider 由主窗口注入（统一状态模块），分组只是同一数据的视图
        self.group_filter = ""
        self.group_provider: Callable[[Unit], str] | None = None
        self.target_provider: Callable[[Unit], str] | None = None
        self._sort_column = -1
        self._sort_order = Qt.AscendingOrder

    # ------------------------------------------------------------------
    def set_units(self, units: list[Unit]) -> None:
        self._all = list(units)
        self.reload()

    def reload(self) -> None:
        self.beginResetModel()
        self._rows = [u for u in self._all if self._matches(u)]
        self._apply_sort()
        self.endResetModel()

    def refresh_all(self) -> None:
        if self._rows:
            self.dataChanged.emit(self.index(0, 0),
                                 self.index(len(self._rows) - 1, len(HEADERS) - 1))
        self.reload()

    def unit_at(self, row: int) -> Unit | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def row_of(self, unit: Unit) -> int:
        try:
            return self._rows.index(unit)
        except ValueError:
            return -1

    def visible_units(self) -> list[Unit]:
        return list(self._rows)

    # ------------------------------------------------------------------
    def _matches(self, unit: Unit) -> bool:
        if self.group_filter and self.group_provider is not None:
            if self.group_provider(unit) != self.group_filter:
                return False
        if self.scope_path:
            scope = os.path.normcase(os.path.normpath(self.scope_path))
            target = os.path.normcase(os.path.normpath(
                unit.boundary_path if unit.is_album else unit.path))
            if not (target == scope or target.startswith(scope + os.sep)
                    or os.path.normcase(os.path.dirname(target)) == scope):
                return False
        if self.search:
            needle = self.search.casefold()
            haystack = " ".join([
                unit.name, unit.title, unit.artist, unit.album, unit.album_artist,
                unit.year, unit.path, unit.messages[0] if unit.messages else "",
            ]).casefold()
            if needle not in haystack:
                return False
        return True

    def _apply_sort(self) -> None:
        if self._sort_column < 0:
            return
        reverse = self._sort_order == Qt.DescendingOrder

        def key(unit: Unit):
            if self._sort_column == COL_KIND:
                return (unit.kind,)
            if self._sort_column == COL_NAME:
                return (unit.name.casefold(),)
            if self._sort_column == COL_ARTIST:
                return ((unit.album_artist or unit.artist or "").casefold(),)
            if self._sort_column == COL_ALBUM:
                return ((unit.album or "").casefold(),)
            if self._sort_column == COL_YEAR:
                return (unit.year or "9999",)
            if self._sort_column == COL_TRACKS:
                return (unit.track or 0, len(unit.audio_files))
            if self._sort_column == COL_SIZE:
                return (unit.size,)
            if self._sort_column == COL_STATUS:
                return (unit.status,)
            return (unit.name.casefold(),)

        self._rows.sort(key=key, reverse=reverse)

    # ------------------------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return HEADERS[section]
        if orientation == Qt.Horizontal and role == Qt.TextAlignmentRole:
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == COL_CHECK:
            return base | Qt.ItemIsUserCheckable
        return base

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        unit = self._rows[index.row()]
        column = index.column()
        if role == Qt.CheckStateRole and column == COL_CHECK:
            return Qt.Checked if unit.checked else Qt.Unchecked
        if role == Qt.DisplayRole:
            if column == COL_KIND:
                return unit.kind_enum.label
            if column == COL_NAME:
                return self._display_name(unit)
            if column == COL_ARTIST:
                return unit.album_artist or unit.artist
            if column == COL_ALBUM:
                return unit.album
            if column == COL_YEAR:
                return unit.year
            if column == COL_TRACKS:
                if unit.is_album:
                    return f"{len(unit.audio_files)} 曲" if unit.audio_files else ""
                return str(unit.track) if unit.track else ""
            if column == COL_SIZE:
                return human_size(unit.size)
            if column == COL_STATUS:
                return unit.status_enum.label
            if column == COL_TARGET:
                if self.target_provider is not None:
                    try:
                        return self.target_provider(unit)
                    except Exception:  # noqa: BLE001 - 预览失败不影响列表
                        return ""
                return unit.last_target
        if role == Qt.ForegroundRole:
            if column in (COL_YEAR, COL_TRACKS, COL_SIZE):
                return QColor("#94A3B8") if unit.status_enum is not UnitStatus.READY else None
            if column == COL_TARGET:
                return QColor("#8A8F98")
            if column == COL_NAME and unit.status_enum.is_pending:
                return QColor("#8A6A2A")
        if role == Qt.ToolTipRole:
            parts = [unit.path]
            if unit.messages:
                parts.append("；".join(unit.messages))
            if unit.is_album and unit.evidence:
                parts.append("识别依据：" + "；".join(unit.evidence[:3]))
            return "\n".join(parts)
        if role == Qt.TextAlignmentRole:
            if column == COL_CHECK:
                return int(Qt.AlignCenter)
            if column in (COL_YEAR, COL_TRACKS, COL_SIZE):
                return int(Qt.AlignLeft | Qt.AlignVCenter)
        if role == Qt.FontRole:
            if column in (COL_YEAR, COL_TRACKS, COL_SIZE):
                return mono_font()
            if unit.is_album and column in (COL_NAME, COL_ALBUM):
                from PySide6.QtGui import QFont
                font = QFont()
                font.setBold(False)
                return font
        if role == Qt.UserRole:
            return unit.id
        if role == STATUS_ROLE:
            return unit.status
        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if not index.isValid() or role != Qt.CheckStateRole or index.column() != COL_CHECK:
            return False
        unit = self._rows[index.row()]
        checked = Qt.CheckState(value) == Qt.Checked
        if unit.checked != checked:
            unit.checked = checked
            self.dataChanged.emit(index, index, [Qt.CheckStateRole])
            self.checkChanged.emit(unit.id, checked)
        return True

    def _display_name(self, unit: Unit) -> str:
        if unit.is_track:
            if unit.title and unit.artist:
                return f"{unit.artist} - {unit.title}"
            return unit.title or unit.name
        if unit.is_archive:
            return unit.name
        return unit.name

    def sort(self, column: int, order=Qt.AscendingOrder) -> None:
        self._sort_column = column
        self._sort_order = order
        self.layoutAboutToBeChanged.emit()
        self._apply_sort()
        self.layoutChanged.emit()


class SourceTreeModel(QStandardItemModel):
    """来源目录树：来源 → 专辑/散曲/压缩包。勾选用于批量勾选该范围。"""

    scopeSelected = Signal(str)          # 目录或来源路径（空字符串表示全部）
    checkToggled = Signal(list, bool)    # unit ids, checked

    ROLE_PATH = Qt.UserRole + 1
    ROLE_UNITS = Qt.UserRole + 2

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHorizontalHeaderLabels(["来源与专辑"])
        self._updating = False
        self.itemChanged.connect(self._on_item_changed)

    def rebuild(self, sources, units: list[Unit], loose: list[Unit]) -> None:
        self._updating = True
        expanded_paths = self._expanded_paths()
        self.clear()
        self.setHorizontalHeaderLabels(["来源与专辑"])
        root = self.invisibleRootItem()
        for source in sources:
            source_item = QStandardItem(self._folder_label(source.path))
            source_item.setEditable(False)
            source_item.setCheckable(True)
            source_item.setData(source.path, self.ROLE_PATH)
            source_item.setToolTip(source.path)
            source_units = [u for u in units if u.source_id == source.id]
            albums = [u for u in source_units if u.is_album]
            tracks = [u for u in source_units if u.is_track]
            archives = [u for u in source_units if u.is_archive]
            ws_units = [u for u in source_units if u.workspace]
            children: list[QStandardItem] = []
            for album in sorted(albums, key=lambda u: u.name.casefold()):
                item = self._unit_item(album, album.name)
                item.setData(album.path, self.ROLE_PATH)
                children.append(item)
            for track in sorted(tracks, key=lambda u: (u.album or "", u.track or 0)):
                if any(self._inside(track.path, u.boundary_path or u.path) for u in albums):
                    continue
                label = f"{track.album or '未分类专辑'} / {track.title or track.name}"
                children.append(self._unit_item(track, label))
            if archives:
                group = QStandardItem(f"压缩包（{len(archives)}）")
                group.setEditable(False)
                group.setData("", self.ROLE_PATH)
                for archive in sorted(archives, key=lambda u: u.name.casefold()):
                    group.appendRow(self._unit_item(archive, archive.name))
                children.append(group)
            if loose:
                group = QStandardItem(f"未关联附件（{len(loose)}）")
                group.setEditable(False)
                group.setData("", self.ROLE_PATH)
                for att in loose:
                    item = QStandardItem(Path(att.path).name)
                    item.setEditable(False)
                    item.setData(att.path, self.ROLE_PATH)
                    item.setToolTip(att.path)
                    group.appendRow(item)
                children.append(group)
            if ws_units:
                group = QStandardItem(f"解压工作区（{len(ws_units)}）")
                group.setEditable(False)
                group.setData("", self.ROLE_PATH)
                children.append(group)
            source_item.appendRows(children)
            root.appendRow(source_item)
            self._refresh_check_states(source_item)
            if source.path in expanded_paths:
                pass
        self._updating = False

    def _unit_item(self, unit: Unit, label: str) -> QStandardItem:
        item = QStandardItem(label)
        item.setEditable(False)
        item.setCheckable(True)
        item.setCheckState(Qt.Checked if unit.checked else Qt.Unchecked)
        item.setData([unit.id], self.ROLE_UNITS)
        item.setToolTip(unit.path)
        return item

    def _folder_label(self, path: str) -> str:
        return Path(path).name or path

    def _inside(self, path: str, root: str) -> bool:
        # 纯字符串前缀判断：resolve() 在 Win 上极慢（千级单元时可占 45s+）
        p = os.path.normcase(os.path.normpath(path))
        d = os.path.normcase(os.path.normpath(root))
        if p == d:
            return True
        return p.startswith(d + os.sep)

    def _expanded_paths(self) -> set[str]:
        paths: set[str] = set()
        for row in range(self.rowCount()):
            item = self.item(row)
            if item is not None and item.isCheckable():
                paths.add(item.data(self.ROLE_PATH) or "")
        return paths

    def _refresh_check_states(self, item: QStandardItem) -> None:
        children = [item.child(i) for i in range(item.rowCount())]
        unit_children = [c for c in children if c.data(self.ROLE_UNITS)]
        if not unit_children:
            return
        states = [c.checkState() for c in unit_children]
        if all(s == Qt.Checked for s in states):
            item.setCheckState(Qt.Checked)
        elif all(s == Qt.Unchecked for s in states):
            item.setCheckState(Qt.Unchecked)
        else:
            item.setCheckState(Qt.PartiallyChecked)

    def _collect_unit_ids(self, item: QStandardItem) -> list[str]:
        ids: list[str] = []
        data = item.data(self.ROLE_UNITS)
        if data:
            ids.extend(data)
        for row in range(item.rowCount()):
            ids.extend(self._collect_unit_ids(item.child(row)))
        return ids

    def _on_item_changed(self, item: QStandardItem) -> None:
        if self._updating or not item.isCheckable():
            return
        self._updating = True
        state = item.checkState()
        if item.rowCount():
            for row in range(item.rowCount()):
                child = item.child(row)
                if child is not None and child.isCheckable():
                    child.setCheckState(state)
        self._updating = False
        if state != Qt.PartiallyChecked:
            ids = self._collect_unit_ids(item)
            if ids:
                self.checkToggled.emit(ids, state == Qt.Checked)
        parent = item.parent()
        if parent is not None:
            self._updating = True
            self._refresh_check_states(parent)
            self._updating = False


class TreeHoverDelegate(QStyledItemDelegate):
    """来源树悬停移除按钮：来源根节点行尾浮现 ×，替代顶部独立移除按钮。"""

    removeRequested = Signal(str)   # 来源路径

    X_ROLE = Qt.UserRole + 1        # 与 SourceTreeModel.ROLE_PATH 一致（定义顺序在前）

    def _x_rect(self, option: QStyleOptionViewItem) -> QRect:
        size = 18
        return QRect(option.rect.right() - size - 4, option.rect.center().y() - size // 2,
                     size, size)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        if index.parent().isValid():
            return  # 仅来源根节点显示
        if not (option.state & QStyle.State_MouseOver):
            return
        painter.save()
        font = QFont(option.font)
        font.setPointSizeF(10)
        painter.setFont(font)
        hovered = self._x_rect(option).contains(
            option.widget.mapFromGlobal(QCursor.pos()))
        painter.setPen(QPen(QColor("#DC2626" if hovered else "#94A3B8"), 1.6,
                            Qt.SolidLine, Qt.RoundCap))
        r = self._x_rect(option).adjusted(5, 5, -5, -5)
        painter.drawLine(r.topLeft(), r.bottomRight())
        painter.drawLine(r.topRight(), r.bottomLeft())
        painter.restore()

    def editorEvent(self, event, model, option: QStyleOptionViewItem, index: QModelIndex) -> bool:
        from PySide6.QtCore import QEvent
        if (not index.parent().isValid() and event.type() == QEvent.MouseButtonRelease
                and getattr(event, "button", lambda: None)() == Qt.LeftButton):
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            if self._x_rect(option).contains(pos):
                path = index.data(self.X_ROLE)
                if path:
                    self.removeRequested.emit(str(path))
                    return True
        return super().editorEvent(event, model, option, index)
