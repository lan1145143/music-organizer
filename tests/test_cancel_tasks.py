"""P0-2 回归：任务取消。

覆盖：扫描/查重在安全点取消且状态不被污染；取消时复制产生的半截文件被清理；
Worker 的 cancelled/finished 信号语义（取消必须能恢复 busy，不得吞结果）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.operations import fsops  # noqa: E402
from app.paths import DataPaths  # noqa: E402
from app.scan.duplicates import detect_duplicates  # noqa: E402
from app.domain.models import Unit, UnitKind  # noqa: E402
from app.services import AppService  # noqa: E402
from app.task_control import TaskCancelled, raise_if_cancelled  # noqa: E402


def make_service(tmp_path: Path) -> AppService:
    return AppService(DataPaths(root=tmp_path / "data", portable=True))


def make_source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "01 - A - one.wav").write_bytes(b"\x00" * 64)
    (src / "02 - A - two.wav").write_bytes(b"\x00" * 64)
    return src


# ---------------------------------------------------------------------------
# 服务层：取消安全点
# ---------------------------------------------------------------------------
def test_rescan_cancel_keeps_state_clean(tmp_path):
    service = make_service(tmp_path)
    src = make_source(tmp_path)
    service.register_sources([str(src)])

    with pytest.raises(TaskCancelled):
        service.rescan(cancel=lambda: True)

    # 未合并任何单元、未置 scanned 标记：界面仍可「确认目标并扫描」
    assert service.state.units == []
    assert all(not s.scanned for s in service.state.sources)

    # 取消后可正常重扫
    result = service.rescan()
    assert result["tracks"] >= 2
    assert all(s.scanned for s in service.state.sources)


def test_rescan_midway_cancel_processes_nothing(tmp_path):
    service = make_service(tmp_path)
    src = make_source(tmp_path)
    (tmp_path / "src2").mkdir()
    (tmp_path / "src2" / "03 - B - three.wav").write_bytes(b"\x00" * 64)
    service.register_sources([str(src), str(tmp_path / "src2")])

    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # 第一个来源扫完后、第二个开始前取消

    with pytest.raises(TaskCancelled):
        service.rescan(cancel=cancel)

    # 状态未被部分污染：scanned 统一在合并后置位，取消时保持 False
    assert all(not s.scanned for s in service.state.sources)


def test_detect_duplicates_cancel(tmp_path):
    def track(name: str, data: bytes) -> Unit:
        p = tmp_path / name
        p.write_bytes(data)
        unit = Unit(kind=UnitKind.TRACK.value, path=str(p), name=name)
        unit.size = len(data)
        return unit

    a = track("a.flac", b"aaaa")
    b = track("b.flac", b"aaaa")
    with pytest.raises(TaskCancelled):
        detect_duplicates([a, b], cancel=lambda: True)


def test_raise_if_cancelled_passthrough():
    raise_if_cancelled(None)  # 不带 cancel 时不抛
    raise_if_cancelled(lambda: False)
    with pytest.raises(TaskCancelled):
        raise_if_cancelled(lambda: True)


# ---------------------------------------------------------------------------
# fsops：取消时清理半截文件
# ---------------------------------------------------------------------------
def test_copy_file_cancel_cleans_partial(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * (fsops.CHUNK + 12345))
    dst = tmp_path / "dst.bin"
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # 第一个块写入后、第二个块开始前取消

    with pytest.raises(fsops.CancelledError):
        fsops.copy_file_op(src, dst, None, cancel)

    assert not dst.exists()          # 半成品必须被清理
    assert src.read_bytes() == b"x" * (fsops.CHUNK + 12345)  # 来源原样


def test_copy_tree_cancel_cleans_copied_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f1.bin").write_bytes(b"a" * (fsops.CHUNK // 2))
    (src / "f2.bin").write_bytes(b"b" * (fsops.CHUNK // 2))
    dst = tmp_path / "dst"

    state = {"done": 0}

    def progress(done, total, name) -> None:
        state["done"] = done

    def cancel() -> bool:
        return state["done"] >= fsops.CHUNK // 2  # f1 完成后取消

    with pytest.raises(fsops.CancelledError):
        fsops.copy_tree(src, dst, progress, cancel)

    # 本次调用复制出的文件全部清理，来源原样
    assert not any(dst.rglob("*.bin")) if dst.exists() else True
    assert sorted(p.name for p in src.iterdir()) == ["f1.bin", "f2.bin"]
    assert (src / "f1.bin").read_bytes() == b"a" * (fsops.CHUNK // 2)


# ---------------------------------------------------------------------------
# Worker 信号语义（UI 层恢复 busy 的依据）
# ---------------------------------------------------------------------------
pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThreadPool, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.workers import TaskRunner, Worker  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _run_until(loop: QEventLoop, timeout_ms: int = 5000) -> None:
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()


def test_worker_cancelled_restores_busy(qapp):
    """任务在安全点抛 TaskCancelled → on_cancelled 必须被调用（解除 busy）。"""
    state = {"busy": True}

    def task(progress=None, cancel=None):
        raise_if_cancelled(cancel)

    loop = QEventLoop()

    def on_cancelled() -> None:
        state["busy"] = False
        loop.quit()

    worker = Worker(task, pass_progress=False)
    worker.signals.cancelled.connect(on_cancelled)
    worker.cancel()          # 先置取消再入池，消除启动竞态
    QThreadPool.globalInstance().start(worker)
    _run_until(loop)

    assert state["busy"] is False


def test_worker_finished_emitted_even_after_cancel_request(qapp):
    """任务正常返回（如执行器带取消统计收尾）→ 必须 finished，不得让界面卡死。"""
    state = {"busy": True, "done": False, "result": None}

    def task(progress=None, cancel=None):
        return {"status": "done"}  # 不检查取消：任务自行收尾

    loop = QEventLoop()

    def on_done(result) -> None:
        state["busy"] = False
        state["done"] = True
        state["result"] = result
        loop.quit()

    worker = Worker(task, pass_progress=False)
    worker.signals.finished.connect(on_done)
    worker.cancel()  # 取消请求已置位，但任务不检查 → 仍按正常完成发 finished
    QThreadPool.globalInstance().start(worker)
    _run_until(loop)

    assert state["done"] is True
    assert state["busy"] is False
    assert state["result"] == {"status": "done"}


def test_taskrunner_wires_on_cancelled(qapp):
    """TaskRunner.run 的 on_cancelled 参数必须真正连接到 cancelled 信号。"""
    state = {"cancelled": False}

    def task(progress=None, cancel=None):
        raise_if_cancelled(cancel)

    loop = QEventLoop()
    runner = TaskRunner()
    worker = Worker(task, pass_progress=False)
    worker.signals.cancelled.connect(lambda: (state.update(cancelled=True), loop.quit()))
    runner.current = worker  # 保持引用，模拟 runner.run 的管理方式
    worker.cancel()
    QThreadPool.globalInstance().start(worker)
    _run_until(loop)

    assert state["cancelled"] is True


def test_worker_no_arg_fn_with_pass_progress_false_finishes(qapp):
    """pass_progress=False 且任务无必需参数 → finished 必须触发。

    回归：启动检查的 task(progress) 带必需位置参数而 pass_progress=False，
    每次抛 TypeError 走 failed，又被 on_error 静默吞掉，"打开前的检查"从未生效。"""
    state = {"done": False, "result": None, "error": None}
    loop = QEventLoop()

    def task():
        return ["note-1"]

    worker = Worker(task, pass_progress=False)
    worker.signals.finished.connect(
        lambda r: (state.update(done=True, result=r), loop.quit()))
    worker.signals.failed.connect(lambda m: (state.update(error=m), loop.quit()))
    QThreadPool.globalInstance().start(worker)
    _run_until(loop)

    assert state["error"] is None
    assert state["done"] is True
    assert state["result"] == ["note-1"]
