"""操作日志：追加写 + fsync，记录阶段与恢复状态（不是最终文本日志）。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "journal.jsonl"
        self._fh = None

    def open(self) -> None:
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def write(self, phase: str, **fields: Any) -> None:
        record = {"ts": datetime.now().isoformat(timespec="milliseconds"), "phase": phase}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False)
        if self._fh is None:
            self.open()
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def __enter__(self) -> "Journal":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def read_journal(run_dir: Path) -> list[dict]:
    path = Path(run_dir) / "journal.jsonl"
    records: list[dict] = []
    if not path.is_file():
        return records
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def tail_state(run_dir: Path) -> dict[str, dict]:
    """按 op_id 归并每项最新阶段，用于异常退出后的状态识别。"""
    state: dict[str, dict] = {}
    for record in read_journal(run_dir):
        op_id = record.get("op_id") or record.get("item_id")
        if not op_id:
            continue
        entry = state.setdefault(op_id, {"phases": []})
        entry["phases"].append(record.get("phase"))
        entry.update({k: v for k, v in record.items() if k not in ("phase",)})
        entry["last_phase"] = record.get("phase")
        entry["last_ts"] = record.get("ts")
    return state


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
