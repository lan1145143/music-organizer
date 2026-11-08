from pathlib import Path

from app.scan.cue import encode_cue, parse_cue, rewrite_file_references


CUE_TEXT = """PERFORMER "安田レイ"
TITLE "プリズム"
FILE "VVCL-2788.wav" WAVE
  TRACK 01 AUDIO
    TITLE "プリズム"
    PERFORMER "安田レイ"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "君の声"
    INDEX 01 03:12:00
"""


def test_parse_basic(tmp_path):
    path = tmp_path / "a.cue"
    path.write_bytes(CUE_TEXT.encode("utf-8"))
    sheet = parse_cue(path)
    assert sheet.error == ""
    assert sheet.album_title == "プリズム"
    assert sheet.album_performer == "安田レイ"
    assert sheet.files == ["VVCL-2788.wav"]
    assert sheet.track_count == 2
    assert sheet.tracks[0].title == "プリズム"
    assert sheet.tracks[1].start == 3 * 60 + 12
    assert sheet.is_single_image
    assert sheet.newline == "\n"


def test_parse_gb18030(tmp_path):
    text = CUE_TEXT.replace("プリズム", "测试专辑").replace("安田レイ", "测试歌手")
    path = tmp_path / "gb.cue"
    path.write_bytes(text.encode("gb18030"))
    sheet = parse_cue(path)
    assert sheet.encoding == "gb18030"
    assert sheet.album_title == "测试专辑"


def test_crlf_preserved(tmp_path):
    path = tmp_path / "crlf.cue"
    path.write_bytes(CUE_TEXT.replace("\n", "\r\n").encode("utf-8"))
    sheet = parse_cue(path)
    assert sheet.newline == "\r\n"


def test_rewrite_keeps_encoding_and_other_lines(tmp_path):
    path = tmp_path / "gb.cue"
    original = CUE_TEXT.replace("プリズム", "测试专辑")
    path.write_bytes(original.encode("gb18030"))
    sheet = parse_cue(path)
    new_text, changes = rewrite_file_references(sheet, {"VVCL-2788.wav": "03 - 歌手 - 测试专辑.wav"})
    assert changes
    assert 'FILE "03 - 歌手 - 测试专辑.wav" WAVE' in new_text
    assert "TITLE \"测试专辑\"" in new_text
    data = encode_cue(new_text, sheet.encoding, sheet.bom, sheet.newline)
    assert data.decode("gb18030") == new_text


def test_missing_reference_reported(tmp_path):
    path = tmp_path / "m.cue"
    path.write_text(CUE_TEXT, encoding="utf-8")
    sheet = parse_cue(path)
    assert sheet.missing_files() == ["VVCL-2788.wav"]
    assert sheet.resolve_files() == []


def test_resolve_found(tmp_path):
    path = tmp_path / "m.cue"
    path.write_text(CUE_TEXT, encoding="utf-8")
    (tmp_path / "VVCL-2788.wav").write_bytes(b"RIFF")
    sheet = parse_cue(path)
    assert sheet.missing_files() == []
    assert len(sheet.resolve_files()) == 1


def test_rewrite_without_change_returns_original(tmp_path):
    path = tmp_path / "a.cue"
    path.write_text(CUE_TEXT, encoding="utf-8")
    sheet = parse_cue(path)
    text, changes = rewrite_file_references(sheet, {})
    assert changes == []
    assert text == sheet.text
