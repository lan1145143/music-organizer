"""第二阶段：撤销时清理"本次创建且已不再需要"的目标空目录。

依据执行日志记录的 created_dirs（非推测）：
- manifest 必须记录每个操作实际创建的目录（路径/是否原本存在/操作 ID）；
- 移动撤销：先恢复来源，再深度倒序清理本次创建的空目录（目标根本身不删）；
- 复制撤销：只删本次创建且未修改的文件，然后清理空目录；
- 原本存在的艺术家/专辑目录绝不删除；
- 目录里有后来新增内容 → 保留并记录原因。
"""

from __future__ import annotations

from pathlib import Path

from app.domain import naming
from app.operations.executor import Executor
from app.operations.history import HistoryStore
from app.operations.undo import UndoService
from app.planning.plan import PlanBuilder, PlanOptions
from app.scan.scanner import Scanner
from app.domain.models import SourceRoot
from tests.fixtures import build_full_rip_album


def prepare_album(tmp_path: Path):
    source_root = tmp_path / "src"
    album_dir = build_full_rip_album(source_root)
    outcome = Scanner().scan_source(SourceRoot(path=str(source_root)))
    unit = [u for u in outcome.units if u.is_album][0]
    unit.boundary_confirmed = True
    unit.checked = True
    return unit, album_dir


def execute(tmp_path: Path, unit, mode: str):
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "target"), mode=mode)) \
        .build([unit])
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    return plan


def artist_dir_name(unit) -> str:
    return naming.sanitize_artist(naming.display_artist(unit))


def test_manifest_records_created_dirs(tmp_path):
    """执行记录必须包含本次创建的目录：路径/是否原本存在/归属操作。"""
    unit, album_dir = prepare_album(tmp_path)
    plan = execute(tmp_path, unit, mode="move")
    target_album = Path(plan.items[0].target)
    run = HistoryStore(tmp_path / "history").list_runs()[0]
    records = run.ops[0].created_dirs
    paths = {Path(r["path"]) for r in records}
    assert target_album in paths
    assert target_album.parent in paths          # 艺术家目录
    for r in records:
        assert r["existed_before"] is False
        assert r["op_id"] == plan.items[0].id
        assert r.get("created_at")


def test_undo_move_cleans_created_empty_dirs(tmp_path):
    unit, album_dir = prepare_album(tmp_path)
    plan = execute(tmp_path, unit, mode="move")
    target_album = Path(plan.items[0].target)
    artist_dir = target_album.parent
    target_root = Path(plan.options.target_root)

    store = HistoryStore(tmp_path / "history")
    run = store.list_runs()[0]
    service = UndoService(store)
    undo = service.build(run)
    action = undo.actions[0]
    assert action.status == "ready"
    assert target_album in [Path(p) for p in action.dirs_delete]
    assert artist_dir in [Path(p) for p in action.dirs_delete]
    assert action.dirs_kept == []

    service.apply(undo)
    assert album_dir.is_dir()                    # 来源已恢复
    assert not target_album.exists()
    assert not artist_dir.exists()               # 本次创建的空艺术家目录已清理
    assert target_root.exists()                  # 目标根本身绝不删除


def test_undo_move_keeps_preexisting_artist_dir(tmp_path):
    """撤销前已存在的艺术家目录绝不删除（其中的本次专辑目录正常清理）。"""
    unit, album_dir = prepare_album(tmp_path)
    target_root = tmp_path / "target"
    pre_dir = target_root / artist_dir_name(unit)
    pre_dir.mkdir(parents=True)
    (pre_dir / "已有资料.txt").write_text("pre", encoding="utf-8")

    plan = PlanBuilder(PlanOptions(target_root=str(target_root), mode="move")) \
        .build([unit])
    Executor(tmp_path / "history").execute(plan)
    target_album = Path(plan.items[0].target)
    assert target_album.is_dir()

    store = HistoryStore(tmp_path / "history")
    run = store.list_runs()[0]
    service = UndoService(store)
    undo = service.build(run)
    assert undo.counts()["ready"] == 1
    service.apply(undo)

    assert not target_album.exists()             # 本次创建的专辑目录已清理
    assert pre_dir.is_dir()                      # 原本存在的艺术家目录保留
    assert (pre_dir / "已有资料.txt").read_text(encoding="utf-8") == "pre"


def test_undo_move_conflict_keeps_dir_and_new_file(tmp_path):
    """目标里出现后来新增文件 → 撤销冲突，目录与新文件原样保留。"""
    unit, album_dir = prepare_album(tmp_path)
    plan = execute(tmp_path, unit, mode="move")
    target_album = Path(plan.items[0].target)
    foreign = target_album / "用户后来放的文件.txt"
    foreign.write_text("mine", encoding="utf-8")

    store = HistoryStore(tmp_path / "history")
    run = store.list_runs()[0]
    service = UndoService(store)
    undo = service.build(run)
    assert undo.counts()["conflict"] == 1
    assert undo.counts()["ready"] == 0
    action = undo.actions[0]
    assert action.dirs_delete == []              # 冲突项不做任何删除

    service.apply(undo)
    assert target_album.is_dir()
    assert foreign.read_text(encoding="utf-8") == "mine"


def test_undo_copy_cleans_files_then_empty_dirs(tmp_path):
    """复制撤销：先删本次创建且未修改的文件，再清理本次创建的空目录。"""
    unit, album_dir = prepare_album(tmp_path)
    plan = execute(tmp_path, unit, mode="copy")
    target_album = Path(plan.items[0].target)
    artist_dir = target_album.parent
    assert target_album.is_dir()

    store = HistoryStore(tmp_path / "history")
    run = store.list_runs()[0]
    service = UndoService(store)
    undo = service.build(run)
    assert undo.counts()["ready"] == 1
    service.apply(undo)

    assert not target_album.exists()
    assert not artist_dir.exists()
    assert Path(plan.options.target_root).exists()
    assert album_dir.is_dir()                    # 来源未被复制模式动过


def test_undo_copy_keeps_dir_with_new_content(tmp_path):
    """复制撤销：目录里有用户后来新增的文件 → 目录保留并给出原因。"""
    unit, album_dir = prepare_album(tmp_path)
    plan = execute(tmp_path, unit, mode="copy")
    target_album = Path(plan.items[0].target)
    foreign = target_album / "备注.txt"
    foreign.write_text("keep me", encoding="utf-8")

    store = HistoryStore(tmp_path / "history")
    run = store.list_runs()[0]
    service = UndoService(store)
    undo = service.build(run)
    assert undo.counts()["ready"] == 1
    service.apply(undo)

    assert target_album.is_dir()                 # 非空 → 保留
    assert foreign.read_text(encoding="utf-8") == "keep me"
    # 撤销记录里说明了保留原因
    undo_run = store.list_runs()[0]
    assert any("保留" in n for n in undo_run.notes)


def test_undo_preview_lists_dirs(tmp_path):
    """撤销预览包含"将清理的空目录"与"不会删除的目录"分类。"""
    unit, album_dir = prepare_album(tmp_path)
    plan = execute(tmp_path, unit, mode="move")
    target_album = Path(plan.items[0].target)

    store = HistoryStore(tmp_path / "history")
    run = store.list_runs()[0]
    undo = UndoService(store).build(run)
    action = undo.actions[0]
    # 预览字段：计划清理 + 不删除（含原因）两个列表都可渲染
    assert isinstance(action.dirs_delete, list) and action.dirs_delete
    assert isinstance(action.dirs_kept, list)
