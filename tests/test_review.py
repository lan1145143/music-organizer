"""全自动零干预流水线：双态机（READY/SKIPPED_ERROR）、默认全勾、艺术家目录解析、计划发现回写。"""

from pathlib import Path

from app.domain.models import (PlanStatus, SourceRoot, Unit, UnitKind, UnitStatus)
from app.paths import DataPaths
from app.planning.review import evaluate_unit, summarize
from app.services import AppService
from app.scan.scanner import Scanner
from tests.fixtures import build_dispersed, build_full_rip_album


def make_service(tmp_path: Path) -> AppService:
    return AppService(DataPaths(root=tmp_path / "data", portable=True))


def album(**kwargs) -> Unit:
    unit = Unit(kind=UnitKind.ALBUM.value, path="X:/a", name="a",
                boundary_path="X:/a", album="专辑", audio_files=["X:/a/01.wav"])
    for key, value in kwargs.items():
        setattr(unit, key, value)
    return unit


def track(**kwargs) -> Unit:
    unit = Unit(kind=UnitKind.TRACK.value, path="X:/t/01 - A - B.wav", name="01",
                title="B", artist="A", album="专辑")
    for key, value in kwargs.items():
        setattr(unit, key, value)
    return unit


# ---------------------------------------------------------------------------
# 双态机：一切正常项 READY，仅失联跳过
# ---------------------------------------------------------------------------
def test_evaluate_groups():
    ready = album(boundary_confirmed=True)
    review = evaluate_unit(ready)
    assert review.organizable and review.group == "ready"
    assert any("未知艺术家" in r for r in review.reminders)

    # 未确认边界/分散资料/CUE 缺失/缺歌名：全部只提醒，不拦截
    unconfirmed = album(boundary_confirmed=False)
    assert evaluate_unit(unconfirmed).organizable
    dispersed = album(manual_note="上级还有资料")
    assert evaluate_unit(dispersed).organizable
    no_title = track(title="")
    review = evaluate_unit(no_title)
    assert review.organizable
    assert any("未命名歌曲" in r or "缺少歌曲名" in r for r in review.reminders)

    archive_auto = Unit(kind=UnitKind.ARCHIVE.value, path="X:/p.zip", name="p",
                        archive={"mode": "archive_only", "status": "pending"})
    assert evaluate_unit(archive_auto).organizable

    done = album(boundary_confirmed=True, status=UnitStatus.DONE.value)
    review = evaluate_unit(done)
    assert review.group == "side" and review.side_kind == "done"

    child = track(status=UnitStatus.CHILD.value)
    review = evaluate_unit(child)
    assert review.group == "side" and review.side_kind == "child"


def test_missing_source_is_skipped_error():
    """唯一拦截：来源失联 → SKIPPED_ERROR（跳过并报告）。"""
    missing = track(status=UnitStatus.MISSING.value)
    review = evaluate_unit(missing)
    assert not review.organizable and review.group == "needs"
    assert any("来源已失联" in b for b in review.blockers)
    assert any("跳过" in b for b in review.blockers)
    # 失联不传染：其它正常项仍可整理
    ok = album(boundary_confirmed=True)
    assert evaluate_unit(ok).organizable


def test_duplicates_kept_and_informative():
    unit = album(boundary_confirmed=True)
    review = evaluate_unit(unit)
    assert not any(r.startswith("未发现重复") for r in review.reminders), \
        "未运行的检查不得显示为已通过"

    suspect = album(boundary_confirmed=True, duplicate_group="g", dup_role="suspect")
    review = evaluate_unit(suspect)
    assert review.organizable, "疑似重复保留各份继续整理"
    assert any("疑似重复" in r for r in review.reminders)


def test_group_summary_counts():
    units = [album(boundary_confirmed=True),
             track(status=UnitStatus.MISSING.value),  # 唯一的 needs（失联）
             album(boundary_confirmed=True, status=UnitStatus.DONE.value)]
    units[0].checked = True
    summary = summarize(units)
    assert summary["ready"] == 1 and summary["ready_checked"] == 1
    assert summary["needs"] == 1 and summary["needs_checked"] == 0
    assert summary["side"] == 1


# ---------------------------------------------------------------------------
# 默认勾选与显式选择
# ---------------------------------------------------------------------------
def test_default_selection_all_checked(tmp_path):
    """全自动：100% 默认勾选（除失联）。"""
    service = make_service(tmp_path)
    units = [album(), track(title=""), track(status=UnitStatus.MISSING.value)]
    service.state.units = units
    service.apply_default_selection()
    assert units[0].checked is True
    assert units[1].checked is True
    assert units[2].checked is False, "失联项默认不勾选"


def test_user_uncheck_persists_across_rescan(tmp_path):
    service = make_service(tmp_path)
    unit = album(boundary_confirmed=True)
    service.state.units = [unit]
    service.set_checked([unit], False, explicit=True)
    assert unit.user_checked is False
    service.apply_default_selection()
    assert unit.checked is False, "用户明确取消后不得自动勾回"


def test_user_explicit_check_survives(tmp_path):
    service = make_service(tmp_path)
    unit = track()
    service.state.units = [unit]
    service.set_checked([unit], True, explicit=True)
    service.apply_default_selection()
    assert unit.checked is True


def test_migration_on_load(tmp_path):
    """旧状态（无 user_checked 字段）加载后按全自动规则应用默认勾选。"""
    service = make_service(tmp_path)
    ready = album(boundary_confirmed=True, name="就绪专辑")
    needs = album(name="未勾选专辑", user_checked=False)
    service.state.units = [ready, needs]
    service.save_state()
    fresh = AppService(DataPaths(root=tmp_path / "data", portable=True))
    loaded = {u.name: u for u in fresh.state.units}
    assert loaded["就绪专辑"].checked is True
    assert loaded["未勾选专辑"].checked is False, "用户显式取消保留"


# ---------------------------------------------------------------------------
# 艺术家目录解析
# ---------------------------------------------------------------------------
def test_resolve_artist_dir_exact_match_case_insensitive(tmp_path):
    service = make_service(tmp_path)
    target = tmp_path / "target"
    (target / "ado").mkdir(parents=True)
    service.settings.target_dir = str(target)
    service.save_settings()

    name, reused = service.resolve_artist_dir("ADO")
    assert reused is True and name == "ado"   # 使用已有目录的真实名称

    name, reused = service.resolve_artist_dir("新歌手")
    assert reused is False and name == "新歌手"


def test_resolve_artist_dir_beyond_display_limit(tmp_path):
    """精确匹配不受候选展示数量（30）限制。"""
    service = make_service(tmp_path)
    target = tmp_path / "target"
    target.mkdir(parents=True)
    for i in range(40):
        (target / f"歌手{i:03d}").mkdir()
    real = target / "深处歌手"
    real.mkdir()
    service.settings.target_dir = str(target)
    service.save_settings()

    assert len(service.existing_artist_dirs("")) <= 30
    name, reused = service.resolve_artist_dir("深处歌手")
    assert reused is True and name == "深处歌手"


def test_resolve_artist_dir_excludes_program_dirs(tmp_path):
    service = make_service(tmp_path)
    target = tmp_path / "target"
    (target / "重复文件").mkdir(parents=True)
    (target / "原始压缩包").mkdir()
    service.settings.target_dir = str(target)
    service.save_settings()

    names = {info["name"] for info in service.existing_artist_dirs()}
    assert names == set(), "程序用途目录不作为艺术家候选"
    name, reused = service.resolve_artist_dir("重复文件")
    assert reused is False


def test_plan_reuses_existing_artist_dir(tmp_path):
    service = make_service(tmp_path)
    source_root = tmp_path / "src"
    album_dir = build_full_rip_album(source_root)
    outcome = Scanner().scan_source(SourceRoot(path=str(source_root)))
    unit = [u for u in outcome.units if u.is_album][0]
    unit.album_artist = "安田レイ"
    unit.checked = True
    service.state.units = [unit]
    target = tmp_path / "target"
    (target / "安田レイ").mkdir(parents=True)
    service.settings.target_dir = str(target)
    service.save_settings()

    plan = service.build_plan()
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value
    assert Path(item.target) == target / "安田レイ" / "プリズム"  # 元数据专辑名（CUE TITLE）
    assert item.extra["artist_dir"] == {"name": "安田レイ", "reused": True}
    assert any("使用已有目录" in d for d in item.details)

    unit.album_artist = "全新歌手"
    plan = service.build_plan()
    item = plan.items[0]
    assert item.extra["artist_dir"]["reused"] is False
    assert any("将新建" in d for d in item.details)


# ---------------------------------------------------------------------------
# 计划发现回写统一状态
# ---------------------------------------------------------------------------
def test_plan_findings_synced_to_units(tmp_path):
    """计划检查发现的异常回写到单元消息，分组与整理中心可见。"""
    import os
    service = make_service(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    a_path = src / "a.wav"
    b_path = src / "b.wav"
    a_path.write_bytes(b"a")
    b_path.write_bytes(b"b")
    a = track(path=str(a_path), title="同名", artist="A", album="M", track=None)
    b = track(path=str(b_path), title="同名", artist="A", album="M", track=None)
    for unit in (a, b):
        unit.checked = True
        unit.album_artist = "A"
    service.state.units = [a, b]
    service.settings.target_dir = str(tmp_path / "target")
    service.save_settings()
    plan = service.build_plan()
    # 全自动：同名目标自动改名，不再产生 CONFLICT
    renamed = [i for i in plan.items if "自动改名" in "；".join(i.messages)]
    assert len(renamed) >= 1, "同名目标应自动改名"
    assert len({naming.normalize_key(i.target) for i in plan.items}) == len(plan.items), \
        "计划内不允许目标键重复"
    # 失联项：跳过并回写结论
    a_path.unlink()
    plan = service.build_plan()
    skipped = [i for i in plan.items if i.status == PlanStatus.SKIPPED.value]
    assert skipped, "来源失联的计划项应跳过"
    assert any(m.startswith("计划检查：") for u in (a,) for m in u.messages), \
        "计划检查结论应回写单元消息"


import app.domain.naming as naming  # noqa: E402


# ---------------------------------------------------------------------------
# 自动边界（全自动：分散资料也自动采用）
# ---------------------------------------------------------------------------
def test_dispersed_album_auto_adopted(tmp_path):
    """全自动：分散资料的候选同样自动采用边界（连带规则见 plan 层）。"""
    source_root = tmp_path / "src"
    build_dispersed(source_root)
    outcome = Scanner().scan_source(SourceRoot(path=str(source_root)))
    unit = [u for u in outcome.units if u.is_album][0]
    assert unit.auto_boundary and unit.boundary_confirmed
    assert unit.status == UnitStatus.READY.value
    review = evaluate_unit(unit)
    assert review.organizable


def test_preserve_plan_selection_keeps_user_unchecks():
    """切标签重建计划后，用户在预览页取消勾选的条目必须保持取消（unit_id+op 作键）。"""
    from app.domain.models import OpKind, PlanItem
    from app.ui.review_center import preserve_plan_selection

    old_album = PlanItem(op=OpKind.ALBUM_MOVE.value, unit_id="album-1", selected=True)
    old_track = PlanItem(op=OpKind.TRACK_MOVE.value, unit_id="track-1", selected=False)
    old_cue = PlanItem(op=OpKind.CUE_FIX.value, unit_id="track-1", selected=True)
    # 新计划：全新对象（模拟 build_plan 重建，selected 恢复默认 True）
    new_album = PlanItem(op=OpKind.ALBUM_MOVE.value, unit_id="album-1", selected=True)
    new_track = PlanItem(op=OpKind.TRACK_MOVE.value, unit_id="track-1", selected=True)
    new_cue = PlanItem(op=OpKind.CUE_FIX.value, unit_id="track-1", selected=True)
    new_extra = PlanItem(op=OpKind.TRACK_MOVE.value, unit_id="track-2", selected=True)

    preserve_plan_selection([old_album, old_track, old_cue],
                            [new_album, new_track, new_cue, new_extra])

    assert new_album.selected is True
    assert new_track.selected is False   # 用户明确取消的保持取消
    assert new_cue.selected is True
    assert new_extra.selected is True    # 新计划新增的项不受影响


def test_preserve_plan_selection_empty_old_is_noop():
    from app.domain.models import OpKind, PlanItem
    from app.ui.review_center import preserve_plan_selection

    item = PlanItem(op=OpKind.TRACK_MOVE.value, unit_id="t", selected=True)
    preserve_plan_selection([], [item])
    assert item.selected is True
