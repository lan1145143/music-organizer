"""P0-5 回归：高频操作（勾选/编辑/边界/分组）后当前行与详情不丢失。

旧实现对每次勾选/编辑调用 refresh_all()（整模型 reset），Qt 必然清空
选择 → 详情跳回空状态。新实现走 _reload_preserving（按稳定 unit id 恢复）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.paths import DataPaths  # noqa: E402
from app.services import AppService  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def make_window(tmp_path: Path) -> tuple[MainWindow, AppService]:
    service = AppService(DataPaths(root=tmp_path / "data", portable=True))
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "01 - A - one.wav").write_bytes(b"\x00" * 64)
    (src / "02 - A - two.wav").write_bytes(b"\x00" * 64)
    service.register_sources([str(src)])
    service.rescan()
    window = MainWindow(service)
    return window, service


def _select_row(window: MainWindow, row: int):
    index = window.table_model.index(row, 0)
    window.table.selectionModel().select(
        index, QItemSelectionModel.SelectionFlag.ClearAndSelect |
        QItemSelectionModel.SelectionFlag.Rows)
    return window.table_model.unit_at(row)


def test_check_toggle_keeps_selection_and_detail(qapp, tmp_path):
    window, _service = make_window(tmp_path)
    try:
        unit = _select_row(window, 0)
        window._on_model_check(unit.id, False)
        rows = window.table.selectionModel().selectedRows()
        assert rows, "勾选后选择被清空"
        assert str(rows[0].data(Qt.UserRole)) == unit.id
        assert window.detail.unit is not None
        assert window.detail.unit.id == unit.id
        assert unit.checked is False and unit.user_checked is False
    finally:
        window.close()


def test_field_edit_keeps_selection_and_detail(qapp, tmp_path):
    window, _service = make_window(tmp_path)
    try:
        unit = _select_row(window, 0)
        window._on_field_edited(unit.id, "title", "新歌名")
        rows = window.table.selectionModel().selectedRows()
        assert rows, "编辑后选择被清空"
        assert str(rows[0].data(Qt.UserRole)) == unit.id
        assert window.detail.unit is not None
        assert window.detail.unit.id == unit.id
        # 编辑已生效（列表/详情用的是同一个 unit 对象）
        assert unit.title == "新歌名"
        assert unit.origins.get("title") == "user"
    finally:
        window.close()


def test_group_filter_switch_preserves_selection(qapp, tmp_path):
    window, _service = make_window(tmp_path)
    try:
        unit = _select_row(window, 0)
        window._on_search("")      # 无过滤：reload 路径
        window._on_search(unit.title or unit.name)
        rows = window.table.selectionModel().selectedRows()
        assert rows and str(rows[0].data(Qt.UserRole)) == unit.id
    finally:
        window.close()


def test_check_toggle_syncs_tree(qapp, tmp_path):
    """勾选主表格后，来源树上的复选框状态必须同步（旧实现漏掉树重建）。"""
    window, _service = make_window(tmp_path)
    try:
        unit = _select_row(window, 0)
        window._on_model_check(unit.id, False)
        tree = window.tree_model
        found = {"checked": None}

        def walk(item):
            data = item.data(tree.ROLE_UNITS)
            if data and unit.id in data:
                found["checked"] = item.checkState() == Qt.Checked
                return True
            for row in range(item.rowCount()):
                child = item.child(row)
                if child is not None and walk(child):
                    return True
            return False

        root = tree.invisibleRootItem()
        for row in range(root.rowCount()):
            if walk(root.child(row)):
                break
        assert found["checked"] is False
    finally:
        window.close()
