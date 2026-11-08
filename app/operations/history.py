"""操作历史：多次运行记录、异常退出状态识别、后续依赖检查。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .journal import tail_state


@dataclass
class RunOp:
    op_id: str = ""
    op: str = ""
    status: str = ""
    source: str = ""
    target: str = ""
    message: str = ""
    created: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    post: dict[str, list] = field(default_factory=dict)
    cue_backup: str = ""
    # 移动后来源目录处理：removed=已移除的空目录；kept_remaining=有剩余内容而保留；
    # kept_incomplete=任务未完成而保留。用于结果展示与撤销时恢复目录结构。
    source_dirs: dict[str, list] = field(default_factory=dict)
    # 歌词随歌曲移动/复制记录：source / target / encoding / mode / renamed
    lyrics: list[dict] = field(default_factory=list)
    # 连带附属资料随整夹移动/复制记录：source / target / renamed
    companions: list[dict] = field(default_factory=list)
    # 本次操作实际创建的目录：path / existed_before / op_id / phase / created_at。
    # 撤销时据此清理空目录（不做推测性删除）。
    created_dirs: list[dict] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        out = [self.source, self.target, *self.created, *self.removed]
        for rec in self.lyrics:
            out.extend([rec.get("source", ""), rec.get("target", "")])
        for rec in self.companions:
            out.extend([rec.get("source", ""), rec.get("target", "")])
        for rec in self.created_dirs:
            out.append(rec.get("path", ""))
        return [p for p in out if p]


@dataclass
class RunRecord:
    run_id: str = ""
    run_dir: str = ""
    started_at: str = ""
    finished_at: str = ""
    mode: str = "move"
    target_root: str = ""
    status: str = "done"
    counts: dict[str, int] = field(default_factory=dict)
    ops: list[RunOp] = field(default_factory=list)
    undone_by: str = ""
    undo_of: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        when = (self.started_at or "").replace("T", " ")
        mode = "复制" if self.mode == "copy" else "移动"
        suffix = "（撤销记录）" if self.undo_of else ""
        state = {"done": "", "running": "［未完成］", "interrupted": "［异常中断］"}.get(self.status, "")
        return f"{when}｜{mode}｜成功 {self.counts.get('success', 0)} 失败 {self.counts.get('failed', 0)}{state}{suffix}"

    def to_manifest(self) -> dict:
        return {
            "run_id": self.run_id, "started_at": self.started_at,
            "finished_at": self.finished_at, "mode": self.mode,
            "target_root": self.target_root, "status": self.status,
            "counts": self.counts, "undone_by": self.undone_by, "undo_of": self.undo_of,
            "notes": self.notes,
            "ops": [
                {"op_id": o.op_id, "op": o.op, "status": o.status, "source": o.source,
                 "target": o.target, "message": o.message, "created": o.created,
                 "removed": o.removed, "post": o.post, "cue_backup": o.cue_backup,
                 "source_dirs": o.source_dirs, "lyrics": o.lyrics,
                 "companions": o.companions, "created_dirs": o.created_dirs}
                for o in self.ops
            ],
        }


class HistoryStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def list_runs(self) -> list[RunRecord]:
        runs: list[RunRecord] = []
        for entry in self.root.iterdir() if self.root.is_dir() else []:
            if not entry.is_dir():
                continue
            record = self.load(entry)
            if record is not None:
                runs.append(record)
        # run_id 含毫秒且按时间递增，作为次级键保证同一秒内顺序稳定
        # （撤销记录与原记录常在同一秒创建，否则 later_runs/撤销顺序可能错乱）
        runs.sort(key=lambda r: (r.started_at or "", r.run_id), reverse=True)
        return runs

    def load(self, run_dir: Path) -> RunRecord | None:
        manifest = Path(run_dir) / "manifest.json"
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        record = RunRecord(
            run_id=data.get("run_id", Path(run_dir).name),
            run_dir=str(run_dir),
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
            mode=data.get("mode", "move"),
            target_root=data.get("target_root", ""),
            status=data.get("status", "done"),
            counts=data.get("counts", {}) or {},
            undone_by=data.get("undone_by", ""),
            undo_of=data.get("undo_of", ""),
            notes=data.get("notes", []) or [],
        )
        for op in data.get("ops", []):
            record.ops.append(RunOp(
                op_id=op.get("op_id", ""), op=op.get("op", ""), status=op.get("status", ""),
                source=op.get("source", ""), target=op.get("target", ""),
                message=op.get("message", ""), created=op.get("created", []) or [],
                removed=op.get("removed", []) or [], post=op.get("post", {}) or {},
                cue_backup=op.get("cue_backup", ""),
                source_dirs=op.get("source_dirs", {}) or {},
                lyrics=op.get("lyrics", []) or [],
                companions=op.get("companions", []) or [],
                created_dirs=op.get("created_dirs", []) or [],
            ))
        return record

    def save(self, record: RunRecord) -> None:
        run_dir = Path(record.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "manifest.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record.to_manifest(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    def reconcile(self, record: RunRecord) -> list[dict]:
        """异常中断后核对实际状态：目标是否已就绪、来源是否仍存在。"""
        if not record.ops:
            state = tail_state(Path(record.run_dir))
            rows = []
            for op_id, info in state.items():
                rows.append({
                    "op_id": op_id, "label": info.get("label", op_id),
                    "last_phase": info.get("last_phase", ""),
                    "source": info.get("source", ""), "target": info.get("target", ""),
                    "source_exists": Path(info.get("source", "")).exists() if info.get("source") else None,
                    "target_exists": Path(info.get("target", "")).exists() if info.get("target") else None,
                })
            return rows
        rows = []
        for op in record.ops:
            rows.append({
                "op_id": op.op_id, "label": op.op, "status": op.status,
                "source": op.source, "target": op.target,
                "source_exists": Path(op.source).exists() if op.source else None,
                "target_exists": Path(op.target).exists() if op.target else None,
                "message": op.message,
            })
        return rows

    def later_runs(self, record: RunRecord) -> list[RunRecord]:
        runs = self.list_runs()
        try:
            index = [r.run_id for r in runs].index(record.run_id)
        except ValueError:
            return []
        return runs[:index]  # 列表按时间倒序，前面的更新

    def overlapping_runs(self, record: RunRecord) -> list[RunRecord]:
        own = [os.path.normcase(p) for op in record.ops for p in op.paths]

        def overlaps(paths: list[str]) -> bool:
            for later in paths:
                key = os.path.normcase(later)
                for mine in own:
                    if key == mine or key.startswith(mine + os.sep) or mine.startswith(key + os.sep):
                        return True
            return False

        hits: list[RunRecord] = []
        for later in self.later_runs(record):
            if later.undone_by:
                continue
            paths = [p for op in later.ops for p in op.paths]
            if overlaps(paths):
                hits.append(later)
        return hits

    def interrupted(self) -> list[RunRecord]:
        return [r for r in self.list_runs() if r.status in ("running", "interrupted")]
