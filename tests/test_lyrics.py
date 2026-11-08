"""外置歌词：识别、编码、关联建议、计划预览、执行与撤销。"""

import os
from pathlib import Path

from app.domain.models import PlanStatus, SourceRoot, UnitKind, UnitStatus
from app.operations.executor import Executor
from app.operations.history import HistoryStore
from app.operations.undo import UndoService
from app.planning.plan import PlanBuilder, PlanOptions
from app.scan.attachments import classify
from app.scan.lyrics_recognizer import (available_encodings, decode_bytes, is_lyrics_file,
                                        load_lyrics)
from app.scan.scanner import Scanner
from tests.fixtures import make_cue, make_wav

LRC_ZH = "[ti:唱]\n[ar:Ado]\n[00:01.00]どうしても消えない\n[00:05.00]君の声が\n"


def make_loose_dir(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    loose = src / "散曲"
    loose.mkdir(parents=True)
    make_wav(loose / "01 - Ado - 唱.wav",
             tags={"title": "唱", "artist": "Ado", "album": "狂言", "track": 1})
    make_wav(loose / "02 - Ado - 踊.wav",
             tags={"title": "踊", "artist": "Ado", "album": "狂言", "track": 2})
    return loose


def scan(paths) -> object:
    return Scanner().scan_source(SourceRoot(path=str(paths)))


def lyrics_of(unit):
    return [a for a in unit.attachments if a.get("role") == "lyrics"]


def build_plan(tmp_path, units, mode="move", target=None):
    opts = PlanOptions(target_root=str(target or (tmp_path / "target")), mode=mode)
    return PlanBuilder(opts).build(units)


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------
def test_lrc_matched_by_name_and_tags(tmp_path):
    loose = make_loose_dir(tmp_path)
    (loose / "01 - Ado - 唱.lrc").write_bytes(LRC_ZH.encode("utf-8"))
    outcome = scan(loose.parent)
    track = [u for u in outcome.units if u.is_track and u.title == "唱"][0]
    atts = lyrics_of(track)
    assert len(atts) == 1
    att = atts[0]
    assert att["match_confidence"] == "high"
    assert att["link_status"] == "auto"
    assert att["selected"] is True           # 自动关联即默认勾选（AC1），取消勾选才不移动
    assert att["has_lrc_timestamps"] is True
    assert att["lyrics_metadata"]["title"] == "唱"
    joined = "".join(att["match_evidence"])
    assert "同名" in joined or "曲号" in joined


def test_txt_log_is_not_lyrics(tmp_path):
    loose = make_loose_dir(tmp_path)
    log = loose / "抓轨说明.txt"
    log.write_text("Exact Audio Copy V1.6\r\n\r\nTrack |  Start  |  Length\r\n"
                   "  1   | 00:00.00 | 03:12.00\r\n", encoding="utf-8")
    assert classify(log) != "lyrics"
    assert is_lyrics_file(log) is False
    outcome = scan(loose.parent)
    assert not any(lyrics_of(u) for u in outcome.units)


def test_txt_is_info_not_lyrics(tmp_path):
    """重构后只认 .lrc：.txt 一律按 info 附件处理，不做内容嗅探。"""
    loose = make_loose_dir(tmp_path)
    text = loose / "歌词 唱.txt"
    text.write_text("唱\nAdo\n\nどうしても消えない\n君の声が\n夜を越えて\n届くまで\n",
                    encoding="utf-8")
    assert classify(text) == "info"
    assert is_lyrics_file(text) is False
    outcome = scan(loose.parent)
    assert not any(lyrics_of(u) for u in outcome.units), ".txt 不得被当作歌词"
    # .txt 作为未关联附件保留（info 角色），不静默丢弃
    info_units = [u for u in outcome.loose_attachments
                  if os.path.normcase(u.path) == os.path.normcase(str(text))]
    assert info_units and info_units[0].attachments[0]["role"] == "info"


def test_encoding_detection_matrix(tmp_path):
    cases = {
        "utf-8": LRC_ZH.encode("utf-8"),
        "utf-8-sig": b"\xef\xbb\xbf" + LRC_ZH.encode("utf-8"),
        "utf-16": LRC_ZH.encode("utf-16"),
        "gb18030": LRC_ZH.encode("gb18030"),
        "cp932": LRC_ZH.encode("cp932"),
    }
    for _name, data in cases.items():
        text, enc = decode_bytes(data)
        assert text.startswith("[ti:唱]"), f"{_name} 解码失败：{enc}"
        info = load_lyrics(_write(tmp_path, data))
        assert info.ok and "唱" in info.text


def _write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / f"lyrics_{abs(hash(data))}.lrc"
    path.write_bytes(data)
    return path


def test_preview_can_switch_encoding(tmp_path):
    path = tmp_path / "a.lrc"
    path.write_bytes(LRC_ZH.encode("gb18030"))
    assert "gb18030" in available_encodings(path)
    correct = load_lyrics(path)
    assert correct.encoding == "gb18030" and "唱" in correct.text
    forced = load_lyrics(path, encoding="utf-8")
    assert forced.ok          # 强制编码也不崩溃（乱码可读）


# ---------------------------------------------------------------------------
# 关联 / 待关联
# ---------------------------------------------------------------------------
def test_ambiguous_lyrics_go_pending(tmp_path):
    loose = make_loose_dir(tmp_path)
    (loose / "lyrics.lrc").write_bytes(
        "[00:01.00]la la la\n[00:03.00]na na na\n".encode("utf-8"))
    outcome = scan(loose.parent)
    assert not any(lyrics_of(u) for u in outcome.units)
    pending = [u for u in outcome.loose_attachments
               if u.status == UnitStatus.NEEDS_LINK.value]
    assert len(pending) == 1
    att = pending[0].attachments[0]
    assert att["link_status"] == "pending"
    assert att["selected"] is False
    assert att["lyrics_candidates"], "待关联必须列出候选"
    assert any("待关联" in m for m in pending[0].messages)


def test_second_language_version_goes_pending(tmp_path):
    loose = make_loose_dir(tmp_path)
    (loose / "01 - Ado - 唱.lrc").write_bytes(LRC_ZH.encode("utf-8"))
    (loose / "01 - Ado - 唱.zh.lrc").write_bytes(
        "[ti:唱]\n[ar:Ado]\n[00:01.00]中文歌词\n".encode("gb18030"))
    outcome = scan(loose.parent)
    track = [u for u in outcome.units if u.is_track and u.title == "唱"][0]
    assert len(lyrics_of(track)) == 1
    pending = [u for u in outcome.loose_attachments
               if u.status == UnitStatus.NEEDS_LINK.value]
    assert len(pending) == 1
    assert "语言版本" in pending[0].messages[0]


def test_cue_album_lyrics_suggestion_without_split(tmp_path):
    src = tmp_path / "src"
    album = src / "[250827]安田レイ - プリズム"
    album.mkdir(parents=True)
    make_wav(album / "VVCL-2788.wav", seconds=0.6)
    make_cue(album / "VVCL-2788.cue", "VVCL-2788.wav", "プリズム", "安田レイ",
             [(1, "プリズム", "安田レイ"), (2, "君の声", "安田レイ")])
    (album / "01 プリズム.lrc").write_bytes(
        "[ti:プリズム]\n[00:01.00]歌詞\n".encode("utf-8"))
    outcome = scan(src)
    unit = [u for u in outcome.units if u.is_album][0]
    assert len(unit.audio_files) == 1, "不得把整轨音频拆成多首"
    atts = lyrics_of(unit)
    assert len(atts) == 1
    assert atts[0]["link_status"] == "auto"
    assert "CUE 曲目" in atts[0]["owner_hint"]
    assert atts[0]["selected"] is True       # 随整夹原样保留


# ---------------------------------------------------------------------------
# 计划与预览
# ---------------------------------------------------------------------------
def test_plan_shows_lyrics_source_and_target(tmp_path):
    loose = make_loose_dir(tmp_path)
    lrc = loose / "01 - Ado - 唱.lrc"
    lrc.write_bytes(LRC_ZH.encode("utf-8"))
    outcome = scan(loose.parent)
    track = [u for u in outcome.units if u.is_track and u.title == "唱"][0]
    track.checked = True
    track.album_artist = "Ado"
    for att in track.attachments:
        if att.get("role") == "lyrics":
            att["selected"] = True
    plan = build_plan(tmp_path, [track], mode="move")
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value
    lyrics_extra = item.extra["lyrics"]
    assert len(lyrics_extra) == 1
    assert lyrics_extra[0]["source"] == str(lrc)
    assert Path(lyrics_extra[0]["target"]).parent == Path(item.target).parent
    text = "\n".join(item.details)
    assert "歌词来源" in text and "歌词目标" in text


def test_unselected_lyrics_excluded_and_reported(tmp_path):
    loose = make_loose_dir(tmp_path)
    lrc = loose / "01 - Ado - 唱.lrc"
    lrc.write_bytes(LRC_ZH.encode("utf-8"))
    outcome = scan(loose.parent)
    track = [u for u in outcome.units if u.is_track and u.title == "唱"][0]
    track.checked = True
    track.album_artist = "Ado"
    for att in track.attachments:
        if att.get("role") == "lyrics":
            att["selected"] = False   # 用户取消勾选（新默认是勾选）
    plan = build_plan(tmp_path, [track], mode="move")
    assert plan.items[0].extra["lyrics"] == []
    assert any(e["status"] == "歌词未勾选" for e in plan.excluded)
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    assert lrc.exists(), "未勾选歌词不得被顺带移动"
    assert any("歌词" in note for note in summary.notes)


# ---------------------------------------------------------------------------
# 执行与撤销
# ---------------------------------------------------------------------------
def test_move_moves_lyrics_and_undo_restores(tmp_path):
    loose = make_loose_dir(tmp_path)
    lrc = loose / "01 - Ado - 唱.lrc"
    data = LRC_ZH.encode("gb18030")
    lrc.write_bytes(data)
    outcome = scan(loose.parent)
    track = [u for u in outcome.units if u.is_track and u.title == "唱"][0]
    track.checked = True
    track.album_artist = "Ado"
    for att in track.attachments:
        if att.get("role") == "lyrics":
            att["selected"] = True
    plan = build_plan(tmp_path, [track], mode="move")
    summary = Executor(tmp_path / "history").execute(plan)
    assert summary.success == 1
    item = plan.items[0]
    album_dir = Path(item.target).parent
    moved = album_dir / lrc.name
    assert moved.is_file()
    assert not lrc.exists()
    assert moved.read_bytes() == data, "歌词内容与编码必须保持不变"

    run = HistoryStore(tmp_path / "history").list_runs()[0]
    assert run.ops[0].lyrics and run.ops[0].lyrics[0]["target"] == str(moved)
    assert "歌词" in run.ops[0].message

    service = UndoService(HistoryStore(tmp_path / "history"))
    undo = service.build(run)
    action = undo.actions[0]
    assert action.lyrics_restore
    service.apply(undo)
    assert lrc.is_file() and lrc.read_bytes() == data
    assert not moved.exists()


def test_copy_mode_copies_lyrics(tmp_path):
    loose = make_loose_dir(tmp_path)
    lrc = loose / "01 - Ado - 唱.lrc"
    lrc.write_bytes(LRC_ZH.encode("utf-8"))
    outcome = scan(loose.parent)
    track = [u for u in outcome.units if u.is_track and u.title == "唱"][0]
    track.checked = True
    track.album_artist = "Ado"
    for att in track.attachments:
        if att.get("role") == "lyrics":
            att["selected"] = True
    plan = build_plan(tmp_path, [track], mode="copy")
    Executor(tmp_path / "history").execute(plan)
    album_dir = Path(plan.items[0].target).parent
    assert (album_dir / lrc.name).is_file()
    assert lrc.is_file(), "复制模式歌词应保留在来源"


def test_lyrics_name_conflict_not_overwritten(tmp_path):
    loose = make_loose_dir(tmp_path)
    lrc = loose / "01 - Ado - 唱.lrc"
    lrc.write_bytes(LRC_ZH.encode("utf-8"))
    outcome = scan(loose.parent)
    track = [u for u in outcome.units if u.is_track and u.title == "唱"][0]
    track.checked = True
    track.album_artist = "Ado"
    for att in track.attachments:
        if att.get("role") == "lyrics":
            att["selected"] = True
    target_root = tmp_path / "target"
    plan = build_plan(tmp_path, [track], mode="move", target=target_root)
    item = plan.items[0]
    album_dir = Path(item.target).parent
    album_dir.mkdir(parents=True, exist_ok=True)
    existing = album_dir / lrc.name
    existing.write_text("已有歌词，不要覆盖", encoding="utf-8")

    plan = build_plan(tmp_path, [track], mode="move", target=target_root)
    item = plan.items[0]
    assert any("同名" in m for m in item.messages)
    planned = item.extra["lyrics"][0]
    assert planned["renamed"] is True and Path(planned["target"]).name != lrc.name

    Executor(tmp_path / "history").execute(plan)
    assert existing.read_text(encoding="utf-8") == "已有歌词，不要覆盖"
    assert Path(planned["target"]).is_file()


def test_full_rip_album_lyrics_kept_verbatim(tmp_path):
    src = tmp_path / "src"
    album = src / "[250827]安田レイ - プリズム"
    album.mkdir(parents=True)
    make_wav(album / "VVCL-2788.wav", seconds=0.4)
    make_cue(album / "VVCL-2788.cue", "VVCL-2788.wav", "プリズム", "安田レイ",
             [(1, "プリズム", "安田レイ")])
    lyric_bytes = "[ti:プリズム]\n[00:01.00]歌詞\n".encode("gb18030")
    (album / "lyrics.JA.lrc").write_bytes(lyric_bytes)
    (album / "歌词.txt").write_text("プリズム\n\n歌詞の一行目\n二行目\n三行目\n", encoding="utf-8")

    outcome = scan(src)
    unit = [u for u in outcome.units if u.is_album][0]
    unit.boundary_confirmed = True
    unit.checked = True
    unit.album_artist = "安田レイ"
    before = {p.name: p.read_bytes() for p in album.iterdir() if p.is_file()}
    plan = build_plan(tmp_path, [unit], mode="move")
    item = plan.items[0]
    assert any("随整夹原样保留" in d for d in item.details)
    Executor(tmp_path / "history").execute(plan)
    target = Path(item.target)
    after = {p.name: p.read_bytes() for p in target.iterdir() if p.is_file()}
    assert before == after, "整夹整理不得改动歌词内容或编码"


LRC_ZH_HANT = ("[ti:細節]\n"
               "[ar:歌手]\n"
               "[00:12.00]細節裡的溫柔\n"
               "[00:25.30]藏在時間的皺褶\n")


def test_big5_lyrics_detected_and_switchable(tmp_path):
    """繁体中文（Big5）歌词：自动检出 big5、预览正常、切换列表包含 Big5。

    回归：候选编码缺 big5 时，Big5 字节被 gb18030 无错解码成乱码并判错编码，
    界面也没有可选的 Big5。"""
    data = LRC_ZH_HANT.encode("big5")
    text, enc = decode_bytes(data)
    assert enc == "big5"
    assert text.startswith("[ti:細節]"), f"Big5 解码失败：{enc}"
    path = _write(tmp_path, data)
    info = load_lyrics(path)
    assert info.ok and info.encoding == "big5" and "細節" in info.text
    assert "big5" in available_encodings(path)


def test_big5_not_misdetected_as_gb18030():
    """同字节的 Big5 与 gb18030 打分竞争：Big5 文本必须胜出（繁中用户直读）。"""
    text, enc = decode_bytes(LRC_ZH_HANT.encode("big5"))
    assert enc == "big5"
    assert "細節" in text and "\ufffd" not in text
