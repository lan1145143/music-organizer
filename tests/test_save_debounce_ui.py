"""D4 回归：勾选/编辑等高频操作合并写盘（400ms 防抖），不再每次点击全量落盘。"""

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

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.domain.models import Unit, UnitKind  # noqa: E402
from app.paths import DataPaths  # noqa: E402
from app.services import AppService  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path):
    service = AppService(DataPaths(root=tmp_path / "data", portable=True))
    unit = Unit(kind=UnitKind.TRACK.value, path="X:/a/01.wav", name="01",
                title="曲", artist="歌手", checked=False)
    service.state.units = [unit]
    win = MainWindow(service)
    yield win, service
    win._save_timer.stop()
    win.close()


def test_repeated_checks_save_once_after_debounce(qapp, window, monkeypatch):
    win, service = window
    calls = []
    monkeypatch.setattr(service.state_store, "save", lambda state: calls.append(1))

    unit = service.state.units[0]
    unit_id = unit.id
    for _ in range(3):   # 连续三次勾选切换：400ms 内应只落盘一次
        win._on_model_check(unit_id, True)
        win._on_model_check(unit_id, False)
    assert calls == []   # 定时器窗口内未写盘

    QTest.qWait(600)     # 越过 400ms 防抖窗口
    assert len(calls) == 1


def test_close_saves_immediately_without_waiting(qapp, window, monkeypatch):
    win, service = window
    calls = []
    monkeypatch.setattr(service.state_store, "save", lambda state: calls.append(1))

    win._schedule_save()          # 防抖已挂起
    win._save_timer.stop()
    win.service.save_state()      # closeEvent 的直接落盘路径
    assert calls == [1]           # 不等防抖窗口，立即写入
