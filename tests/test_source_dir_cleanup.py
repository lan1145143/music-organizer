"""移动后来源文件夹清理：空目录移除、剩余内容保留、失败不清理、撤销恢复。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.domain.models import OpKind, PlanStatus, SourceRoot, Unit, UnitKind, UnitStatus
from app.operations.executor import Executor
from app.operations.fsops import count_dir_entries
from app.operations.history import HistoryStore
from app.operations.undo import UndoService
from app.planning.plan import PlanBuilder, PlanOptions
from app.scan.scanner import Scanner
from tests.fixtures import build_full_rip_album, make_wav


def prepare_album(tmp_path: Path, source_root: Path):
    album_dir = build_full_rip_album(source_root)
    outcome = Scanner().scan_source(SourceRoot(path=str(source_root)))
    unit = [u for u in outcome.units if u.is_album][0]
    unit.boundary_confirmed = True
    unit.checked = True
    return unit, album_dir


def build_plan(tmp_path: Path, units, mode="move"):
    opts = PlanOptions(target_root=str(tmp_path / "target"), mode=mode)
    return PlanBuilder(opts).build(units)


def test_album_move_removes_empty_source_parent(tmp_path):
    """整夹移动成功：若其上级目录因此变空，移除并记录。"""
    src = tmp_path / "src"
    unit, album_dir = prepare_album(tmp_path, src)
    # build_full_rip_album 会在 src 下再建一层专辑目录，unit 边界即该层
    wrapper = Path(unit.boundary_path).parent          # src（计划检查的父目录）
    assert wrapper != Path(unit.boundary_path)
    plan = build_plan(tmp_path, [unit], mode="move")
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    # 专辑目录已随目标走
    assert not Path(unit.boundary_path).exists()
    # 其父目录 src 因此变空 → 移除
    assert not src.exists()
    record = HistoryStore(tmp_path / "history").list_runs()[0]
    op = record.ops[0]
    assert any(Path(p).name == "src" for p in op.source_dirs.get("removed", []))


def test_album_move_bundles_parent_accessory(tmp_path):
    """全自动：上级的分散附属文件（txt）随整夹连带打包进目标目录。"""
    src = tmp_path / "src"
    unit, album_dir = prepare_album(tmp_path, src)
    wrapper = Path(unit.boundary_path).parent          # src
    (wrapper / "未整理.txt").write_text("leftover", encoding="utf-8")
    plan = build_plan(tmp_path, [unit], mode="move")
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    target = Path(unit.last_target)
    assert (target / "未整理.txt").is_file(), "连带附属资料应随整夹进入目标"
    assert not (wrapper / "未整理.txt").exists()
    record = HistoryStore(tmp_path / "history").list_runs()[0]
    op = record.ops[0]
    moved = op.companions
    assert moved and Path(moved[0]["source"]).name == "未整理.txt"


def scan_track(tmp_path: Path, src: Path, name: str) -> Unit:
    """扫描单个 wav 并补齐必需信息，使其可执行。"""
    wav = src / name
    make_wav(wav, seconds=0.2)
    outcome = Scanner().scan_source(SourceRoot(path=str(src)))
    track = [u for u in outcome.units if u.is_track and u.path == str(wav)][0]
    track.title = track.title or name
    track.artist = track.artist or "歌手"
    track.status = UnitStatus.READY.value
    track.checked = True
    return track


def test_track_move_removes_parent_only_if_empty(tmp_path):
    """散曲移动：原父目录变空才移除，有兄弟文件则保留。"""
    src = tmp_path / "src"
    track = scan_track(tmp_path, src, "a.wav")
    # 同目录另一个未勾选文件
    make_wav(src / "b.wav", seconds=0.2)

    plan = build_plan(tmp_path, [track], mode="move")
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    assert not (src / "a.wav").exists()
    assert (src / "b.wav").is_file()  # 未整理内容
    assert src.is_dir()               # 目录保留
    record = HistoryStore(tmp_path / "history").list_runs()[0]
    op = record.ops[0]
    assert op.source_dirs.get("kept_remaining")

    # 反例:目录只有一个文件,移走后变空 → 移除（独立 history 目录避免同秒排序歧义）
    src2 = tmp_path / "src2"
    track2 = scan_track(tmp_path, src2, "only.wav")
    plan2 = build_plan(tmp_path, [track2], mode="move")
    history2 = tmp_path / "history2"
    summary2 = Executor(history2).execute(plan2)
    assert summary2.success == 1
    assert not src2.exists()
    record2 = HistoryStore(history2).list_runs()[0]
    op2 = [o for o in record2.ops if o.op == OpKind.TRACK_MOVE.value][0]
    assert any(Path(p).name == "src2" for p in op2.source_dirs.get("removed", []))


def test_copy_mode_never_removes_source_dirs(tmp_path):
    """复制模式:来源文件与文件夹始终保留,不做任何清理。"""
    src = tmp_path / "src"
    unit, album_dir = prepare_album(tmp_path, src / "[250827]专辑")
    plan = build_plan(tmp_path, [unit], mode="copy")
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    assert album_dir.exists()
    assert src.is_dir()
    record = HistoryStore(tmp_path / "history").list_runs()[0]
    op = record.ops[0]
    assert not op.source_dirs.get("removed")
    assert not op.source_dirs.get("kept_remaining")


def test_failed_task_keeps_source_dirs(tmp_path):
    """失败/取消的任务不清理来源目录。"""
    src = tmp_path / "src"
    track = scan_track(tmp_path, src, "a.wav")
    plan = build_plan(tmp_path, [track], mode="move")

    executor = Executor(tmp_path / "history")
    # 仅拦截往目标目录的移动（同盘 os.replace 路径），来源文件保持原位
    import app.operations.fsops as fsops
    target_root = str((tmp_path / "target").resolve())
    original_replace = os.replace

    def failing_replace(src, dst):
        if str(Path(dst).resolve()).startswith(target_root):
            raise OSError("模拟移动失败")
        return original_replace(src, dst)

    fsops.os.replace = failing_replace
    try:
        summary = executor.execute(plan)
    finally:
        fsops.os.replace = original_replace
    assert summary.failed >= 1
    assert (src / "a.wav").is_file()  # 来源未动
    assert src.is_dir()               # 目录未清理
    record = HistoryStore(tmp_path / "history").list_runs()[0]
    op = record.ops[0]
    assert not op.source_dirs.get("removed")


def test_cleanup_never_touches_root_or_recursive(tmp_path):
    """只删除空目录本身:不递归删除、不上溯清理父级。"""
    deep = tmp_path / "src" / "l1" / "l2"
    track = scan_track(tmp_path, deep, "only.wav")
    plan = build_plan(tmp_path, [track], mode="move")
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    # only.wav 移走后 l2 空 → 移除 l2;但只检查直接父目录,不上溯
    assert not (tmp_path / "src" / "l1" / "l2").exists()
    # l1 仍存在（不递归上溯清理）
    assert (tmp_path / "src" / "l1").is_dir()


def test_undo_restores_removed_source_dirs(tmp_path):
    """撤销时恢复执行中移除的来源目录结构。"""
    src = tmp_path / "src"
    unit, album_dir = prepare_album(tmp_path, src)
    plan = build_plan(tmp_path, [unit], mode="move")
    Executor(tmp_path / "history").execute(plan)
    assert not src.exists()  # 已被清理

    store = HistoryStore(tmp_path / "history")
    run = store.list_runs()[0]
    undo = UndoService(store).build(run)
    assert undo.ready_actions
    restored_msg = undo.ready_actions[0].message
    assert "恢复执行时移除的来源目录" in restored_msg
    UndoService(store).apply(undo)
    assert src.is_dir()          # 目录结构恢复
    assert Path(unit.boundary_path).is_dir()  # 专辑目录回到原位
