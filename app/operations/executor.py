"""执行整理计划：逐项移动/复制、附件、CUE 修正、原包归档，并记录可撤销的操作历史。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..domain.models import OpKind, PlanItem, PlanStatus, TaskResult
from ..planning.plan import Plan
from ..scan.cue import encode_cue, parse_cue, rewrite_file_references
from . import fsops
from .journal import Journal, now_stamp

ProgressFn = Callable[[str, int, int], None]
CancelFn = Callable[[], bool]


@dataclass
class RunSummary:
    run_id: str = ""
    run_dir: str = ""
    started_at: str = ""
    finished_at: str = ""
    mode: str = "move"
    target_root: str = ""
    results: list[TaskResult] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    manifest_path: str = ""
    notes: list[str] = field(default_factory=list)   # 未随本次执行移动的提醒（如待关联歌词）

    @property
    def success(self) -> int:
        return self.counts.get("success", 0)

    @property
    def failed(self) -> int:
        return self.counts.get("failed", 0) + self.counts.get("partial", 0)

    def summary_text(self) -> str:
        return (f"成功 {self.counts.get('success', 0)} 项，失败 {self.counts.get('failed', 0)} 项，"
                f"部分完成 {self.counts.get('partial', 0)} 项，跳过 {self.counts.get('skipped', 0)} 项，"
                f"取消 {self.counts.get('cancelled', 0)} 项")


class Executor:
    def __init__(self, history_root: Path, verify_hash: bool = True) -> None:
        self.history_root = Path(history_root)
        self.verify_hash = verify_hash
        self.cancel_requested = False

    # ------------------------------------------------------------------
    def execute(self, plan: Plan, progress: ProgressFn | None = None,
                cancel: CancelFn | None = None) -> RunSummary:
        run_id = now_stamp()
        run_dir = self.history_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        summary = RunSummary(run_id=run_id, run_dir=str(run_dir),
                             started_at=datetime.now().isoformat(timespec="seconds"),
                             mode=plan.options.mode, target_root=plan.options.target_root)
        items = [i for i in plan.items if i.selected and i.status == PlanStatus.READY.value]
        total_bytes = 0
        for item in items:
            total_bytes += plan.bytes_for(item)
        done_bytes = 0
        # 未关联/未勾选/待确认的歌词：执行后明确提醒，避免以为已经一起整理
        for entry in plan.excluded:
            if entry.get("status") in ("歌词待关联", "歌词未勾选"):
                summary.notes.append(
                    f"{entry['name']}：{entry.get('reason', '')}（歌词保留在来源目录）")
            elif entry.get("status") in ("来源失联（跳过）", "来源失联"):
                summary.notes.append(
                    f"{entry['name']}：来源失联，本次跳过（其余项目不受影响）")

        with Journal(run_dir) as journal:
            journal.write("run_start", run_id=run_id, mode=plan.options.mode,
                          target_root=plan.options.target_root, items=len(items),
                          total_bytes=total_bytes)
            for index, item in enumerate(items, start=1):
                if (cancel is not None and cancel()) or self.cancel_requested:
                    result = TaskResult(item_id=item.id, op=item.op, status="cancelled",
                                        source=item.source, target=item.target,
                                        message="已取消，未启动该项")
                    summary.results.append(result)
                    journal.write("item_cancelled", op_id=item.id, label=item.label)
                    continue
                label = item.label
                item_bytes = plan.bytes_for(item)

                def report(cur: int, total: int, name: str) -> None:
                    if progress is not None:
                        progress(f"[{index}/{len(items)}] {label} · {name}",
                                 done_bytes + cur, total_bytes)

                result = self._run_item(item, journal, run_dir, report, cancel)
                done_bytes += item_bytes
                if progress is not None:
                    progress(f"[{index}/{len(items)}] {label}", done_bytes, total_bytes)
                summary.results.append(result)
                self._save_manifest(summary, run_dir, finished=False)

            summary.finished_at = datetime.now().isoformat(timespec="seconds")
            counts: dict[str, int] = {}
            for result in summary.results:
                counts[result.status] = counts.get(result.status, 0) + 1
            summary.counts = counts
            journal.write("run_finished", counts=counts,
                          finished_at=summary.finished_at)
        self._save_manifest(summary, run_dir, finished=True)
        summary.manifest_path = str(run_dir / "manifest.json")
        return summary

    # ------------------------------------------------------------------
    def _run_item(self, item: PlanItem, journal: Journal, run_dir: Path,
                  progress: Callable[[int, int, str], None],
                  cancel: CancelFn | None) -> TaskResult:
        result = TaskResult(item_id=item.id, op=item.op, source=item.source,
                            target=item.target)
        started = time.time()
        journal.write("item_intent", op_id=item.id, label=item.label, op=item.op,
                      source=item.source, target=item.target, action=item.action,
                      details=item.details[:10])
        try:
            if item.op == OpKind.CUE_FIX.value:
                outcome = self._do_cue_fix(item, run_dir, journal)
            elif item.op == OpKind.ARCHIVE_ARCHIVE.value:
                outcome = self._do_transfer(Path(item.source), Path(item.target),
                                           item.action, progress, cancel, journal, item, is_dir=False)
            else:
                is_dir = item.op in (OpKind.ALBUM_MOVE.value, OpKind.DUPLICATE_MOVE.value) and \
                    Path(item.source).is_dir()
                outcome = self._do_transfer(Path(item.source), Path(item.target), item.action,
                                           progress, cancel, journal, item, is_dir=is_dir)
                if outcome.status == "success" and item.op == OpKind.TRACK_MOVE.value:
                    att_results, att_created, lyrics_records = self._sync_attachments(
                        item, journal, cancel, outcome)
                    outcome.created.extend(att_created)
                    if lyrics_records:
                        outcome.extra["lyrics"] = lyrics_records
                    if att_results:
                        outcome.message = (outcome.message + "；" if outcome.message else "") + att_results
                if outcome.status == "success" and item.op == OpKind.ALBUM_MOVE.value:
                    # 连带附属资料：随整夹一起搬入目标目录（失败只记录不回滚）
                    comp_created, comp_records, comp_notes = self._sync_companions(
                        item, journal, cancel)
                    outcome.created.extend(comp_created)
                    if comp_records:
                        outcome.extra["companions_moved"] = comp_records
                    if comp_notes:
                        outcome.message = (outcome.message + "｜" if outcome.message else "") + comp_notes
            result.created = outcome.created
            result.removed = outcome.removed
            result.status = outcome.status
            result.message = outcome.message
            result.bytes_done = outcome.bytes_done
            result.extra = dict(outcome.extra or {})
            if item.op in (OpKind.ALBUM_MOVE.value, OpKind.TRACK_MOVE.value,
                           OpKind.DUPLICATE_MOVE.value):
                unit = item.extra.get("unit")
                if unit is not None:
                    unit.last_target = item.target
            # 移动成功后清理来源空目录（仅 move 模式；散曲看父目录，整夹看边界父目录）
            if item.action == "move" and result.status == "success" and \
                    item.op != OpKind.CUE_FIX.value:
                self._cleanup_source_dirs(item, result, journal)
        except fsops.CancelledError:
            result.status = "cancelled"
            # fsops 在取消时会删除本次复制产生的半成品文件（见 _copy_file/
            # copy_tree），这里如实描述，不得谎称清理了未清理的内容
            result.message = "已取消，当前项未完成；本次复制产生的临时文件已清理"
            journal.write("item_cancelled", op_id=item.id, label=item.label)
        except OSError as exc:
            result.status = "failed"
            result.message = f"{type(exc).__name__}: {exc.strerror or exc}"
            journal.write("item_failed", op_id=item.id, label=item.label, error=result.message)
        except Exception as exc:  # noqa: BLE001 - 单项失败不影响其他项
            result.status = "failed"
            result.message = f"{type(exc).__name__}: {exc}"
            journal.write("item_failed", op_id=item.id, label=item.label, error=result.message)
        result.message = (result.message + f"｜耗时 {time.time() - started:.1f}s").strip("｜")
        journal.write("item_done", op_id=item.id, status=result.status,
                      created=result.created[:200], removed=result.removed[:200],
                      message=result.message)
        return result

    # ------------------------------------------------------------------
    def _do_transfer(self, source: Path, target: Path, action: str, progress, cancel,
                     journal: Journal, item: PlanItem, is_dir: bool) -> TaskResult:
        if not source.exists():
            raise OSError(f"来源不存在：{source}")
        # 计划期已校验目标不存在；执行前重查一次，堵住计划与执行之间的窗口：
        # 同盘 os.replace 会覆盖已有文件、复制写入会截断/合并同名内容，绝不覆盖任何文件
        if target.exists():
            raise OSError(f"目标已存在，为避免覆盖已停止：{target}")
        # 记录本次操作实际创建的目录（撤销时据此清理空目录，不做推测性删除）
        created_dirs = self._record_created_dirs(
            [target.parent], item, journal, phase="transfer")
        if is_dir:
            created_dirs.append(self._dir_record(target, item, "transfer"))
        expected: dict[str, tuple[int, int]] = {}
        if is_dir:
            for path in fsops.tree_files(source):
                try:
                    expected[os.path.normcase(str(path))] = fsops.file_state(path)
                except OSError:
                    continue
        else:
            try:
                expected[os.path.normcase(str(source))] = fsops.file_state(source)
            except OSError:
                pass

        journal.write("transfer_start", op_id=item.id, source=str(source),
                      target=str(target), action=action, is_dir=is_dir,
                      expected=len(expected))
        if action == "copy":
            result = (fsops.copy_tree(source, target, progress, cancel, self.verify_hash)
                      if is_dir else
                      fsops.copy_file_op(source, target, progress, cancel, self.verify_hash))
            status = "success" if result.verified else "failed"
            if not result.verified:
                result.message = result.message or "复制校验未通过"
        else:
            if is_dir:
                result = fsops.move_tree(source, target, progress, cancel, self.verify_hash,
                                         expected)
            else:
                result = fsops.move_file(source, target, progress, cancel, self.verify_hash)
            if result.verified and result.source_left:
                status = "partial"
            elif result.verified:
                status = "success"
            else:
                status = "failed"
            if status == "partial" and not result.message:
                result.message = "目标已就绪，来源未完全移除"
        journal.write("transfer_done", op_id=item.id, status=status,
                      bytes=result.bytes_done, verified=result.verified,
                      created=len(result.created), removed=len(result.removed),
                      source_left=result.source_left[:20], message=result.message)
        return TaskResult(item_id=item.id, op=item.op, status=status, source=str(source),
                          target=str(target), message=result.message,
                          created=list(result.created), removed=list(result.removed),
                          bytes_done=result.bytes_done,
                          extra={"created_dirs": created_dirs})

    # ------------------------------------------------------------------
    @staticmethod
    def _dir_record(path, item: PlanItem, phase: str) -> dict:
        """一条目录创建记录：绝对路径 + 是否原本存在 + 归属操作 + 阶段时间。"""
        return {
            "path": str(path),
            "existed_before": False,
            "op_id": item.id,
            "op": item.op,
            "item_id": item.unit_id,
            "phase": phase,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _record_created_dirs(self, dirs, item: PlanItem, journal: Journal,
                             phase: str) -> list[dict]:
        """逐个确保父目录存在并记录本次新建的部分（撤销清理的依据）。"""
        records: list[dict] = []
        for directory in dirs:
            created = fsops.ensure_parents_recorded(Path(directory))
            for path in created:
                record = self._dir_record(path, item, phase)
                records.append(record)
                journal.write("dir_created", op_id=item.id, directory=str(path),
                              existed_before=False, stage=phase)
        return records

    # ------------------------------------------------------------------
    def _sync_companions(self, item: PlanItem, journal: Journal,
                         cancel: CancelFn | None) -> tuple[list[str], list[dict], str]:
        """连带附属资料：跟随整夹移动/复制到其目标目录根部（原样保留）。

        目标同名自动改名不覆盖；单个失败只记录到 journal，不影响整夹结果。
        返回 (created 路径, 记录列表, 提示文本)；记录同时进
        outcome.extra["companions_moved"] 供 manifest 与撤销恢复。
        """
        companions = [p for p in (item.extra.get("companions") or []) if Path(p).is_file()]
        if not companions:
            return [], [], ""
        dest_dir = Path(item.target)
        created: list[str] = []
        moved: list[dict] = []
        failures: list[str] = []
        for src in companions:
            src_path = Path(src)
            if cancel is not None and cancel():
                break
            dst = dest_dir / src_path.name
            unique = _unique(dst)
            renamed = os.path.normcase(str(unique)) != os.path.normcase(str(dst))
            try:
                if item.action == "copy":
                    fsops.copy_file_op(src_path, unique, None, cancel, self.verify_hash)
                else:
                    fsops.move_file(src_path, unique, None, cancel, self.verify_hash)
                created.append(str(unique))
                moved.append({"source": str(src_path), "target": str(unique),
                              "renamed": renamed})
                journal.write("companion_done", op_id=item.id, source=str(src_path),
                              target=str(unique), renamed=renamed)
            except fsops.CancelledError:
                raise
            except OSError as exc:
                failures.append(f"{src_path.name}: {exc.strerror or exc}")
                journal.write("companion_failed", op_id=item.id, source=str(src_path),
                              error=str(exc))
        note = ""
        if moved:
            note = f"连带附属资料 {len(moved)} 个已随整夹处理"
        if failures:
            note += (f"；{len(failures)} 个失败：" + "、".join(failures[:3]))
        return created, moved, note

    # ------------------------------------------------------------------
    def _sync_attachments(self, item: PlanItem, journal: Journal,
                          cancel: CancelFn | None,
                          outcome: TaskResult | None = None) -> tuple[str, list[str], list[dict]]:
        """散曲附件：封面/歌词放专辑根目录、扫描图放 Scans/、日志放所在目录。

        歌词只做移动/复制，不改写内容、不转码、不写入音频标签；
        目标同名时不覆盖，自动改名并记录。新建目录（如 Scans/）记入
        outcome.extra["created_dirs"]，供撤销清理。
        """
        attachments = item.extra.get("attachments") or []
        if not attachments:
            return "", [], []
        album_dir = Path(item.target).parent
        created: list[str] = []
        notes: list[str] = []
        lyrics_records: list[dict] = []
        lyrics_count = 0
        failures = 0
        for att in attachments:
            src = Path(att.get("path", ""))
            role = att.get("role", "misc")
            if role == "cue":
                # CUE 由"引用修正"子操作统一处理（改写后再移动）
                continue
            if not src.is_file():
                notes.append(f"{src.name} 不存在已跳过")
                continue
            if role == "scans":
                dst = album_dir / "Scans" / src.name
            else:
                dst = album_dir / src.name
            if outcome is not None:
                outcome.extra.setdefault("created_dirs", []).extend(
                    self._record_created_dirs([dst.parent], item, journal,
                                              phase="attachment"))
            renamed = False
            unique = _unique(dst)
            if os.path.normcase(str(unique)) != os.path.normcase(str(dst)):
                renamed = True
            dst = unique
            try:
                if (cancel is not None and cancel()):
                    raise fsops.CancelledError()
                journal.write("attachment_start", op_id=item.id, source=str(src),
                              target=str(dst), role=role)
                if item.action == "copy":
                    fsops.copy_file_op(src, dst, None, cancel, self.verify_hash)
                else:
                    fsops.move_file(src, dst, None, cancel, self.verify_hash)
                created.append(str(dst))
                if role == "lyrics":
                    lyrics_count += 1
                    lyrics_records.append({
                        "source": str(src), "target": str(dst),
                        "encoding": att.get("lyrics_encoding", ""),
                        "mode": item.action, "renamed": renamed,
                    })
                    if renamed:
                        notes.append(f"歌词目标已存在同名文件，{src.name} 已改名为 {dst.name}")
                journal.write("attachment_done", op_id=item.id, target=str(dst), role=role)
            except fsops.CancelledError:
                raise
            except OSError as exc:
                failures += 1
                notes.append(f"{src.name} 附件处理失败：{exc.strerror or exc}")
                journal.write("attachment_failed", op_id=item.id, source=str(src),
                              error=str(exc))
        message = ""
        if lyrics_count:
            message = f"歌词 {lyrics_count} 个已随歌曲{'复制' if item.action == 'copy' else '移动'}"
        other = len(created) - lyrics_count
        if other:
            message = (message + "；" if message else "") + f"附件 {other} 个已随曲目整理"
        if failures:
            message = (message + "；" if message else "") + "；".join(notes)
        return message, created, lyrics_records

    # ------------------------------------------------------------------
    def _cleanup_source_dirs(self, item: PlanItem, result: TaskResult,
                             journal: Journal) -> None:
        """移动成功后检查来源目录：为空则移除（仅目录本身，不递归不上溯），
        有剩余内容则保留。处理结果记入 result.extra 与 journal，供结果页
        显示与撤销时恢复目录结构。"""
        from . import fsops
        source = Path(item.source)
        # 移动完成后来源文件/目录已不在原位：散曲检查其父目录；
        # 整夹移动其来源目录本身已随目标走，检查其父目录
        candidates: list[Path] = []
        if item.op == OpKind.ALBUM_MOVE.value and item.extra.get("boundary"):
            candidates.append(Path(item.extra["boundary"]).parent)
        else:
            candidates.append(source.parent)
            if item.extra.get("boundary"):
                candidates.append(Path(item.extra["boundary"]).parent)
        removed: list[str] = []
        kept_remaining: list[str] = []
        for directory in candidates:
            # 只处理来源根内部的目录：绝不顺带清理驱动器根/来源根本身
            if not directory.is_dir():
                continue
            if fsops.count_dir_entries(directory) == 0:
                if fsops.remove_single_empty_dir(directory):
                    removed.append(str(directory))
                    journal.write("source_dir_removed", op_id=item.id,
                                  directory=str(directory))
                    continue
            kept_remaining.append(str(directory))
            journal.write("source_dir_kept", op_id=item.id, directory=str(directory),
                          entries=fsops.count_dir_entries(directory))
        if removed or kept_remaining:
            result.extra["source_dirs"] = {
                "removed": removed, "kept_remaining": kept_remaining}
            if removed:
                note = "来源空目录已移除：" + "、".join(Path(p).name for p in removed)
                result.message = (result.message + "｜" if result.message else "") + note
            if kept_remaining:
                note = "来源文件夹仍有未整理内容，因此已保留"
                result.message = (result.message + "｜" if result.message else "") + note

    # ------------------------------------------------------------------
    def _do_cue_fix(self, item: PlanItem, run_dir: Path, journal: Journal) -> TaskResult:
        cue_path = Path(item.extra.get("cue") or item.source)
        mapping = item.extra.get("mapping") or {}
        if not cue_path.is_file():
            raise OSError(f"CUE 不存在：{cue_path}")
        sheet = parse_cue(cue_path)
        if sheet.error and "编码" in sheet.error:
            return TaskResult(item_id=item.id, op=item.op, status="failed",
                              source=str(cue_path), target=item.target,
                              message="CUE 编码无法可靠保持，已标记待手动处理，未改写")
        new_text, changes = rewrite_file_references(sheet, mapping)
        if not changes:
            return TaskResult(item_id=item.id, op=item.op, status="skipped",
                              source=str(cue_path), target=item.target,
                              message="引用无需修改")
        backups = run_dir / "backups"
        backups.mkdir(exist_ok=True)
        backup = backups / (cue_path.name + ".orig")
        index = 2
        while backup.exists():
            backup = backups / f"{cue_path.name}.orig{index}"
            index += 1
        original = cue_path.read_bytes()
        backup.write_bytes(original)
        data = encode_cue(new_text, sheet.encoding, sheet.bom, sheet.newline)
        tmp = cue_path.with_suffix(cue_path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, cue_path)
        journal.write("cue_fix", op_id=item.id, cue=str(cue_path), backup=str(backup),
                      changes=changes)
        created = [str(cue_path)]
        removed: list[str] = []
        target = Path(item.target)
        extra = {"cue_backup": str(backup), "changes": changes}
        if os.path.normcase(str(target)) != os.path.normcase(str(cue_path)):
            extra["created_dirs"] = self._record_created_dirs(
                [target.parent], item, journal, phase="cue_fix")
            target = _unique(target)
            if item.action == "copy":
                fsops.copy_file_op(cue_path, target, None, None, self.verify_hash)
            else:
                fsops.move_file(cue_path, target, None, None, self.verify_hash)
                removed.append(str(cue_path))
            created.append(str(target))
            journal.write("cue_moved", op_id=item.id, source=str(cue_path), target=str(target))
        return TaskResult(item_id=item.id, op=item.op, status="success",
                          source=str(cue_path), target=str(target),
                          message="CUE 引用已修正：" + "；".join(changes),
                          created=created, removed=removed,
                          extra=extra)

    # ------------------------------------------------------------------
    def _save_manifest(self, summary: RunSummary, run_dir: Path, finished: bool) -> None:
        data = {
            "run_id": summary.run_id,
            "started_at": summary.started_at,
            "finished_at": summary.finished_at if finished else "",
            "mode": summary.mode,
            "target_root": summary.target_root,
            "status": "done" if finished else "running",
            "counts": summary.counts,
            "notes": list(summary.notes),
            "ops": [
                {
                    "op_id": r.item_id, "op": r.op, "status": r.status,
                    "source": r.source, "target": r.target, "message": r.message,
                    "created": r.created, "removed": r.removed,
                    "post": _post_state(r.created),
                    "cue_backup": (r.extra or {}).get("cue_backup", ""),
                    "source_dirs": (r.extra or {}).get("source_dirs", {}),
                    "lyrics": (r.extra or {}).get("lyrics", []),
                    "companions": (r.extra or {}).get("companions_moved", []),
                    "created_dirs": (r.extra or {}).get("created_dirs", []),
                }
                for r in summary.results
            ],
        }
        tmp = run_dir / "manifest.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, run_dir / "manifest.json")


def _post_state(paths: list[str]) -> dict[str, list]:
    """记录执行后目标文件状态，供撤销时校验"未被后续修改"。"""
    state: dict[str, list] = {}
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        state[os.path.normcase(path)] = [st.st_size, int(st.st_mtime)]
    return state


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    index = 2
    while True:
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
        index += 1
