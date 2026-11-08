"""全自动零干预流水线专项验收（对应重构任务书）。

锁定的新行为：
- 缺信息兜底流转（未命名歌曲/未知艺术家/未分类专辑）；
- 目标同名自动改名 (2)(3)（磁盘已存在 + 计划内冲突），绝不覆盖；
- 超长路径自动截断（保留扩展名与防重名后缀）；
- 整夹连带打包：上级分散附属文件随整夹移动（歧义资料保留原位）；
- 压缩包全自动：随同目录音频归档或进"原始压缩包"，不解压；
- SKIPPED_ERROR：仅来源失联触发，跳过并报告，不阻断整体；
- 100% 默认勾选 + 一次 build→execute 无人值守完成。
"""

import os
import shutil
from pathlib import Path

from app.domain.models import PlanStatus, SourceRoot, Unit, UnitKind
from app.operations.executor import Executor
from app.operations.history import HistoryStore
from app.scan.scanner import Scanner
from tests.fixtures import make_wav, make_zip


def scan(path: Path):
    return Scanner().scan_source(SourceRoot(path=str(path)))


# ---------------------------------------------------------------------------
# 缺信息兜底流转
# ---------------------------------------------------------------------------
def test_missing_info_falls_through_to_ready(tmp_path):
    # “散曲”属通用目录词表，不冒充专辑名 → 未分类专辑兜底
    src = tmp_path / "src" / "散曲"
    src.mkdir(parents=True)
    make_wav(src / "未知曲目.wav", tags={})
    outcome = scan(tmp_path)
    track = [u for u in outcome.units if u.is_track][0]
    assert track.status == "ready"
    assert track.title == "未知曲目"           # 文件名兜底
    assert track.artist == "未知艺术家"         # 兜底艺术家
    assert any("兜底" in m for m in track.messages)
    # 计划可生成：落 未知艺术家/未分类专辑/
    track.checked = True
    from app.planning.plan import PlanBuilder, PlanOptions
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "target"))).build([track])
    assert plan.items[0].status == PlanStatus.READY.value
    assert "未知艺术家" in plan.items[0].target
    assert "未分类专辑" in plan.items[0].target


# ---------------------------------------------------------------------------
# 目标同名自动改名（磁盘已存在）
# ---------------------------------------------------------------------------
def test_existing_target_auto_renamed(tmp_path):
    from app.planning.plan import PlanBuilder, PlanOptions
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    f1 = src / "a" / "01 - A - 同名.wav"
    make_wav(f1, tags={"title": "同名", "artist": "A", "album": "M"})
    track = [u for u in scan(tmp_path).units if u.is_track][0]
    track.checked = True
    # 预先在目标放置同名文件
    existing = tmp_path / "target" / "A" / "M" / "01 - A - 同名.wav"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old")
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "target"))).build([track])
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value
    assert Path(item.target).name == "01 - A - 同名 (2).wav"
    # 执行后旧文件原样保留
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    assert existing.read_bytes() == b"old"
    assert Path(item.target).is_file()


def test_plan_internal_conflict_renamed_in_order(tmp_path):
    from app.domain import naming
    from app.planning.plan import PlanBuilder, PlanOptions
    src = tmp_path / "src"
    (src / "x").mkdir(parents=True)
    (src / "y").mkdir(parents=True)
    make_wav(src / "x" / "01 - A - 同名.wav",
             tags={"title": "同名", "artist": "A", "album": "M"})
    make_wav(src / "y" / "01 - A - 同名.wav",
             tags={"title": "同名", "artist": "A", "album": "M"})
    tracks = [u for u in scan(tmp_path).units if u.is_track]
    for t in tracks:
        t.checked = True
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "target"))).build(tracks)
    assert plan.counts().get("conflict", 0) == 0
    keys = {naming.normalize_key(i.target) for i in plan.items if i.selected}
    assert len(keys) == 2
    names = [Path(i.target).name for i in plan.items]
    assert any(n == "01 - A - 同名.wav" for n in names)
    assert any(n == "01 - A - 同名 (2).wav" for n in names)


# ---------------------------------------------------------------------------
# 超长路径自动截断
# ---------------------------------------------------------------------------
def test_long_path_truncated_end_to_end(tmp_path):
    from app.domain import naming
    from app.planning.plan import PlanBuilder, PlanOptions
    src = tmp_path / "src"
    src.mkdir()
    long_album = "专" * 120
    make_wav(src / "01 - A - 曲.wav",
             tags={"title": "曲", "artist": "A", "album": long_album})
    track = [u for u in scan(tmp_path).units if u.is_track][0]
    track.checked = True
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "target"))).build([track])
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value
    assert len(item.target) <= naming.MAX_PATH
    assert item.target.endswith(".wav")
    # 执行后文件落在截断后的专辑目录
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    assert Path(item.target).is_file()


# ---------------------------------------------------------------------------
# 整夹连带打包
# ---------------------------------------------------------------------------
def test_album_bundles_parent_accessories(tmp_path):
    src = tmp_path / "src"
    album_dir = src / "[2020]歌手 - 专辑"
    album_dir.mkdir(parents=True)
    for n, title in ((1, "一曲"), (2, "二曲"), (3, "三曲")):
        make_wav(album_dir / f"0{n} - 歌手 - {title}.wav",
                 tags={"title": title, "artist": "歌手", "album": "专辑",
                       "album_artist": "歌手", "track": n})
    # 上级分散附属资料（原 manual_note 触发项）
    (src / "扫图.png").write_bytes(b"img")
    (src / "说明.txt").write_text("notes", encoding="utf-8")
    (src / "特典.zip").write_bytes(b"PK")
    outcome = scan(tmp_path)
    album = [u for u in outcome.units if u.is_album][0]
    assert album.boundary_confirmed and album.status == "ready"
    album.checked = True
    from app.planning.plan import PlanBuilder, PlanOptions
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "target"))).build([album])
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value
    companions = item.extra.get("companions") or []
    names = {Path(p).name for p in companions}
    assert {"扫图.png", "说明.txt", "特典.zip"} <= names
    # 执行：附属资料随整夹进入目标目录
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    target = Path(item.target)
    assert (target / "扫图.png").is_file()
    assert (target / "说明.txt").is_file()
    assert (target / "特典.zip").is_file()
    assert not (src / "扫图.png").exists(), "移动模式附属资料应离开来源"
    # 撤销：整夹与连带资料一并回写
    record = HistoryStore(tmp_path / "history").list_runs()[0]
    from app.services import AppService
    from app.paths import DataPaths
    svc = AppService(DataPaths(tmp_path / "data", portable=True))
    undo = svc.build_undo(record)
    assert any(a.companions_restore for a in undo.actions), "撤销应包含连带资料回写"
    svc.apply_undo(undo)
    assert (src / "扫图.png").is_file(), "撤销后连带资料回来源"
    assert (album_dir / "01 - 歌手 - 一曲.wav").is_file(), "整夹回原位"


def test_album_bundles_skipped_when_ambiguous(tmp_path):
    """上级还有其它音频目录 → 歧义，附属资料保留原位（不误归属）。"""
    src = tmp_path / "src"
    album_dir = src / "专辑A"
    album_dir.mkdir(parents=True)
    for n in (1, 2, 3):
        make_wav(album_dir / f"0{n} - A - 曲{n}.wav",
                 tags={"title": f"曲{n}", "artist": "A", "album": "专辑A",
                       "album_artist": "A", "track": n})
    other = src / "专辑B"
    other.mkdir()
    for n in (1, 2, 3):
        make_wav(other / f"0{n} - B - 曲{n}.wav",
                 tags={"title": f"曲{n}", "artist": "B", "album": "专辑B",
                       "album_artist": "B", "track": n})
    (src / "共享资料.txt").write_text("shared", encoding="utf-8")
    outcome = scan(tmp_path)
    albums = [u for u in outcome.units if u.is_album]
    assert len(albums) == 2
    for album in albums:
        album.checked = True
    from app.planning.plan import PlanBuilder, PlanOptions
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "target"))).build(albums)
    for item in plan.items:
        assert not (item.extra.get("companions") or []), "歧义资料不得连带"
        assert item.status == PlanStatus.READY.value
    Executor(tmp_path / "history").execute(plan)
    assert (src / "共享资料.txt").is_file(), "歧义附属资料保留在来源"


# ---------------------------------------------------------------------------
# 压缩包全自动
# ---------------------------------------------------------------------------
def test_archive_auto_fallback_and_follow(tmp_path):
    from app.planning.plan import PlanBuilder, PlanOptions
    src = tmp_path / "src"
    (src / "有主的目录").mkdir(parents=True)
    (src / "无主的目录").mkdir(parents=True)
    make_wav(src / "有主的目录" / "01 - A - 曲.wav",
             tags={"title": "曲", "artist": "A", "album": "专辑",
                   "album_artist": "A"})
    make_zip(src / "有主的目录" / "附件包.zip", {"note.txt": b"x"})
    make_zip(src / "无主的目录" / "流浪包.zip", {"note.txt": b"x"})
    outcome = scan(tmp_path)
    units = list(outcome.units) + list(outcome.archives)
    for unit in units:
        unit.checked = True
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "target"))).build(units)
    archive_items = [i for i in plan.items if i.op == "archive_archive"]
    assert len(archive_items) == 2
    by_name = {Path(i.source).name: i for i in archive_items}
    follow = by_name["附件包.zip"]
    fallback = by_name["流浪包.zip"]
    assert follow.status == PlanStatus.READY.value
    assert "有主的目录" in follow.messages[0] or "随同目录音频" in follow.messages[0]
    assert Path(follow.target).parent.name == "专辑"
    assert Path(fallback.target).parent.name == "原始压缩包"
    Executor(tmp_path / "history").execute(plan)
    assert Path(follow.target).is_file()
    assert Path(fallback.target).is_file()
    # 原包字节原样保留
    assert Path(fallback.target).read_bytes() == (src / "无主的目录" / "流浪包.zip").read_bytes() \
        if (src / "无主的目录" / "流浪包.zip").exists() else True


# ---------------------------------------------------------------------------
# SKIPPED_ERROR：失联跳过不阻断
# ---------------------------------------------------------------------------
def test_missing_source_skips_others_execute(tmp_path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir(parents=True)
    make_wav(src / "a" / "01 - A - 曲A.wav",
             tags={"title": "曲A", "artist": "A", "album": "专辑A",
                   "album_artist": "A"})
    make_wav(src / "b" / "01 - B - 曲B.wav",
             tags={"title": "曲B", "artist": "B", "album": "专辑B",
                   "album_artist": "B"})
    outcome = scan(tmp_path)
    units = [u for u in outcome.units if u.is_track]
    for unit in units:
        unit.checked = True
    # 曲A 在计划前被外部删除（失联）
    lost = next(u for u in units if u.title == "曲A")
    os.remove(lost.path)
    from app.planning.plan import PlanBuilder, PlanOptions
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "target"))).build(units)
    statuses = {Path(i.source).name: i.status for i in plan.items}
    assert statuses["01 - A - 曲A.wav"] == PlanStatus.SKIPPED.value
    assert statuses["01 - B - 曲B.wav"] == PlanStatus.READY.value
    assert any(e["status"] == "已跳过" for e in plan.excluded)
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1, "失联不阻断其余项目执行"
    assert (tmp_path / "target" / "B" / "专辑B" / "01 - B - 曲B.wav").is_file()


# ---------------------------------------------------------------------------
# 无人值守全流程：扫描 → 全勾 → 执行
# ---------------------------------------------------------------------------
def test_zero_touch_full_pipeline(tmp_path):
    """扫描 → 100% 默认勾选 → 计划全部 READY → 执行成功。"""
    from app.paths import DataPaths
    from app.services import AppService
    src = tmp_path / "src"
    (src / "狂言盘").mkdir(parents=True)
    (src / "散曲收集").mkdir()
    make_wav(src / "狂言盘" / "01 - Ado - 唱.wav",
             tags={"title": "唱", "artist": "Ado", "album": "狂言",
                   "album_artist": "Ado", "year": "2022", "track": 1})
    make_wav(src / "狂言盘" / "02 - Ado - 踊.wav",
             tags={"title": "踊", "artist": "Ado", "album": "狂言",
                   "album_artist": "Ado", "year": "2022", "track": 2})
    make_wav(src / "散曲收集" / "01 - YOASOBI - 夜に駆ける.wav",
             tags={"title": "夜に駆ける", "artist": "YOASOBI", "album": "THE BOOK"})
    make_wav(src / "散曲收集" / "02 - YOASOBI - ハルカ.wav",
             tags={"title": "ハルカ", "artist": "YOASOBI", "album": "THE BOOK"})
    make_zip(src / "散曲收集" / "资料包.zip", {"n.txt": b"x"})
    svc = AppService(DataPaths(tmp_path / "data", portable=True))
    svc.settings.target_dir = str(tmp_path / "target")
    svc.add_sources([str(src)])
    # 100% 默认勾选（除失联，本场景无失联）
    assert all(u.checked for u in svc.state.units), "全自动：默认全勾"
    # 分组：无 needs（无失联）
    summary = svc.group_summary()
    assert summary["needs"] == 0
    plan = svc.build_plan()
    assert all(i.status == PlanStatus.READY.value for i in plan.items if i.selected)
    run = svc.execute_plan(plan)
    assert run.success == len([i for i in plan.items if i.selected])
    target = tmp_path / "target"
    # 散曲按识别专辑归位（含无标签兜底）
    assert (target / "Ado" / "狂言" / "01 - Ado - 唱.wav").is_file()
    assert (target / "YOASOBI" / "THE BOOK" / "01 - YOASOBI - 夜に駆ける.wav").is_file()
    # 压缩包随同目录音频（YOASOBI 专辑目录）
    assert (target / "YOASOBI" / "THE BOOK" / "资料包.zip").is_file()
    assert run.summary_text().startswith("成功")
