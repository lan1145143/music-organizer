"""撤销：按历史记录反向恢复，先预览校验，再逐项执行。

规则：
- 撤销前再次预览；发生后续移动、内容变化、同名占用、缺文件时不盲目反向覆盖。
- 复制操作撤销只删除本次创建且确认未被后续修改的目标。
- 文件夹含后来新增内容时，不递归删除整个目标文件夹。
- CUE 引用修正撤销时恢复修改前内容。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import fsops
from .history import HistoryStore, RunOp, RunRecord
from .journal import Journal, now_stamp
from ..domain.naming import walk_pruned


@dataclass
class UndoAction:
    op_id: str = ""
    op: str = ""
    label: str = ""
    status: str = "ready"        # ready | conflict | blocked | skipped
    message: str = ""
    move_from: str = ""
    move_to: str = ""
    delete_files: list[str] = field(default_factory=list)
    keep_files: list[str] = field(default_factory=list)
    restore_dirs: list[str] = field(default_factory=list)  # 撤销时恢复的来源目录
    cue_restore: tuple[str, str] | None = None
    # 歌词回写：[(当前目标, 原来源), ...]，撤销时把随歌移动的歌词放回原处
    lyrics_restore: list[tuple[str, str]] = field(default_factory=list)
    # 连带附属资料回写：[(当前目标, 原来源), ...]
    companions_restore: list[tuple[str, str]] = field(default_factory=list)
    # 本次操作创建、撤销后计划清理的空目录（深度倒序执行；只删空目录）
    dirs_delete: list[str] = field(default_factory=list)
    # 不删除的目录及原因：{"path", "reason"}（原本存在/目标根/非空/被后续操作使用）
    dirs_kept: list[dict] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.status == "ready"


@dataclass
class UndoPlan:
    run: RunRecord | None = None
    actions: list[UndoAction] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def ready_actions(self) -> list[UndoAction]:
        return [a for a in self.actions if a.ready]

    def counts(self) -> dict[str, int]:
        data = {"ready": 0, "conflict": 0, "blocked": 0, "skipped": 0}
        for action in self.actions:
            data[action.status] = data.get(action.status, 0) + 1
        return data


class UndoService:
    def __init__(self, history: HistoryStore) -> None:
        self.history = history

    # ------------------------------------------------------------------
    def build(self, run: RunRecord) -> UndoPlan:
        plan = UndoPlan(run=run)
        if run.undo_of:
            plan.messages.append("这是一次撤销记录，不支持对撤销记录再次撤销")
            return plan
        if run.undone_by:
            plan.messages.append(f"该记录已撤销过（撤销记录：{run.undone_by}）")
        overlaps = self.history.overlapping_runs(run)
        if overlaps:
            names = "、".join(r.run_id for r in overlaps[:3])
            plan.messages.append(f"存在后续操作记录（{names}），重叠项需先撤销后续记录或手动处理")

        overlap_paths = [os.path.normcase(p) for r in overlaps for op in r.ops for p in op.paths]

        for op in reversed(run.ops):
            plan.actions.append(self._build_action(run, op, overlap_paths))
        return plan

    # ------------------------------------------------------------------
    def _build_action(self, run: RunRecord, op: RunOp,
                      overlap_paths: list[str]) -> UndoAction:
        action = UndoAction(op_id=op.op_id, op=op.op, label=op.message or op.op,
                            move_from=op.target, move_to=op.source)
        if op.status not in ("success", "partial", "skipped"):
            action.status = "skipped"
            action.message = f"该项状态为 {op.status}，无需撤销"
            return action
        if op.op == "cue_fix":
            backup = op.cue_backup
            if not backup or not Path(backup).is_file():
                action.status = "conflict"
                action.message = "缺少原始 CUE 备份，需手动恢复"
                return action
            action.cue_restore = (op.source, backup)
            action.message = "恢复 CUE 修改前内容"
            return action

        target = Path(op.target)
        source = Path(op.source)
        if _has_overlap(target, overlap_paths) or _has_overlap(source, overlap_paths):
            action.status = "conflict"
            action.message = "该路径在后续操作中被再次改动，需先撤销后续记录"
            return action

        if run.mode == "copy":
            return self._build_copy_undo(action, op, run, overlap_paths)
        return self._build_move_undo(action, op, run, overlap_paths)

    # ------------------------------------------------------------------
    def _classify_created_dirs(self, op: RunOp, run: RunRecord,
                               overlap_paths: list[str]) -> tuple[list[str], list[dict]]:
        """把本次执行记录的 created_dirs 分成"计划清理"与"不删除"两类。

        依据操作日志（非推测）：只考虑本次新建（existed_before=False）、
        位于本次目标根内、未被后续操作记录使用的目录。
        目录当前是否为空在 apply 时再核验——预览时文件尚未回搬/删除，
        预览列表只能表达"将尝试清理"，实际以执行时为准。
        """
        delete: list[str] = []
        kept: list[dict] = []
        target_root = os.path.normcase(os.path.normpath(run.target_root or ""))
        for record in op.created_dirs:
            path = record.get("path", "")
            if not path:
                continue
            key = os.path.normcase(os.path.normpath(path))
            if record.get("existed_before"):
                kept.append({"path": path, "reason": "本次操作前已存在"})
                continue
            if not target_root:
                kept.append({"path": path, "reason": "记录缺少目标根，谨慎起见不删"})
                continue
            if key == target_root:
                kept.append({"path": path, "reason": "目标根目录不删除"})
                continue
            if not key.startswith(target_root + os.sep):
                kept.append({"path": path, "reason": "不在本次目标根内，不删除"})
                continue
            if _has_overlap(Path(path), overlap_paths):
                kept.append({"path": path, "reason": "被后续操作记录使用"})
                continue
            delete.append(path)
        return delete, kept

    def _build_copy_undo(self, action: UndoAction, op: RunOp, run: RunRecord,
                         overlap_paths: list[str]) -> UndoAction:
        files = [p for p in op.created if Path(p).is_file()]
        missing = [p for p in op.created if not Path(p).exists()]
        if not any(Path(p).exists() for p in op.created) and not missing:
            action.status = "skipped"
            action.message = "没有可删除的本次创建内容"
            action.dirs_delete, action.dirs_kept = self._classify_created_dirs(
                op, run, overlap_paths)
            return action
        changed: list[str] = []
        for path in files:
            if not _unchanged(path, op.post):
                changed.append(path)
        delete = [p for p in files if p not in changed]
        action.dirs_delete, action.dirs_kept = self._classify_created_dirs(
            op, run, overlap_paths)
        if changed:
            action.keep_files = changed
            if delete:
                action.status = "ready"
                action.message = (f"删除本次创建且未修改的 {len(delete)} 个文件；"
                                  f"保留 {len(changed)} 个已修改文件（不删除后续新增内容）")
            else:
                action.status = "conflict"
                action.message = f"{len(changed)} 个目标文件在执行后被修改，不能删除"
                action.dirs_delete = []   # 冲突项不做任何删除
            action.delete_files = delete
            return action
        action.delete_files = delete
        action.status = "ready" if delete else "skipped"
        action.message = f"删除本次创建的 {len(delete)} 个文件"
        return action

    def _build_move_undo(self, action: UndoAction, op: RunOp, run: RunRecord,
                         overlap_paths: list[str]) -> UndoAction:
        target = Path(op.target)
        source = Path(op.source)
        removed_dirs = (op.source_dirs or {}).get("removed", []) or []
        kept_remaining = (op.source_dirs or {}).get("kept_remaining", []) or []
        if not target.exists():
            action.status = "conflict"
            action.message = "目标已不存在（可能被移动或删除），需手动处理"
            return action
        if source.exists():
            action.status = "conflict"
            action.message = "原位置已存在同名内容，不能直接回写"
            return action
        extra = _extra_files(target, op.created)
        if extra:
            action.status = "conflict"
            action.message = (f"目标中有执行后新增的内容（{len(extra)} 个文件），"
                              "不递归删除整个文件夹，需手动处理")
            action.keep_files = extra
            return action
        action.dirs_delete, action.dirs_kept = self._classify_created_dirs(
            op, run, overlap_paths)
        action.status = "ready"
        message = f"移回原位置：{source}"
        # 歌词：随歌移动过去的，一并放回原来源位置
        lyrics_pairs: list[tuple[str, str]] = []
        blocked_lyrics: list[str] = []
        for rec in (op.lyrics or []):
            lsrc = rec.get("source", "")
            ldst = rec.get("target", "")
            if not lsrc or not ldst:
                continue
            if os.path.normcase(lsrc) == os.path.normcase(ldst):
                continue
            if Path(lsrc).exists():
                blocked_lyrics.append(lsrc)
                continue
            if Path(ldst).is_file():
                lyrics_pairs.append((ldst, lsrc))
        if lyrics_pairs:
            action.lyrics_restore = lyrics_pairs
            message += f"；歌词 {len(lyrics_pairs)} 个一并回写"
        if blocked_lyrics:
            message += ("；" + f"{len(blocked_lyrics)} 个歌词原位置已有同名文件，需手动处理")
        companion_pairs: list[tuple[str, str]] = []
        blocked_companions: list[str] = []
        for rec in (op.companions or []):
            csrc = rec.get("source", "")
            cdst = rec.get("target", "")
            if not csrc or not cdst:
                continue
            if os.path.normcase(csrc) == os.path.normcase(cdst):
                continue
            if Path(csrc).exists():
                blocked_companions.append(csrc)
                continue
            if Path(cdst).is_file():
                companion_pairs.append((cdst, csrc))
        if companion_pairs:
            action.companions_restore = companion_pairs
            message += f"；连带附属资料 {len(companion_pairs)} 个一并回写"
        if blocked_companions:
            message += ("；" + f"{len(blocked_companions)} 个连带资料原位置已有同名文件，需手动处理")
        if removed_dirs:
            message += (f"；恢复执行时移除的来源目录（{len(removed_dirs)} 个）："
                        + "、".join(Path(p).name for p in removed_dirs[:4]))
        action.message = message
        # 供 apply 恢复目录结构
        action.restore_dirs = [p for p in removed_dirs
                               if not Path(p).exists() and Path(p).is_absolute()]
        if kept_remaining:
            action.message += "；来源目录因有剩余内容未移除，无需恢复"
        return action

    # ------------------------------------------------------------------
    def apply(self, plan: UndoPlan, progress: Callable[[int, int, str], None] | None = None,
              cancel: Callable[[], bool] | None = None) -> RunRecord:
        run = plan.run
        actions = plan.ready_actions
        undo_run = RunRecord(
            run_id=f"{now_stamp()}-undo-{run.run_id if run else 'unknown'}",
            started_at=datetime.now().isoformat(timespec="seconds"),
            mode=run.mode if run else "move",
            target_root=run.target_root if run else "",
            status="running",
            undo_of=run.run_id if run else "",
        )
        undo_run.run_dir = str(self.history.root / undo_run.run_id)
        counts: dict[str, int] = {}
        record_notes: list[str] = []   # 目录清理结果摘要（写入撤销记录供历史查看）
        with Journal(Path(undo_run.run_dir)) as journal:
            journal.write("undo_start", undo_of=run.run_id if run else "",
                          actions=len(actions))
            for index, action in enumerate(actions, start=1):
                if cancel is not None and cancel():
                    journal.write("undo_cancelled", done=index - 1)
                    break
                status = "success"
                message = action.message
                try:
                    if action.cue_restore is not None:
                        cue_path, backup = action.cue_restore
                        shutil.copy2(backup, cue_path)
                        journal.write("undo_cue_restore", op_id=action.op_id,
                                      cue=cue_path, backup=backup)
                    elif action.delete_files:
                        for path in action.delete_files:
                            try:
                                os.remove(path)
                                journal.write("undo_delete", op_id=action.op_id, path=path)
                            except OSError as exc:
                                status = "partial"
                                message += f"；删除失败：{path} ({exc.strerror or exc})"
                        # 只清理"本次创建、现为空"的目录；非空/原本存在的保留并记录
                        removed_dirs, kept_dirs = _cleanup_created_dirs(
                            action, journal, action.op_id)
                        if removed_dirs:
                            record_notes.append(
                                f"已清理本次创建的空目录 {len(removed_dirs)} 个")
                        for info in kept_dirs:
                            record_notes.append(
                                f"保留目录：{info['path']}（{info['reason']}）")
                    elif action.move_from and action.move_to:
                        src = Path(action.move_from)
                        dst = Path(action.move_to)
                        # 执行时移除的来源目录在回写前恢复（空目录重建）
                        for directory in action.restore_dirs:
                            try:
                                Path(directory).mkdir(parents=True, exist_ok=True)
                                journal.write("undo_dir_restored", op_id=action.op_id,
                                              directory=directory)
                            except OSError as exc:
                                status = "partial"
                                message += f"；来源目录恢复失败：{directory} ({exc.strerror or exc})"
                        # 连带附属资料先回原位（它们住在整夹目标目录里，
                        # 必须在整夹树整体回搬之前移出，否则会被拖错位置）
                        for from_path, to_path in action.companions_restore:
                            try:
                                if Path(to_path).exists():
                                    # 构建撤销计划时校验过，此处重查：间隙被占用绝不覆盖
                                    raise OSError(f"目标已存在，为避免覆盖已停止：{to_path}")
                                Path(to_path).parent.mkdir(parents=True, exist_ok=True)
                                fsops.move_file(Path(from_path), Path(to_path))
                                journal.write("undo_companion_restore", op_id=action.op_id,
                                              source=from_path, target=to_path)
                            except OSError as exc:
                                status = "partial" if status == "success" else status
                                message += (f"；连带资料回写失败："
                                            f"{Path(from_path).name} ({exc.strerror or exc})")
                        if dst.exists():
                            status = "failed"
                            message += "；回写目标已出现同名内容，为避免覆盖已停止"
                            journal.write("undo_skipped", op_id=action.op_id,
                                          target=str(dst), reason="target-exists")
                        else:
                            result = (fsops.move_tree(src, dst) if src.is_dir()
                                      else fsops.move_file(src, dst))
                            if not result.verified:
                                status = "failed"
                                message += "；回写校验未通过"
                            journal.write("undo_move", op_id=action.op_id, source=str(src),
                                          target=str(dst), verified=result.verified)
                        # 移动回原位后：清理本次创建且现在为空的目标目录
                        # （深度倒序：先 CD1/专辑，再尝试艺术家目录）
                        removed_dirs, kept_dirs = _cleanup_created_dirs(
                            action, journal, action.op_id)
                        if removed_dirs:
                            message += (f"；已清理本次创建的空目录 {len(removed_dirs)} 个")
                            record_notes.append(
                                f"已清理本次创建的空目录 {len(removed_dirs)} 个："
                                + "、".join(removed_dirs[:6]))
                        for info in kept_dirs:
                            record_notes.append(
                                f"保留目录：{info['path']}（{info['reason']}）")
                        for from_path, to_path in action.lyrics_restore:
                            try:
                                if Path(to_path).exists():
                                    # 构建撤销计划时校验过，此处重查：间隙被占用绝不覆盖
                                    raise OSError(f"目标已存在，为避免覆盖已停止：{to_path}")
                                Path(to_path).parent.mkdir(parents=True, exist_ok=True)
                                fsops.move_file(Path(from_path), Path(to_path))
                                journal.write("undo_lyrics_restore", op_id=action.op_id,
                                              source=from_path, target=to_path)
                            except OSError as exc:
                                status = "partial" if status == "success" else status
                                message += (f"；歌词回写失败："
                                            f"{Path(from_path).name} ({exc.strerror or exc})")
                except OSError as exc:
                    status = "failed"
                    message = f"{type(exc).__name__}: {exc.strerror or exc}"
                    journal.write("undo_failed", op_id=action.op_id, error=message)
                counts[status] = counts.get(status, 0) + 1
                undo_run.ops.append(RunOp(op_id=action.op_id, op="undo", status=status,
                                          source=action.move_from, target=action.move_to,
                                          message=message))
                if progress is not None:
                    progress(f"撤销 [{index}/{len(actions)}] {message}", index, len(actions))
                journal.write("undo_item", op_id=action.op_id, status=status, message=message)
            undo_run.finished_at = datetime.now().isoformat(timespec="seconds")
            undo_run.counts = counts
            undo_run.status = "done"
            undo_run.notes = record_notes
            journal.write("undo_finished", counts=counts, dir_notes=len(record_notes))
        self.history.save(undo_run)
        if run is not None:
            run.undone_by = undo_run.run_id
            self.history.save(run)
        return undo_run


# ----------------------------------------------------------------------
def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _has_overlap(path: Path, others: list[str]) -> bool:
    key = _norm(str(path))
    for other in others:
        if key == other or key.startswith(other + os.sep) or other.startswith(key + os.sep):
            return True
    return False


def _unchanged(path: str, post: dict[str, list]) -> bool:
    record = post.get(os.path.normcase(path))
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not record:
        return True
    return st.st_size == record[0] and int(st.st_mtime) == record[1]


def _extra_files(root: Path, created: list[str]) -> list[str]:
    if root.is_file():
        return []
    known = {os.path.normcase(p) for p in created}
    extra: list[str] = []
    for dirpath, _dirnames, filenames in walk_pruned(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.normcase(full) not in known:
                extra.append(full)
    return extra


def _purge_empty_subdirs(directory: Path) -> None:
    """递归删除 directory 内部的空子目录（深度倒序）。

    只删空目录，不碰任何文件：只要树里有文件，其各级父目录自然删不掉。
    复制模式会在专辑目录里重建 BD_ROM/Scans/ 等内部层级（文件已随撤销
    删除），这些内部空壳目录不逐个记录，随所属记录目录一并清理。
    """
    for dirpath, dirnames, _files in sorted(walk_pruned(directory),
                                            key=lambda x: -len(x[0])):
        for name in dirnames:
            try:
                os.rmdir(Path(dirpath) / name)
            except OSError:
                continue


def _cleanup_created_dirs(action: UndoAction, journal: Journal,
                          op_id: str) -> tuple[list[str], list[dict]]:
    """按操作日志清理本次创建、现已为空的目标目录（深度倒序，只删空目录）。

    - 目录已不存在 → 跳过（例如整树已随移动撤销搬回原位）；
    - 目录非空（用户后来新增/其他操作复用/本次未删净）→ 保留并记录原因；
    - 权限错误等其他 OSError → 保留并记录，继续处理其余目录。
    绝不删除记录之外、原本存在或目标根本身。
    """
    removed: list[str] = []
    kept: list[dict] = []
    for path in sorted(action.dirs_delete, key=lambda p: -len(Path(p).parts)):
        try:
            directory = Path(path)
            if not directory.exists():
                journal.write("undo_dir_skip", op_id=op_id, directory=path,
                              reason="already-gone")
                continue
            _purge_empty_subdirs(directory)
            if fsops.count_dir_entries(directory) != 0:
                kept.append({"path": path, "reason": "目录非空（含后来新增或未删除的内容）"})
                journal.write("undo_dir_kept", op_id=op_id, directory=path,
                              reason="non-empty")
                continue
            directory.rmdir()
            removed.append(path)
            journal.write("undo_dir_removed", op_id=op_id, directory=path)
        except OSError as exc:
            kept.append({"path": path, "reason": f"删除失败：{exc.strerror or exc}"})
            journal.write("undo_dir_kept", op_id=op_id, directory=path,
                          reason=str(exc))
    return removed, kept