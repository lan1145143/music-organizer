"""跨层任务取消：UI 线程取消后台任务时的统一异常与检查助手。

UI 的 TaskRunner 把 ``is_cancelled`` 回调传给任务函数；任务在安全点
（来源之间、逐项之间、复制块之间）调用 ``raise_if_cancelled``。
抛出的 TaskCancelled 由 Worker 捕获并转成 cancelled 信号，
主线程据此恢复界面状态（解除 busy），不会误报为成功。
"""

from __future__ import annotations

from typing import Callable


class TaskCancelled(RuntimeError):
    """后台任务被用户取消。Worker 捕获后发送 cancelled 信号（不发送 finished）。"""


def raise_if_cancelled(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise TaskCancelled("任务已取消")
