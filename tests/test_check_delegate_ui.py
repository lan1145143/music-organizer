"""B2 回归：CheckDelegate 点击命中区必须与绘制区一致（单元格正中 16px）。

旧实现勾选框绘制在单元格正中，但未重写 editorEvent，Qt 默认命中矩形
左对齐——瞄准可见勾选框点击约半数无效。"""

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

from PySide6.QtCore import QEvent, QModelIndex, QPointF, QRect, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QStyleOptionViewItem, QApplication  # noqa: E402

from app.domain.models import Unit, UnitKind  # noqa: E402
from app.ui.models import CheckDelegate, COL_CHECK, UnitTableModel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_model(qapp) -> UnitTableModel:
    unit = Unit(kind=UnitKind.TRACK.value, path="X:/a/01.wav", name="01",
                title="曲", artist="歌手", checked=False)
    model = UnitTableModel()
    model.set_units([unit])
    return model


def _release_event(x: float, y: float) -> QMouseEvent:
    return QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(x, y), QPointF(x, y),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)


def _delegate_option() -> tuple[CheckDelegate, QStyleOptionViewItem]:
    delegate = CheckDelegate(lambda: "#3B6EA5")
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 40, 24)   # 40px 宽勾选列，框画在 x≈12-28
    return delegate, option


def test_click_on_painted_checkbox_toggles(qapp):
    model = _make_model(qapp)
    delegate, option = _delegate_option()
    index = model.index(0, COL_CHECK)
    center = option.rect.center()

    assert delegate.editorEvent(_release_event(center.x(), center.y()),
                                model, option, index) is True
    assert model._rows[0].checked is True   # 命中绘制框中心：勾上

    assert delegate.editorEvent(_release_event(center.x(), center.y()),
                                model, option, index) is True
    assert model._rows[0].checked is False  # 再点一次：取消


def test_click_outside_checkbox_does_not_toggle(qapp):
    model = _make_model(qapp)
    delegate, option = _delegate_option()
    index = model.index(0, COL_CHECK)

    # 右侧空白区（既不在绘制框 x≈12-28，也不在样式默认命中区 x≈2-14）
    assert delegate.editorEvent(_release_event(38, 12), model, option, index) is False
    assert model._rows[0].checked is False


def test_check_rect_matches_painted_rect(qapp):
    """绘制与命中必须共用同一矩形，避免再次错位。"""
    delegate, option = _delegate_option()
    rect = delegate._check_rect(option)
    assert rect == QRect(option.rect.center().x() - 8,
                         option.rect.center().y() - 8, 16, 16)
