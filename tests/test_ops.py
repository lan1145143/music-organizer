import os
import time
from pathlib import Path

import pytest

from app.domain.models import PlanStatus, SourceRoot, Unit, UnitKind
from app.operations import fsops
from app.operations.executor import Executor
from app.operations.history import HistoryStore
from app.operations.undo import UndoService
from app.planning.plan import PlanBuilder, PlanOptions
from app.scan.scanner import Scanner
from tests.fixtures import build_full_rip_album, build_loose_tracks, make_wav


def tree_hashes(root: Path) -> dict[str, str]:
    result = {}
    for dirpath, _dn, filenames in os.walk(root):
        for name in filenames:
            full = Path(dirpath) / name
            result[str(full.relative_to(root))] = fsops.hash_file(full)
    return result


def prepare_album(tmp_path):
    source_root = tmp_path / "src"
    album_dir = build_full_rip_album(source_root)
    outcome = Scanner().scan_source(SourceRoot(path=str(source_root)))
    unit = [u for u in outcome.units if u.is_album][0]
    unit.boundary_confirmed = True
    unit.checked = True
    return unit, album_dir


def build_plan(tmp_path, units, mode="copy", target=None):
    opts = PlanOptions(target_root=str(target or (tmp_path / "target")), mode=mode)
    return PlanBuilder(opts).build(units)


def test_copy_album_then_undo(tmp_path):
    unit, album_dir = prepare_album(tmp_path)
    before = tree_hashes(album_dir)
    plan = build_plan(tmp_path, [unit], mode="copy")
    assert plan.items[0].status == PlanStatus.READY.value
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    target = Path(plan.items[0].target)
    assert target.is_dir()
    assert tree_hashes(target) == before
    assert album_dir.exists()
    run = HistoryStore(tmp_path / "history").list_runs()[0]
    assert run.status == "done"

    undo = UndoService(HistoryStore(tmp_path / "history")).build(run)
    assert undo.counts()["ready"] == 1
    UndoService(HistoryStore(tmp_path / "history")).apply(undo)
    assert not target.exists()
    assert album_dir.exists()
    assert tree_hashes(album_dir) == before


def test_move_album_then_undo_restores(tmp_path):
    unit, album_dir = prepare_album(tmp_path)
    before = tree_hashes(album_dir)
    plan = build_plan(tmp_path, [unit], mode="move")
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    target = Path(plan.items[0].target)
    assert not album_dir.exists()
    assert tree_hashes(target) == before

    store = HistoryStore(tmp_path / "history")
    run = store.list_runs()[0]
    service = UndoService(store)
    undo = service.build(run)
    assert undo.counts()["ready"] == 1
    service.apply(undo)
    assert album_dir.is_dir()
    assert tree_hashes(album_dir) == before
    assert not target.exists()


def test_undo_refuses_when_target_gained_new_files(tmp_path):
    unit, album_dir = prepare_album(tmp_path)
    plan = build_plan(tmp_path, [unit], mode="move")
    Executor(tmp_path / "history").execute(plan)
    target = Path(plan.items[0].target)
    (target / "later.txt").write_text("新内容", encoding="utf-8")

    store = HistoryStore(tmp_path / "history")
    service = UndoService(store)
    run = store.list_runs()[0]
    undo = service.build(run)
    action = undo.actions[0]
    assert action.status == "conflict"
    assert "新增" in action.message
    service.apply(undo)
    assert (target / "later.txt").exists()
    assert not album_dir.exists()


def test_undo_copy_keeps_modified_files(tmp_path):
    unit, album_dir = prepare_album(tmp_path)
    plan = build_plan(tmp_path, [unit], mode="copy")
    Executor(tmp_path / "history").execute(plan)
    target = Path(plan.items[0].target)
    cover = target / "cover.jpg"
    time.sleep(0.01)
    cover.write_bytes(b"changed")
    os.utime(cover, (time.time() + 5, time.time() + 5))

    store = HistoryStore(tmp_path / "history")
    service = UndoService(store)
    undo = service.build(store.list_runs()[0])
    action = undo.actions[0]
    assert action.status == "ready"
    assert action.keep_files
    service.apply(undo)
    assert cover.exists()
    assert not (target / "VVCL-2788.wav").exists()


def test_track_move_places_attachments(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    track = make_wav(source / "01 - 歌手 - 曲名.wav",
                     tags={"title": "曲名", "artist": "歌手", "album": "专辑", "track": 1})
    (source / "cover.jpg").write_bytes(b"cover")
    (source / "01 - 歌手 - 曲名.log").write_text("log", encoding="utf-8")
    scanner = Scanner()
    outcome = scanner.scan_source(SourceRoot(path=str(source)))
    unit = [u for u in outcome.units if u.is_track][0]
    unit.checked = True
    unit.album_artist = "专辑歌手"
    unit.attachments = [
        {"path": str(source / "cover.jpg"), "role": "cover", "selected": True},
        {"path": str(source / "01 - 歌手 - 曲名.log"), "role": "log", "selected": True},
    ]
    plan = build_plan(tmp_path, [unit], mode="move")
    Executor(tmp_path / "history").execute(plan)
    album_dir = Path(plan.items[0].target).parent
    assert (album_dir / "cover.jpg").exists()
    assert (album_dir / "01 - 歌手 - 曲名.log").exists()
    assert not track.exists()


def test_cue_fix_and_undo(tmp_path):
    source = tmp_path / "src"
    album = build_full_rip_album(source)
    cue = album / "VVCL-2788.cue"
    original = cue.read_bytes()
    unit = Unit(kind=UnitKind.TRACK.value, path=str(album / "VVCL-2788.wav"),
                name="VVCL-2788", title="プリズム", artist="安田レイ", album="プリズム",
                album_artist="安田レイ", track=1, year="2025", checked=True,
                attachments=[{"path": str(cue), "role": "cue", "selected": True}])
    (album / "VVCL-2788.wav").unlink()
    make_wav(album / "VVCL-2788.wav", seconds=0.3, tags={"title": "プリズム", "artist": "安田レイ"})
    plan = build_plan(tmp_path, [unit], mode="move")
    kinds = [i.op for i in plan.items]
    assert "cue_fix" in kinds
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == len(plan.ready_items)
    new_cue = [Path(i.target) for i in plan.items if i.op == "cue_fix"][0]
    assert new_cue.exists()
    text = new_cue.read_bytes().decode("utf-8")
    assert "VVCL-2788.wav" not in text or "01 - 安田レイ - プリズム.wav" in text

    store = HistoryStore(tmp_path / "history")
    service = UndoService(store)
    undo = service.build(store.list_runs()[0])
    service.apply(undo)
    restored = [a for a in undo.actions if a.cue_restore]
    assert restored
    assert cue.read_bytes() == original


def test_cross_volume_move_copies_then_deletes(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    file = make_wav(source / "a.wav", seconds=0.2)
    monkeypatch.setattr(fsops, "same_volume", lambda a, b: False)
    target = tmp_path / "dst" / "a.wav"
    result = fsops.move_file(file, target)
    assert result.verified
    assert target.exists()
    assert not file.exists()


def test_move_tree_stops_delete_when_source_changed(tmp_path, monkeypatch):
    src = tmp_path / "album"
    src.mkdir()
    make_wav(src / "a.wav", seconds=0.2)
    expected = {os.path.normcase(str(src / "a.wav")): (1, 1)}  # 故意记录错误签名
    dst = tmp_path / "out"
    monkeypatch.setattr(fsops, "same_volume", lambda a, b: False)
    result = fsops.move_tree(src, dst, expected=expected)
    assert result.verified
    assert result.source_left
    assert "变化" in result.message
    assert src.exists()


def test_same_volume_move_uses_rename(tmp_path, monkeypatch):
    src = tmp_path / "a.wav"
    src.write_bytes(b"data")
    dst = tmp_path / "b.wav"
    monkeypatch.setattr(fsops, "same_volume", lambda a, b: True)
    result = fsops.move_file(src, dst)
    assert result.verified and dst.exists() and not src.exists()


def test_cancel_before_items(tmp_path):
    unit, _ = prepare_album(tmp_path)
    plan = build_plan(tmp_path, [unit], mode="copy")
    summary = Executor(tmp_path / "history").execute(plan, cancel=lambda: True)
    assert summary.counts.get("cancelled") == 1
    assert not Path(plan.items[0].target).exists()


def test_execute_fails_when_target_appeared_after_plan(tmp_path):
    """计划与执行之间目标被占用：该项失败、来源保留、已有目标内容不被覆盖。"""
    unit, album_dir = prepare_album(tmp_path)
    before = tree_hashes(album_dir)
    plan = build_plan(tmp_path, [unit], mode="move")
    target = Path(plan.items[0].target)
    target.parent.mkdir(parents=True, exist_ok=True)
    occupied = "占用文件".encode("utf-8")  # 计划生成后、执行前，目标位置出现同名内容
    target.write_bytes(occupied)

    summary = Executor(tmp_path / "history").execute(plan)

    assert summary.counts.get("failed") == 1
    assert target.read_bytes() == occupied
    assert tree_hashes(album_dir) == before  # 来源原样保留


def test_undo_move_skips_when_target_reappeared(tmp_path):
    """撤销计划构建后目标被占用：撤销该项停止，不覆盖占用内容。"""
    unit, album_dir = prepare_album(tmp_path)
    plan = build_plan(tmp_path, [unit], mode="move")
    Executor(tmp_path / "history").execute(plan)
    assert not album_dir.exists()

    store = HistoryStore(tmp_path / "history")
    service = UndoService(store)
    undo = service.build(store.list_runs()[0])
    assert undo.counts()["ready"] == 1
    album_dir.mkdir(parents=True)   # 原位置被外部重新占用（父目录已随整理清空移除）
    (album_dir / "new.wav").write_bytes(b"new")

    service.apply(undo)
    target = Path(plan.items[0].target)
    assert target.exists()          # 回写被停止，目标内容仍在
    assert (album_dir / "new.wav").exists()  # 占用内容未被覆盖


def test_progress_callback_matches_ui_signal_arity(tmp_path):
    """UI 的 Qt 信号是严格 3 参 (message, current, total)。

    回归：executor/undo 曾用 5 参回调，UI 执行时报
    "progress(QString,int,int) only accepts 3 argument(s), 5 given!"。
    """
    unit, _ = prepare_album(tmp_path)
    plan = build_plan(tmp_path, [unit], mode="copy")

    def strict_emit(message, current, total):
        assert isinstance(message, str)
        assert isinstance(current, int)
        assert isinstance(total, int)

    summary = Executor(tmp_path / "history").execute(plan, progress=strict_emit)
    assert summary.counts.get("success") == 1

    run = HistoryStore(tmp_path / "history").list_runs()[0]
    undo_plan = UndoService(HistoryStore(tmp_path / "history")).build(run)
    if undo_plan.ready_actions:
        UndoService(HistoryStore(tmp_path / "history")).apply(undo_plan,
                                                             progress=strict_emit)


def test_copy_hash_verify_passes_identical(tmp_path):
    """verify_hash（默认开）：同内容复制校验通过。"""
    src = tmp_path / "a.wav"
    src.write_bytes(bytes(range(256)) * 64)
    result = fsops.copy_file_op(src, tmp_path / "b.wav", verify=True)
    assert result.verified


def test_copy_hash_verify_detects_content_change(tmp_path, monkeypatch):
    """复制校验须比对内容摘要：同大小的损坏/篡改不得漏检（verify_hash 名副其实）。"""
    src = tmp_path / "a.wav"
    src.write_bytes(b"A" * 8192)
    # 模拟目标端内容被篡改（大小相同、内容不同）：校验读到不一致的摘要
    monkeypatch.setattr(fsops, "_file_digest", lambda path, cancel=None: "deadbeef")
    result = fsops.copy_file_op(src, tmp_path / "b.wav", verify=True)
    assert not result.verified
    assert "哈希" in result.message


def test_copy_tree_hash_verify_detects_content_change(tmp_path, monkeypatch):
    src = tmp_path / "album"
    src.mkdir()
    (src / "a.wav").write_bytes(b"X" * 8192)
    (src / "sub").mkdir()
    (src / "sub" / "b.wav").write_bytes(b"Y" * 8192)
    real_digest = fsops._file_digest

    def tampered(path, cancel=None):
        # 子目录里的文件校验时返回错误摘要，模拟传输损坏
        if str(path).endswith("b.wav"):
            return real_digest(tmp_path / "zero")
        return real_digest(path, cancel)

    (tmp_path / "zero").write_bytes(b"\x00" * 8192)
    monkeypatch.setattr(fsops, "_file_digest", tampered)
    result = fsops.copy_tree(src, tmp_path / "out", verify=True)
    assert not result.verified
    assert "哈希" in result.message


def test_tree_files_skips_junction(tmp_path):
    """tree_files（复制/移动的文件清单）不进入目录联接。"""
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.txt").write_text("x", encoding="utf-8")
    import subprocess
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "loop"),
                             str(tmp_path)], capture_output=True, text=True)
    if result.returncode != 0 or not (tmp_path / "loop").exists():
        pytest.skip("无法创建目录联接（需要 NTFS）")
    files = fsops.tree_files(tmp_path)
    assert [f.name for f in files] == ["a.txt"]
