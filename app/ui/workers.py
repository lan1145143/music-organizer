"""后台任务：扫描、哈希、解压、文件传输都在线程池执行，主线程保持响应。"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from ..task_control import TaskCancelled


class WorkerSignals(QObject):
    progress = Signal(str, int, int)      # message, current, total
    finished = Signal(object)             # 返回值
    failed = Signal(str)
    cancelled = Signal()


class Worker(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args, pass_progress: bool = True, **kwargs) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.pass_progress = pass_progress
        self._cancelled = False
        self.setAutoDelete(True)

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:
        try:
            kwargs = dict(self.kwargs)
            if self.pass_progress:
                kwargs.setdefault("progress", self.signals.progress.emit)
            if "cancel" in kwargs or self._needs_cancel():
                kwargs["cancel"] = self.is_cancelled
            result = self.fn(*self.args, **kwargs)
        except TaskCancelled:
            # 任务在安全点检测到取消：发 cancelled，由 on_cancelled 恢复界面。
            # 绝不吞掉——旧实现从不给 on_cancelled 接线，取消后界面永久 busy。
            self.signals.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - 后台异常需要回传界面
            detail = traceback.format_exc(limit=3)
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}\n{detail}")
        else:
            # 任务正常返回（可能携带部分完成的统计，如执行器的取消计数）：
            # 一律发 finished，保证 on_done 一定执行、界面一定会解除 busy。
            # 取消与否由返回值本身表达（cancelled 计数/状态），不在此丢弃结果。
            self.signals.finished.emit(result)

    def _needs_cancel(self) -> bool:
        try:
            import inspect
            params = inspect.signature(self.fn).parameters
            return "cancel" in params
        except (TypeError, ValueError):
            return False


class TaskRunner(QObject):
    """轻量任务调度：同一时间只保留最近一次任务的引用，便于取消。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.pool = QThreadPool.globalInstance()
        self.current: Worker | None = None

    def run(self, fn: Callable[..., Any], on_done=None, on_error=None, on_progress=None,
            on_cancelled=None, pass_progress: bool = True, **kwargs) -> Worker:
        worker = Worker(fn, pass_progress=pass_progress, **kwargs)
        if on_progress is not None:
            worker.signals.progress.connect(on_progress)
        if on_done is not None:
            worker.signals.finished.connect(on_done)
        if on_error is not None:
            worker.signals.failed.connect(on_error)
        if on_cancelled is not None:
            worker.signals.cancelled.connect(on_cancelled)
        self.current = worker
        self.pool.start(worker)
        return worker

    def cancel_current(self) -> None:
        if self.current is not None:
            self.current.cancel()

    @property
    def busy(self) -> bool:
        return self.current is not None and not self.current.is_cancelled()

    def clear(self) -> None:
        self.current = None
