"""识别机制重构回归（docs/code_artifact.md §6 十项 Bug 对照 + 新规则锁定）。

- #1 日期目录名 8 位截断修复
- #2 _resolve_overlaps 父候选被子候选 100% 覆盖时剔除
- #3 _merge_multidisc 自然排序 + single_image_cues/issues 合并
- #4/#6 疑似重复与散曲聚合废除（无群星、组 ID 稳定）
- #5 专辑 duration 累加
- #7 iter_files(recursive=False) 仅当前层
- #8 分卷组判定（≥2 同主干分卷；单 .part1.rar 按普通单包）
- #9 整夹查重 (文件数, 字节) 预筛
- #10 后缀/词表口径统一收口 constants.py
以及：整夹艺术家确定性提取（绝不产生“群星”）、散曲元数据直读优先级、
无 CUE 整夹条件收紧、歌词三级确定性匹配。
"""

from __future__ import annotations

from pathlib import Path

from app.domain.models import SourceRoot, UnitStatus
from app.scan.album_detect import (AlbumCandidate, _resolve_overlaps,
                                   resolve_album_artist)
from app.scan.audio import AudioMeta, extract_dir_year_and_clean_name
from app.scan.attachments import iter_files
from app.scan.constants import GENERIC_DIR_WORDS, is_archive_file, is_generic_dirname
from app.scan.duplicates import detect_duplicates
from app.scan.lyrics_recognizer import (TrackCandidate, load_lyrics, match_lyrics_in_dir)
from app.scan.scanner import Scanner
from tests.fixtures import make_cue, make_wav


def scan(path: Path):
    return Scanner().scan_source(SourceRoot(path=str(path)))


def meta(path: str, **kw) -> AudioMeta:
    m = AudioMeta(path=path, fmt=kw.pop("fmt", "flac"), size=kw.pop("size", 10))
    for key, value in kw.items():
        setattr(m, key, value)
    return m


# ---------------------------------------------------------------------------
# Bug #1：日期目录名
# ---------------------------------------------------------------------------
def test_date_prefix_8digit_not_truncated():
    year, cleaned = extract_dir_year_and_clean_name("20250827 新专辑")
    assert (year, cleaned) == (2025, "新专辑")
    year, cleaned = extract_dir_year_and_clean_name("[250827]安田レイ - プリズム")
    assert (year, cleaned) == (2025, "安田レイ - プリズム")
    # 无日期前缀：年份识别但目录名不强行拆分
    year, cleaned = extract_dir_year_and_clean_name("2019 - Live")
    assert year == 2019 and cleaned == "2019 - Live"
    # 非日期数字不误判
    year, _ = extract_dir_year_and_clean_name("12345678 abcd")
    assert year is None


def test_scanner_track_uses_full_date_dir(tmp_path):
    src = tmp_path / "src"
    d = src / "20250827 新专辑"
    d.mkdir(parents=True)
    make_wav(d / "01 - 歌手A - 曲一.wav",
             tags={"title": "曲一", "artist": "歌手A", "track": 1})
    outcome = scan(src)
    track = [u for u in outcome.units if u.is_track][0]
    assert track.year == "2025", "8 位日期前缀必须取完整年份 2025（旧版错判 2020）"
    # 专辑层强制取自元数据（方案 §1）：无标签 → 未分类专辑，目录名不冒充
    assert track.album == "未分类专辑"
    assert not track.origins.get("album")


# ---------------------------------------------------------------------------
# Bug #2：父子候选重叠消解
# ---------------------------------------------------------------------------
def test_resolve_overlaps_drops_fully_covered_parent():
    child = AlbumCandidate(path="X:/m/Disc1", audio_files=["X:/m/Disc1/a.flac",
                                                           "X:/m/Disc1/b.flac"])
    parent = AlbumCandidate(path="X:/m", audio_files=child.audio_files)
    kept = _resolve_overlaps([parent, child])
    paths = {c.path for c in kept}
    assert "X:/m" not in paths, "父候选音频被子候选 100% 覆盖时必须剔除"
    assert "X:/m/Disc1" in paths


def test_resolve_overlaps_keeps_parent_with_extra_audio():
    child = AlbumCandidate(path="X:/m/Disc1", audio_files=["X:/m/Disc1/a.flac"])
    parent = AlbumCandidate(path="X:/m", audio_files=["X:/m/Disc1/a.flac",
                                                      "X:/m/b.flac"])
    kept = _resolve_overlaps([parent, child])
    assert {c.path for c in kept} == {"X:/m", "X:/m/Disc1"}, "未 100% 覆盖时父候选保留"


# ---------------------------------------------------------------------------
# Bug #3：多碟合并
# ---------------------------------------------------------------------------
def test_multidisc_natural_order_and_attribute_merge(tmp_path):
    base = tmp_path / "[240915]米津玄師 - LOST CORNER"
    for disc in (2, 10, 1):  # 故意乱序创建，验证自然排序 CD2 < CD10
        d = base / f"Disc{disc}"
        d.mkdir(parents=True)
        wav = make_wav(d / f"disc{disc}.wav", seconds=0.3)
        if disc == 2:
            # 引用缺失 → 子候选 issue + 整轨虚拟曲目，必须随合并保留（旧版静默丢失）
            make_cue(d / f"disc{disc}.cue", "ghost.wav", "LOST CORNER", "米津玄師",
                     [(1, "曲A", "米津玄師"), (2, "曲B", "米津玄師")])
        else:
            make_cue(d / f"disc{disc}.cue", wav.name, "LOST CORNER", "米津玄師",
                     [(1, f"曲{disc}", "米津玄師"), (2, f"返り咲く{disc}", "米津玄師")])
    outcome = scan(tmp_path)
    units = [u for u in outcome.units if u.is_album]
    assert len(units) == 1
    unit = units[0]
    assert unit.total_discs == 3
    order = [e for e in unit.evidence if e.startswith("碟片顺序")]
    assert order and order[0] == "碟片顺序：Disc1 / Disc2 / Disc10", \
        "碟序必须自然排序（旧版字典序 CD10 排 CD2 前）"
    assert any("缺少被引用文件" in i for i in unit.messages), "子候选 issues 必须合并保留"
    assert len(unit.virtual_tracks) == 6, "整轨 CUE 虚拟曲目必须随合并保留"


# ---------------------------------------------------------------------------
# Bug #4 / 群星禁令：多歌手整夹按主创归属，绝不产生“群星”
# ---------------------------------------------------------------------------
def test_album_artist_never_various_artists(tmp_path):
    src = tmp_path / "src"
    d = src / "合辑目录"
    d.mkdir(parents=True)
    make_wav(d / "01 - 主唱 - A曲.wav", seconds=0.3,
             tags={"title": "A曲", "artist": "主唱", "album": "合辑", "track": 1})
    make_wav(d / "02 - 主唱 - B曲.wav", seconds=0.3,
             tags={"title": "B曲", "artist": "主唱", "album": "合辑", "track": 2})
    make_wav(d / "03 - 客串 - C曲.wav", seconds=0.3,
             tags={"title": "C曲", "artist": "客串", "album": "合辑", "track": 3})
    outcome = scan(src)
    album = [u for u in outcome.units if u.is_album][0]
    assert album.album_artist == "主唱"
    assert album.album_artist != "群星"
    assert any("主创/首轨" in e or "频次" in e or "归属" in e for e in album.evidence)


def test_resolve_album_artist_priority_chain():
    files = ["a.flac", "b.flac"]
    # 1) 曲目 artist 唯一 → 直接采用
    metas = {"a.flac": meta("a.flac", artist="歌手X", album_artist="专辑歌手"),
             "b.flac": meta("b.flac", artist="歌手X", album_artist="专辑歌手")}
    artist, origin, _ = resolve_album_artist(files, metas, cue_performer="CUE歌手")
    assert (artist, origin) == ("歌手X", "tag")
    # 2) 曲目 artist 多值 → album_artist 唯一
    metas = {"a.flac": meta("a.flac", artist="歌手X", album_artist="专辑歌手"),
             "b.flac": meta("b.flac", artist="歌手Y", album_artist="专辑歌手")}
    artist, origin, _ = resolve_album_artist(files, metas, cue_performer="CUE歌手")
    assert (artist, origin) == ("专辑歌手", "tag")
    # 3) 标签全空 → CUE PERFORMER
    metas = {"a.flac": meta("a.flac"), "b.flac": meta("b.flac")}
    artist, origin, _ = resolve_album_artist(files, metas, cue_performer="CUE歌手")
    assert (artist, origin) == ("CUE歌手", "cue")
    # 4) 多值无 album_artist → 频次最高（并列取首轨）
    metas = {"a.flac": meta("a.flac", artist="乙"), "b.flac": meta("b.flac", artist="甲")}
    artist, origin, ev = resolve_album_artist(["a.flac", "b.flac"], metas, "")
    assert (artist, origin) == ("乙", "tag") and "主创" in ev
    # 5) 无任何线索 → 空串（由调用方落“未知艺术家”）
    artist, origin, _ = resolve_album_artist(files, {"a.flac": meta("a.flac")}, "")
    assert artist == "" and origin == "none"


def test_rip_dir_format_condition_direct():
    """无 CUE 整夹格式条件（②全部无损 或 含抓轨日志）直接验证。"""
    from app.scan.album_detect import _looks_like_rip_dir, _track_numbers_complete
    # mp3（非无损、无日志）→ False
    files = ["d/01.mp3", "d/02.mp3", "d/03.mp3"]
    metas = {f: meta(f, fmt="mp3") for f in files}
    assert _looks_like_rip_dir("d", files, metas) is False
    # 无损 → True；曲号断号 → False
    files = ["d/01.flac", "d/02.flac", "d/04.flac"]
    metas = {f: meta(f, fmt="flac", track=int(Path(f).stem)) for f in files}
    assert _looks_like_rip_dir("d", files, metas) is True
    assert _track_numbers_complete(files, metas) is False
    files_ok = ["d/01.flac", "d/02.flac", "d/03.flac"]
    metas_ok = {f: meta(f, fmt="flac", track=int(Path(f).stem)) for f in files_ok}
    assert _track_numbers_complete(files_ok, metas_ok) is True
    # 缺曲号 → False
    metas_none = {f: meta(f, fmt="flac") for f in files_ok}
    assert _track_numbers_complete(files_ok, metas_none) is False


# ---------------------------------------------------------------------------
# Bug #5：专辑 duration 累加
# ---------------------------------------------------------------------------
def test_album_duration_is_sum_of_tracks(tmp_path):
    src = tmp_path / "src"
    d = src / "[250827]安田レイ - プリズム"
    d.mkdir(parents=True)
    make_wav(d / "a.wav", seconds=0.6)
    make_cue(d / "a.cue", "a.wav", "プリズム", "安田レイ", [(1, "曲", "安田レイ")])
    outcome = scan(src)
    album = [u for u in outcome.units if u.is_album][0]
    assert album.duration > 0.5, "专辑时长必须为子曲目之和（旧版恒 0）"


# ---------------------------------------------------------------------------
# Bug #6：疑似重复废除
# ---------------------------------------------------------------------------
def test_suspect_detection_removed(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    # 同大小、同标签、同时长、不同内容：旧版会聚成 suspect 组
    make_wav(src / "a.wav", seconds=0.4, tags={"title": "同名", "artist": "A"})
    make_wav(src / "b.wav", seconds=0.4, tags={"title": "同名", "artist": "A"})
    units = [u for u in scan(src).units if u.is_track]
    report = detect_duplicates(units, hasher=lambda p: "hash-" + Path(p).name)
    assert report.groups == [], "字节不同不得再判疑似重复"
    assert report.suspect_count == 0
    # 相同内容仍成 exact 组，组 ID 稳定（内容哈希前缀）
    units[1].path = units[0].path  # 模拟同哈希：直接以签名相同验证
    report2 = detect_duplicates(units, hasher=lambda p: "samehash")
    assert len(report2.groups) == 1 and report2.groups[0].kind == "exact"
    assert report2.groups[0].id.startswith("exact-")


# ---------------------------------------------------------------------------
# Bug #7：iter_files 递归形参
# ---------------------------------------------------------------------------
def test_iter_files_non_recursive(tmp_path):
    (tmp_path / "top.jpg").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.jpg").write_bytes(b"x")
    top = iter_files(tmp_path, recursive=False)
    assert [Path(p).name for p in top] == ["top.jpg"]
    both = iter_files(tmp_path, recursive=True)
    assert {Path(p).name for p in both} == {"top.jpg", "nested.jpg"}


# ---------------------------------------------------------------------------
# Bug #8：分卷组判定
# ---------------------------------------------------------------------------
def test_multivolume_requires_two_same_stem_volumes(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "真分卷.part1.rar").write_bytes(b"a" * 16)
    (src / "真分卷.part2.rar").write_bytes(b"b" * 16)
    (src / "单卷.part1.rar").write_bytes(b"c" * 16)
    outcome = scan(src)
    by_name = {u.name: u for u in outcome.archives}
    assert by_name["真分卷.part1.rar"].archive["multivolume"] is True
    assert "分卷压缩包" in by_name["真分卷.part1.rar"].messages[0]
    assert by_name["单卷.part1.rar"].archive["multivolume"] is False, \
        "单个 .part1.rar 不得误标分卷"
    assert "原始压缩包" in by_name["单卷.part1.rar"].messages[0]


# ---------------------------------------------------------------------------
# Bug #9：整夹查重预筛（不同 (文件数,字节) 的整夹不哈希）
# ---------------------------------------------------------------------------
def test_album_dedup_prefilter_skips_hash(tmp_path):
    calls: list[str] = []

    def counting_hasher(path, progress=None, base=0, total=0):
        calls.append(str(path))
        return "same-content-hash"

    one = Unit_like_album("A", ["a1.flac"])
    two = Unit_like_album("B", ["b1.flac", "b2.flac"])
    t1 = Unit_like_track("t1", size=100)
    t2 = Unit_like_track("t2", size=100)
    report = detect_duplicates([one, two, t1, t2], hasher=counting_hasher)
    # 只有同大小散曲对被哈希（2 次）；两个整夹 (1,N) 与 (2,N) 不同键 → 不哈希
    assert len(calls) == 2, f"预筛失效：哈希了 {len(calls)} 个单元"
    assert len(report.groups) == 1 and report.groups[0].kind == "exact"
    assert set(report.groups[0].unit_ids) == {"t1", "t2"}


class Unit_like_album:
    """detect_duplicates 只用到 is_album/is_track/audio_files/status 等窄接口。"""

    def __init__(self, name: str, files: list[str]) -> None:
        self.name = name
        self.kind = "album"
        self.is_album = True
        self.is_track = False
        self.audio_files = [f"{name}/{f}" for f in files]
        self.status = "ready"
        self.status_enum_value = "ready"
        self.id = name
        self.path = f"X:/{name}"
        self.display_album = name
        self.fmt = "FLAC"
        self.duration = 0.0
        self.bitrate = 0
        self.sample_rate = 0
        self.bits = 0
        self.size = 0
        self.boundary_path = self.path
        self.status_enum = UnitStatus.READY


class Unit_like_track(Unit_like_album):
    def __init__(self, name: str, size: int) -> None:
        super().__init__(name, [])
        self.kind = "track"
        self.is_album = False
        self.is_track = True
        self.audio_files = []
        self.size = size
        self.path = f"X:/{name}.flac"


# ---------------------------------------------------------------------------
# Bug #10：口径统一
# ---------------------------------------------------------------------------
def test_constants_unified_suffix_rules():
    assert is_archive_file("x.002")
    assert is_archive_file("x.r01")
    assert is_archive_file("x.z02")
    assert is_archive_file("x.part01.rar")
    assert not is_archive_file("x.txt")
    from app.scan.attachments import is_attachment_candidate
    assert not is_attachment_candidate("x.002"), "第 2+ 卷不得再进附件列表（双条目）"
    from app.scan import scanner
    assert scanner.is_archive_name("x.7z")
    assert "散曲" in GENERIC_DIR_WORDS and is_generic_dirname(" 散曲 ")


# ---------------------------------------------------------------------------
# 新规则：散曲元数据直读优先级
# ---------------------------------------------------------------------------
def test_track_artist_priority_tag_over_albumartist_over_filename(tmp_path):
    src = tmp_path / "src" / "专辑目录"
    src.mkdir(parents=True)
    make_wav(src / "01 - 文件名歌手 - 歌名.wav", seconds=0.3,
             tags={"title": "歌名", "artist": "标签歌手", "album_artist": "专辑歌手",
                   "album": "标签专辑", "track": 1})
    make_wav(src / "02 - 文件名歌手 - 二曲.wav", seconds=0.3,
             tags={"title": "二曲", "album_artist": "专辑歌手", "album": "标签专辑",
                   "track": 2})
    outcome = scan(src.parent)
    tracks = {u.title: u for u in outcome.units if u.is_track}
    assert tracks["歌名"].artist == "标签歌手"
    assert tracks["歌名"].origins["artist"] == "tag"
    assert tracks["二曲"].artist == "专辑歌手", "单曲 artist 缺失时回退 album_artist"
    assert tracks["二曲"].origins["artist"] == "tag"
    assert tracks["二曲"].album == "标签专辑"
    assert tracks["二曲"].year == ""  # 各曲独立保留自身年份，不连坐


def test_track_album_comes_from_metadata_only(tmp_path):
    """专辑层强制元数据（方案 §1/§3.3）：无论父目录名是否通用，无标签一律
    落"未分类专辑"，原目录名不得进入目标路径。"""
    src = tmp_path / "src"
    generic = src / "散曲"
    nongeneric = src / "私藏精选集"
    for d in (generic, nongeneric):
        d.mkdir(parents=True)
    make_wav(generic / "a.wav", seconds=0.3, tags={"title": "甲", "artist": "歌手"})
    make_wav(nongeneric / "b.wav", seconds=0.3, tags={"title": "乙", "artist": "歌手"})
    make_wav(nongeneric / "c.wav", seconds=0.3,
             tags={"title": "丙", "artist": "歌手", "album": "标签专辑"})
    outcome = scan(src)
    tracks = {u.title: u for u in outcome.units if u.is_track}
    assert tracks["甲"].album == "未分类专辑"
    assert not tracks["甲"].origins.get("album"), "兜底专辑不标来源"
    assert tracks["乙"].album == "未分类专辑", "父目录名不得冒充专辑层"
    assert tracks["丙"].album == "标签专辑"
    assert tracks["丙"].origins["album"] == "tag"


def test_tracks_same_metadata_album_share_target_dir(tmp_path):
    """统一性目标（方案 §1）：不同来源目录的同元数据专辑自然汇入同一目录。"""
    src = tmp_path / "src"
    d1 = src / "下载1"
    d2 = src / "下载2"
    for d in (d1, d2):
        d.mkdir(parents=True)
    make_wav(d1 / "01 - 歌手 - 曲一.wav", seconds=0.3,
             tags={"title": "曲一", "artist": "歌手", "album": "同名专辑", "track": 1})
    make_wav(d2 / "01 - 歌手 - 曲二.wav", seconds=0.3,
             tags={"title": "曲二", "artist": "歌手", "album": "同名专辑", "track": 1})
    from app.planning.plan import PlanBuilder, PlanOptions
    units = [u for u in scan(src).units if u.is_track]
    for u in units:
        u.checked = True
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "t"))).build(units)
    dirs = {str(Path(i.target).parent) for i in plan.items}
    assert len(dirs) == 1, f"同一元数据专辑必须共享目录：{dirs}"
    assert "同名专辑" in next(iter(dirs))


# ---------------------------------------------------------------------------
# 新规则：无 CUE 整夹条件收紧
# ---------------------------------------------------------------------------
def test_rip_dir_conditions_tightened(tmp_path):
    src = tmp_path / "src"
    # ① 无损 + 标签一致 + 曲号 1..3 连续 → 整夹
    ok = src / "OK专辑"
    ok.mkdir(parents=True)
    for n in (1, 2, 3):
        make_wav(ok / f"{n:02d}.wav", seconds=0.3,
                 tags={"album": "无损专辑", "track": n, "artist": "歌手"})
    # ④ 曲号 1,2,4 断号 → 不是整夹，逐首散曲处理
    gap = src / "断号目录"
    gap.mkdir(parents=True)
    for n in (1, 2, 4):
        make_wav(gap / f"{n:02d}.wav", seconds=0.3,
                 tags={"album": "断号专辑", "track": n, "artist": "歌手"})
    outcome = scan(src)
    albums = [u.name for u in outcome.units if u.is_album]
    tracks_by_album = {}
    for u in outcome.units:
        if u.is_track:
            tracks_by_album.setdefault(u.album, []).append(u)
    assert "OK专辑" in albums, "满足全部条件的无损目录应识别为整夹"
    assert "断号目录" not in albums
    assert len(tracks_by_album.get("断号专辑", [])) == 3, "曲号断号的目录应逐首散曲处理"


# ---------------------------------------------------------------------------
# 新规则：歌词三级确定性匹配
# ---------------------------------------------------------------------------
def test_lyrics_level2_stripped_stem_match(tmp_path):
    src = tmp_path / "src" / "目录"
    src.mkdir(parents=True)
    make_wav(src / "01. 晴天.wav", seconds=0.3,
             tags={"title": "晴天", "artist": "周杰伦", "track": 1})
    (src / "晴天.lrc").write_bytes("[ti:晴天]\n[00:01.00]歌词\n".encode("utf-8"))
    outcome = scan(src.parent)
    track = [u for u in outcome.units if u.is_track][0]
    atts = [a for a in track.attachments if a.get("role") == "lyrics"]
    assert len(atts) == 1
    assert atts[0]["link_status"] == "auto"
    assert "曲号" in "".join(atts[0]["match_evidence"])


def test_lyrics_level3_title_tag_match(tmp_path):
    src = tmp_path / "src" / "目录"
    src.mkdir(parents=True)
    make_wav(src / "track01.wav", seconds=0.3,
             tags={"title": "夜に駆ける", "artist": "YOASOBI", "track": 1})
    (src / "完全无关命名.lrc").write_bytes(
        "[ti:夜に駆ける]\n[00:01.00]歌詞\n".encode("utf-8"))
    outcome = scan(src.parent)
    track = [u for u in outcome.units if u.is_track][0]
    atts = [a for a in track.attachments if a.get("role") == "lyrics"]
    assert len(atts) == 1 and atts[0]["link_status"] == "auto"
    assert "唯一标题" in atts[0]["note"] or "LRC 标题" in "".join(atts[0]["match_evidence"])


def test_lyrics_match_function_level_and_claim():
    infos = [load_lyrics_str("同名.lrc", "[00:01.00]x"),
             load_lyrics_str("同名 (1).lrc", "[ti:曲]\n[00:01.00]x")]
    cands = [TrackCandidate(key="t1", name="同名", title="曲", path="X:/d/同名.flac")]
    bindings = match_lyrics_in_dir(infos, cands)
    assert bindings[0].candidate is not None and bindings[0].level == 1
    assert bindings[1].candidate is None
    assert bindings[1].claimed_by == "同名.lrc", "第二份命中同一曲目 → 标记语言版本场景"


def load_lyrics_str(name: str, text: str):
    """在临时固定目录构造 LyricsInfo（match_lyrics_in_dir 只读 path/stem）。"""
    import tempfile
    d = Path(tempfile.gettempdir()) / "lyr_test"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_bytes((text or "[00:01.00]x").encode("utf-8"))
    return load_lyrics(p)


# ---------------------------------------------------------------------------
# 整夹目标 = 元数据专辑名（基于元数据动态重命名专辑目录方案）
# ---------------------------------------------------------------------------
def test_album_target_uses_cue_title_and_reports_rename(tmp_path):
    """CUE TITLE 是元数据专辑名：目标目录重命名，预览明示，内部原样。"""
    src = tmp_path / "src"
    d = src / "[250827]安田レイ - プリズム"
    d.mkdir(parents=True)
    make_wav(d / "a.wav", seconds=0.4)
    make_cue(d / "a.cue", "a.wav", "プリズム", "安田レイ", [(1, "曲", "安田レイ")])
    outcome = scan(src)
    unit = [u for u in outcome.units if u.is_album][0]
    assert unit.album == "プリズム" and unit.origins.get("album") == "cue"
    unit.checked = True
    from app.planning.plan import PlanBuilder, PlanOptions
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "t"))).build([unit])
    item = plan.items[0]
    assert Path(item.target) == tmp_path / "t" / "安田レイ" / "プリズム"
    renamed = [x for x in item.details if "专辑目录重命名" in x]
    assert renamed and "プリズム" in renamed[0]


def test_album_target_tag_album_when_cue_title_missing(tmp_path):
    """CUE 无 TITLE 时回退标签专辑名；两者皆缺 → 未分类专辑兜底。"""
    src = tmp_path / "src"
    d1 = src / "rip_tagged"
    d1.mkdir(parents=True)
    make_wav(d1 / "a.wav", seconds=0.4, tags={"album": "标签专辑名", "artist": "歌手"})
    p = d1 / "a.cue"
    cue_lines = '\r\n'.join(['FILE "a.wav" WAVE',
                             '  TRACK 01 AUDIO',
                             '    INDEX 01 00:00:00', ''])
    p.write_text(cue_lines, encoding="utf-8")
    outcome = scan(src)
    unit = [u for u in outcome.units if u.is_album][0]
    assert unit.album == "标签专辑名" and unit.origins.get("album") == "tag"
    unit.checked = True
    from app.planning.plan import PlanBuilder, PlanOptions
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "t"))).build([unit])
    assert Path(plan.items[0].target) == tmp_path / "t" / "歌手" / "标签专辑名"
