import os
from pathlib import Path

from app.domain.models import OpKind, PlanStatus, Unit, UnitKind
from app.planning.plan import PlanBuilder, PlanOptions


def make_album(path: Path, artist="测试歌手", album="专辑", confirmed=True, **kwargs):
    path.mkdir(parents=True, exist_ok=True)
    (path / "cover.jpg").write_bytes(b"x")
    unit = Unit(kind=UnitKind.ALBUM.value, path=str(path), name=path.name,
                boundary_path=str(path), album=album, album_artist=artist,
                checked=True, boundary_confirmed=confirmed, file_count=1, size=10)
    for key, value in kwargs.items():
        setattr(unit, key, value)
    return unit


def make_track(path: Path, **kwargs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    defaults = dict(kind=UnitKind.TRACK.value, path=str(path), name=path.stem,
                    title="曲名", artist="歌手", album="专辑", album_artist="专辑歌手",
                    track=3, year="2024", checked=True, size=5)
    defaults.update(kwargs)
    return Unit(**defaults)


def options(tmp_path, **kwargs):
    return PlanOptions(target_root=str(tmp_path / "target"), **kwargs)


def test_album_plan_renames_to_metadata_album_dir(tmp_path):
    """专辑目录动态重命名：目标专辑层 = 元数据专辑名（不再沿用来源目录名）。"""
    album = make_album(tmp_path / "src" / "[250827]某歌手 - 专辑名")
    plan = PlanBuilder(options(tmp_path)).build([album])
    item = [i for i in plan.items if i.op == OpKind.ALBUM_MOVE.value][0]
    assert item.status == PlanStatus.READY.value
    assert Path(item.target) == tmp_path / "target" / "测试歌手" / "专辑"
    renamed = [d for d in item.details if "专辑目录重命名" in d]
    assert renamed and "[250827]某歌手 - 专辑名" in renamed[0] and "专辑" in renamed[0],         "预览必须明示目录重命名（来源名 → 元数据名）"
    assert "cover.jpg" in item.details
    assert item.extra["content_note"] == "内部名称与层级保持原样"


def test_album_plan_keeps_name_when_already_metadata(tmp_path):
    """来源目录名恰为元数据专辑名时不显示重命名提示。"""
    album = make_album(tmp_path / "src" / "专辑")
    plan = PlanBuilder(options(tmp_path)).build([album])
    item = plan.items[0]
    assert Path(item.target) == tmp_path / "target" / "测试歌手" / "专辑"
    assert not any("专辑目录重命名" in d for d in item.details)


def test_album_without_metadata_falls_back_to_unknown_album(tmp_path):
    """元数据缺失（§3.3）：整夹目标落"未分类专辑"并提示。"""
    album = make_album(tmp_path / "src" / "神秘专辑", album="")
    plan = PlanBuilder(options(tmp_path)).build([album])
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value
    assert Path(item.target) == tmp_path / "target" / "测试歌手" / "未分类专辑"
    assert any("未分类专辑" in m for m in item.messages)


def test_unconfirmed_boundary_still_ready(tmp_path):
    """全自动：边界一律视为已采用，不再存在"待确认边界"拦截。"""
    album = make_album(tmp_path / "src" / "专辑A", confirmed=False)
    plan = PlanBuilder(options(tmp_path)).build([album])
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value


def test_missing_album_artist_no_longer_blocks(tmp_path):
    """缺整理艺术家不阻止执行：落"未知艺术家"目录并带建议提醒。"""
    album = make_album(tmp_path / "src" / "专辑A", artist="")
    album.artist_suggestion = "测试歌手"
    album.artist_suggestion_confidence = "medium"
    album.artist_suggestion_evidence = "曲目艺术家一致"
    plan = PlanBuilder(options(tmp_path)).build([album])
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value
    assert "未知艺术家" in item.target
    assert any("未知艺术家" in m for m in item.messages)


def test_missing_album_artist_without_suggestion_still_runs(tmp_path):
    """完全无线索也不阻止：提醒并落"未知艺术家"。"""
    album = make_album(tmp_path / "src" / "专辑A", artist="")
    plan = PlanBuilder(options(tmp_path)).build([album])
    assert plan.items[0].status == PlanStatus.READY.value
    assert any("未知艺术家" in m for m in plan.items[0].messages)


def test_target_exists_auto_renamed_not_overwrite(tmp_path):
    """目标已存在同名项 → 自动追加 (2)，绝不覆盖，状态保持可执行。"""
    album = make_album(tmp_path / "src" / "专辑A")
    existing = tmp_path / "target" / "测试歌手" / "专辑"
    existing.mkdir(parents=True)
    plan = PlanBuilder(options(tmp_path)).build([album])
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value
    assert Path(item.target).name == "专辑 (2)"
    assert any("自动改名" in m for m in item.messages)


def test_target_inside_source_blocked(tmp_path):
    album = make_album(tmp_path / "src" / "专辑A")
    opts = PlanOptions(target_root=str(tmp_path / "src" / "专辑A" / "整理"))
    plan = PlanBuilder(opts).build([album])
    assert not plan.items
    assert any("来源内部" in m for m in plan.messages)
    # 显式来源根同样生效
    plan2 = PlanBuilder(opts).build([album], source_roots=[str(tmp_path / "src" / "专辑A")])
    assert not plan2.items
    assert any("来源内部" in m for m in plan2.messages)


def test_track_plan_naming_template_a(tmp_path):
    track = make_track(tmp_path / "src" / "01 - 歌手 - 曲名.flac")
    plan = PlanBuilder(options(tmp_path)).build([track])
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value
    assert Path(item.target) == tmp_path / "target" / "专辑歌手" / "专辑" / "03 - 歌手 - 曲名.flac"


def test_track_template_b_and_missing_year(tmp_path):
    track = make_track(tmp_path / "src" / "x.flac", year="")
    plan = PlanBuilder(options(tmp_path, template="B")).build([track])
    assert Path(plan.items[0].target).parent.name == "专辑"


def test_track_missing_artist_uses_fallback(tmp_path):
    """全自动：缺艺术家用"未知艺术家"兜底，不再阻止。"""
    track = make_track(tmp_path / "src" / "x.flac", artist="", album_artist="")
    plan = PlanBuilder(options(tmp_path)).build([track])
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value
    assert "未知艺术家" in item.target


def test_parent_and_child_not_planned_twice(tmp_path):
    album_dir = tmp_path / "src" / "专辑A"
    album_dir.mkdir(parents=True)
    audio = album_dir / "01 - 歌手 - 曲名.flac"
    audio.write_bytes(b"audio")
    album = make_album(album_dir)
    album.audio_files = [str(audio)]
    track = make_track(audio)
    plan = PlanBuilder(options(tmp_path)).build([album, track])
    ops = [i.op for i in plan.items if i.selected]
    assert ops.count(OpKind.ALBUM_MOVE.value) == 1
    assert ops.count(OpKind.TRACK_MOVE.value) == 0
    assert track.status == "child"


def test_parent_child_overlap_auto_dedup(tmp_path):
    """计划内父子重叠 → 整夹优先，子级项自动剔除（不再冲突拦截）。"""
    album_dir = tmp_path / "src" / "专辑A"
    album_dir.mkdir(parents=True)
    audio = album_dir / "01 - 歌手 - 曲名.flac"
    audio.write_bytes(b"audio")
    album = make_album(album_dir)
    album.audio_files = [str(audio)]
    track = make_track(audio)
    track.parent_id = album.id  # 模拟残留的子级计划
    plan = PlanBuilder(options(tmp_path)).build([album, track])
    album_items = [i for i in plan.items if i.op == OpKind.ALBUM_MOVE.value and i.selected]
    track_items = [i for i in plan.items if i.op == OpKind.TRACK_MOVE.value and i.selected]
    assert len(album_items) == 1
    assert not track_items, "整夹优先，重叠散曲项自动剔除"


def test_unchecked_units_excluded(tmp_path):
    album = make_album(tmp_path / "src" / "专辑A")
    album.checked = False
    plan = PlanBuilder(options(tmp_path)).build([album])
    assert plan.items == []
    assert plan.excluded and plan.excluded[0]["status"] == "未勾选"


def test_missing_source_skipped_listed(tmp_path):
    """来源失联的勾选项：跳过并列入排除（不阻断整体计划）。"""
    album = make_album(tmp_path / "src" / "专辑A")
    import shutil
    shutil.rmtree(album.boundary_path)
    plan = PlanBuilder(options(tmp_path)).build([album])
    assert plan.items[0].status == PlanStatus.SKIPPED.value
    assert any(e["status"] == "已跳过" for e in plan.excluded)


def test_duplicate_item_goes_to_duplicate_dir(tmp_path):
    track = make_track(tmp_path / "src" / "01 - 歌手 - 曲名.flac")
    track.dup_role = "duplicate"
    track.duplicate_group = "exact-1"
    plan = PlanBuilder(options(tmp_path)).build([track])
    item = [i for i in plan.items if i.op == OpKind.DUPLICATE_MOVE.value][0]
    assert item.status == PlanStatus.READY.value
    assert Path(item.target).parent.name == "重复文件"


def test_path_too_long_auto_truncated(tmp_path):
    """超长路径自动截断文件名主干（保留扩展名），不再阻止。"""
    long_name = "长" * 100
    track = make_track(tmp_path / "src" / "x.flac", title=long_name, album=long_name,
                       album_artist=long_name)
    opts = PlanOptions(target_root=str(tmp_path / ("t" * 80)))
    plan = PlanBuilder(opts).build([track])
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value
    from app.domain.naming import MAX_PATH
    assert len(item.target) <= MAX_PATH, item.target
    assert item.target.endswith(".flac")
    assert any("截断" in m for m in item.messages)


def test_plan_internal_conflict_auto_renamed(tmp_path):
    """计划内目标冲突 → 顺序自动改名，不产生冲突状态。"""
    a = make_track(tmp_path / "src" / "a" / "x.flac")
    b = make_track(tmp_path / "src" / "b" / "y.flac")
    plan = PlanBuilder(options(tmp_path)).build([a, b])
    assert plan.counts().get("conflict", 0) == 0
    targets = [i.target for i in plan.items if i.selected]
    from app.domain import naming
    assert len({naming.normalize_key(t) for t in targets}) == len(targets)
    assert any("自动改名" in m for i in plan.items for m in i.messages)


def test_archive_item_target(tmp_path):
    archive = Unit(kind=UnitKind.ARCHIVE.value, path=str(tmp_path / "src" / "包.zip"),
                   name="包.zip", checked=True)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "包.zip").write_bytes(b"PK")
    archive.archive = {"mode": "archive_only", "status": "pending"}
    plan = PlanBuilder(options(tmp_path)).build([archive])
    item = plan.items[0]
    assert item.op == OpKind.ARCHIVE_ARCHIVE.value
    assert item.status == PlanStatus.READY.value
    assert Path(item.target) == tmp_path / "target" / "原始压缩包" / "包.zip"


def test_archive_follows_same_dir_audio(tmp_path):
    """同目录音频有唯一归属目录 → 原包随音频走。"""
    track = make_track(tmp_path / "src" / "01 - 歌手 - 曲名.flac")
    archive = Unit(kind=UnitKind.ARCHIVE.value, path=str(tmp_path / "src" / "包.zip"),
                   name="包.zip", checked=True)
    (tmp_path / "src" / "包.zip").write_bytes(b"PK")
    archive.archive = {"mode": "archive_only", "status": "pending"}
    plan = PlanBuilder(options(tmp_path)).build([track, archive])
    arch_item = [i for i in plan.items if i.op == OpKind.ARCHIVE_ARCHIVE.value][0]
    track_item = [i for i in plan.items if i.op == OpKind.TRACK_MOVE.value][0]
    assert arch_item.status == PlanStatus.READY.value
    assert Path(arch_item.target).parent == Path(track_item.target).parent
    assert any("随同目录音频" in m for m in arch_item.messages)


def test_archive_skip_mode_still_planned(tmp_path):
    """全自动：不再存在 skip 模式，旧状态里的 skip 也照常归档。"""
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "包.zip").write_bytes(b"PK")
    archive = Unit(kind=UnitKind.ARCHIVE.value, path=str(tmp_path / "src" / "包.zip"),
                   name="包.zip", checked=True)
    archive.archive = {"mode": "skip", "status": "skipped"}
    plan = PlanBuilder(options(tmp_path)).build([archive])
    assert len(plan.items) == 1
    assert plan.items[0].status == PlanStatus.READY.value


def test_cue_fix_skipped_when_track_source_missing(tmp_path):
    """散曲失联（本次跳过）时不得生成 CUE 修正项：
    改写会把引用指向曲目从未到达的目标，CUE 就此作废。"""
    cue = tmp_path / "src" / "album.cue"
    cue.parent.mkdir(parents=True)
    cue.write_text('TITLE "专辑"\nFILE "01 - 歌手 - 曲名.wav" WAVE\n'
                   "  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n", encoding="utf-8")
    unit = make_track(tmp_path / "src" / "01 - 歌手 - 曲名.wav",
                      attachments=[{"path": str(cue), "role": "cue", "selected": True}])
    unit.path = str(tmp_path / "src" / "01 - 歌手 - 曲名.wav")
    (tmp_path / "src" / "01 - 歌手 - 曲名.wav").unlink()  # 音频已被外部删除，未重扫

    plan = PlanBuilder(options(tmp_path)).build([unit])

    track_item = [i for i in plan.items if i.op == OpKind.TRACK_MOVE.value][0]
    assert track_item.status == PlanStatus.SKIPPED.value
    assert [i for i in plan.items if i.op == OpKind.CUE_FIX.value] == []


def test_cue_fix_present_when_track_ready(tmp_path):
    """对照：散曲就绪且引用路径改变时，仍生成 CUE 修正项。"""
    cue = tmp_path / "src" / "album.cue"
    cue.parent.mkdir(parents=True)
    cue.write_text('TITLE "专辑"\nFILE "01 - 歌手 - 曲名.wav" WAVE\n'
                   "  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n", encoding="utf-8")
    unit = make_track(tmp_path / "src" / "01 - 歌手 - 曲名.wav",
                      attachments=[{"path": str(cue), "role": "cue", "selected": True}])

    plan = PlanBuilder(options(tmp_path)).build([unit])

    assert [i for i in plan.items if i.op == OpKind.CUE_FIX.value]
