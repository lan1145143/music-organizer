"""P0-6 回归：CUE 编码识别。

旧实现按 gb18030→cp932 顺序"第一个能解码者赢"，GB18030 几乎能严格解码
所有 SJIS 字节流 → 日文 CUE 必乱码且不标记。新实现多候选评分；
难分高下时标记 uncertain；uncertain 的专辑名不得用于目录命名。
"""

from __future__ import annotations

from pathlib import Path

from app.scan.album_detect import detect_candidates
from app.scan.cue import parse_cue


def _write(tmp_path: Path, name: str, raw: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(raw)
    return p


def test_cp932_japanese_cue_decoded_as_cp932(tmp_path):
    text = ('TITLE "プリズム"\nPERFORMER "安田レイ"\n'
            'FILE "a.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')
    p = _write(tmp_path, "a.cue", text.encode("cp932"))
    sheet = parse_cue(p)
    assert sheet.encoding == "cp932"
    assert sheet.encoding_uncertain is False
    assert sheet.error == ""
    assert sheet.album_title == "プリズム"
    assert sheet.album_performer == "安田レイ"


def test_gb18030_chinese_cue_decoded_as_gb18030(tmp_path):
    text = ('TITLE "周杰伦的专辑"\nPERFORMER "周杰伦"\n'
            'FILE "a.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')
    p = _write(tmp_path, "a.cue", text.encode("gb18030"))
    sheet = parse_cue(p)
    assert sheet.encoding == "gb18030"
    assert sheet.encoding_uncertain is False
    assert sheet.album_title == "周杰伦的专辑"


def test_utf8_cue_certain(tmp_path):
    text = ('TITLE "Ünïcödé Album"\nFILE "a.wav" WAVE\n'
            '  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')
    p = _write(tmp_path, "a.cue", text.encode("utf-8"))
    sheet = parse_cue(p)
    assert sheet.encoding == "utf-8"
    assert sheet.encoding_uncertain is False
    assert sheet.album_title == "Ünïcödé Album"


def test_utf8_bom_cue_certain(tmp_path):
    text = 'TITLE "有BOM"\nFILE "a.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
    p = _write(tmp_path, "a.cue", b"\xef\xbb\xbf" + text.encode("utf-8"))
    sheet = parse_cue(p)
    assert sheet.encoding == "utf-8-sig"
    assert sheet.bom is True
    assert sheet.encoding_uncertain is False


def test_pure_kanji_ambiguous_marks_uncertain(tmp_path):
    """纯汉字（无假名）的 SJIS 与 GB 解码难分高下 → 必须标记 uncertain。"""
    text = 'TITLE "恋愛手帳"\nFILE "a.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
    p = _write(tmp_path, "a.cue", text.encode("cp932"))
    sheet = parse_cue(p)
    assert sheet.encoding_uncertain is True
    assert "编码" in sheet.error


def test_undecodable_falls_back_uncertain(tmp_path):
    p = _write(tmp_path, "a.cue", b"\x81\x81\x81\xfe\xfe")
    sheet = parse_cue(p)
    assert sheet.encoding_uncertain is True
    assert "编码" in sheet.error


def test_uncertain_title_not_used_for_naming(tmp_path):
    """编码不确定的 CUE：专辑名/艺术家不得流入候选命名。

    用纯汉字标题构造 SJIS/GB 难分高下的场景（无假名 → 评分并列 → uncertain）。
    """
    text = ('TITLE "漢字手帳"\nPERFORMER "漢字人物"\n'
            'FILE "a.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')
    p = _write(tmp_path, "a.cue", text.encode("cp932"))
    sheet = parse_cue(p)
    assert sheet.encoding_uncertain is True
    candidates = detect_candidates(root=str(tmp_path), audio_files=[str(tmp_path / "a.wav")],
                                   cue_paths=[str(p)])
    assert all("漢字手帳" not in c.album_title for c in candidates)
    assert all("漢字人物" not in c.album_artist for c in candidates)
    # 原因必须写进 issues，不允许静默丢弃
    assert any("编码无法确认" in i for c in candidates for i in c.issues)
