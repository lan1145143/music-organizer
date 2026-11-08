import pytest

from app.domain import naming
from app.domain.models import Unit, UnitKind


def test_invalid_chars_become_fullwidth():
    assert naming.sanitize_component('a/b:c*d?e"f<g>h|i\\j') == "a／b：c＊d？e＂f＜g＞h｜i＼j"


def test_trailing_spaces_and_dots_removed():
    assert naming.sanitize_component("专辑名. ") == "专辑名"


def test_reserved_names_prefixed():
    assert naming.sanitize_component("CON") == "_CON"
    assert naming.sanitize_component("com1.txt") == "_com1.txt"


def test_empty_name_fallback():
    assert naming.sanitize_component("   ") == "未命名"
    assert naming.sanitize_component("...") == "未命名"


def _track(**kwargs):
    base = dict(kind=UnitKind.TRACK.value, path="/music/x.flac", title="曲名",
                artist="歌手", album="专辑", album_artist="专辑歌手", track=3, year="2024")
    base.update(kwargs)
    return Unit(**base)


def test_template_a():
    unit = _track()
    assert naming.album_dir_parts(unit, "A") == ["专辑歌手", "专辑"]
    assert naming.track_filename(unit, 2) == "03 - 歌手 - 曲名.flac"


def test_template_b_with_year():
    unit = _track()
    assert naming.album_dir_parts(unit, "B") == ["专辑歌手", "2024 - 专辑"]


def test_template_b_without_year_omits_year():
    unit = _track(year="")
    assert naming.album_dir_parts(unit, "B") == ["专辑歌手", "专辑"]


def test_missing_album_uses_placeholder():
    unit = _track(album="")
    assert naming.album_dir_parts(unit, "A")[1] == "未分类专辑"


def test_artist_falls_back_to_track_artist():
    unit = _track(album_artist="")
    assert naming.album_dir_parts(unit, "A")[0] == "歌手"


def test_missing_track_number_omits_number():
    unit = _track(track=None)
    assert naming.track_filename(unit, 2) == "歌手 - 曲名.flac"


def test_multidisc_adds_cd_dir():
    unit = _track(total_discs=2, disc=2)
    assert naming.album_dir_parts(unit, "A") == ["专辑歌手", "专辑", "CD2"]


def test_track_digits_padded():
    unit = _track(track=7)
    assert naming.track_filename(unit, 3).startswith("007 - ")


def test_missing_artist_fallback():
    """全自动：缺艺术家用"未知艺术家"兜底，不再抛异常阻断。"""
    unit = _track(artist="", album_artist="")
    name = naming.track_filename(unit)
    assert "未知艺术家" in name


def test_path_length_limit():
    ok, length = naming.path_length_ok("C:\\" + "a" * 40)
    assert ok and length == 43
    ok, length = naming.path_length_ok("C:\\" + "a" * 300)
    assert not ok and length > naming.MAX_PATH


def test_unique_name_avoids_existing(tmp_path):
    existing = tmp_path / "封面.jpg"
    existing.write_bytes(b"x")
    assert naming.unique_name(existing).name == "封面 (2).jpg"


def test_normalize_key_case_and_slash():
    assert naming.normalize_key("C:/Music/A/B.MP3") == naming.normalize_key("c:\\music\\a\\b.mp3")
