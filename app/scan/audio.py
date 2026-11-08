"""音频标签与参数读取（mutagen）。

读取失败必须返回具体原因，不能误报为文件损坏；不同容器支持程度不同，
读取不到就留空，交由文件名猜测和用户补全。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .constants import AUDIO_EXTS  # 统一口径（识别机制.md 弱点 #10 收口）

_LOSSLESS = {".flac", ".wav", ".aiff", ".aif", ".ape", ".wv"}


@dataclass
class AudioMeta:
    path: str = ""
    fmt: str = ""
    duration: float = 0.0
    bitrate: int = 0
    sample_rate: int = 0
    bits: int = 0
    channels: int = 0
    size: int = 0
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track: int | None = None
    disc: int | None = None
    year: str = ""
    genre: str = ""
    error: str = ""
    read_ok: bool = False

    @property
    def fields(self) -> dict[str, object]:
        return {
            "title": self.title, "artist": self.artist, "album": self.album,
            "album_artist": self.album_artist, "track": self.track,
            "disc": self.disc, "year": self.year, "genre": self.genre,
        }

    def describe_audio(self) -> str:
        bits = []
        if self.fmt:
            bits.append(self.fmt.upper())
        if self.bitrate:
            bits.append(f"{self.bitrate // 1000} kbps")
        if self.sample_rate:
            bits.append(f"{self.sample_rate / 1000:g} kHz")
        if self.bits:
            bits.append(f"{self.bits} bit")
        return " · ".join(bits)


def is_audio(path: str | Path) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXTS


def _first(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _first(item)
            if text:
                return text
        return ""
    return str(value).strip()


def _to_int(value) -> int | None:
    text = _first(value)
    if not text:
        return None
    head = text.replace("/", " ").split()[0].strip()
    try:
        return int(float(head))
    except (TypeError, ValueError):
        return None


def _year_from(value) -> str:
    text = _first(value)
    if not text:
        return ""
    for token in text.replace("-", " ").replace("/", " ").split():
        if len(token) >= 4 and token[:4].isdigit():
            year = int(token[:4])
            if 1900 <= year <= 2100:
                return str(year)
    return ""


def read_audio(path: str | Path) -> AudioMeta:
    """读取单个音频文件；任何异常都转成可读原因。"""
    p = Path(path)
    meta = AudioMeta(path=str(p), fmt=p.suffix.lower().lstrip("."))
    try:
        meta.size = p.stat().st_size
    except OSError as exc:
        meta.error = f"无法读取文件信息：{exc.strerror or exc}"
        return meta

    try:
        import mutagen
    except ImportError:  # pragma: no cover - 打包环境缺失时
        meta.error = "缺少音频标签组件 mutagen"
        return meta

    try:
        handle = mutagen.File(str(p), easy=False)
    except Exception as exc:  # noqa: BLE001 - mutagen 抛出的异常种类很多
        meta.error = f"读取失败：{type(exc).__name__}: {exc}"
        return meta
    if handle is None:
        meta.error = "无法识别该音频格式，未能读取标签"
        return meta

    try:
        _fill_info(meta, handle)
        _fill_tags(meta, handle)
        _clean_tag_texts(meta)
        meta.read_ok = True
    except Exception as exc:  # noqa: BLE001
        meta.error = f"解析部分失败：{type(exc).__name__}: {exc}"
    return meta


def _clean_text(value: str) -> str:
    """标签文本清理：控制字符替换为空格并去除首尾空白。

    非法路径字符（\\ / : * ? " < > |）不在读取层改写——保持原标签字符串，
    目标路径生成时由 naming.sanitize_component 统一处理。
    """
    if not value:
        return ""
    return "".join(" " if (ord(ch) < 32 and ch != "\t") else ch
                   for ch in value).strip()


def _clean_tag_texts(meta: AudioMeta) -> None:
    meta.title = _clean_text(meta.title)
    meta.artist = _clean_text(meta.artist)
    meta.album = _clean_text(meta.album)
    meta.album_artist = _clean_text(meta.album_artist)
    meta.genre = _clean_text(meta.genre)


def _fill_info(meta: AudioMeta, handle) -> None:
    info = getattr(handle, "info", None)
    if info is None:
        return
    meta.duration = float(getattr(info, "length", 0.0) or 0.0)
    bitrate = getattr(info, "bitrate", 0) or 0
    if bitrate:
        meta.bitrate = int(bitrate)
    meta.sample_rate = int(getattr(info, "sample_rate", 0) or 0)
    bits = getattr(info, "bits_per_sample", 0) or 0
    meta.bits = int(bits or 0)
    meta.channels = int(getattr(info, "channels", 0) or 0)


def _fill_tags(meta: AudioMeta, handle) -> None:
    tags = getattr(handle, "tags", None)
    if tags is None:
        return
    try:
        from mutagen.id3 import ID3
    except ImportError:  # pragma: no cover
        ID3 = None  # type: ignore
    try:
        from mutagen.mp4 import MP4Tags
    except ImportError:  # pragma: no cover
        MP4Tags = None  # type: ignore

    if ID3 is not None and isinstance(tags, ID3):
        _fill_id3(meta, tags)
        return
    if MP4Tags is not None and isinstance(tags, MP4Tags):
        _fill_mp4(meta, tags)
        return
    _fill_mapping(meta, tags)


def _fill_id3(meta: AudioMeta, tags) -> None:
    def get(frame: str) -> str:
        try:
            value = tags.get(frame)
        except Exception:  # noqa: BLE001
            return ""
        return _first(value)

    meta.title = get("TIT2")
    meta.artist = get("TPE1")
    meta.album = get("TALB")
    meta.album_artist = get("TPE2")
    meta.track = _to_int(get("TRCK"))
    meta.disc = _to_int(get("TPOS"))
    meta.year = _year_from(get("TDRC") or get("TYER") or get("TDOR"))
    meta.genre = get("TCON")


def _fill_mp4(meta: AudioMeta, tags) -> None:
    def get(key: str) -> str:
        try:
            return _first(tags.get(key))
        except Exception:  # noqa: BLE001
            return ""

    meta.title = get("\xa9nam")
    meta.artist = get("\xa9ART")
    meta.album = get("\xa9alb")
    meta.album_artist = get("aART")
    meta.track = _to_int(get("trkn"))
    meta.disc = _to_int(get("disk"))
    meta.year = _year_from(get("\xa9day"))
    meta.genre = get("\xa9gen")


_PROBES = {
    "title": ("title", "TITLE", "Title", "©nam"),
    "artist": ("artist", "ARTIST", "Artist", "Author"),
    "album": ("album", "ALBUM", "Album", "WM/AlbumTitle"),
    "album_artist": ("albumartist", "ALBUMARTIST", "Album Artist", "AlbumArtist",
                     "WM/AlbumArtist", "album artist"),
    "track": ("tracknumber", "TRACKNUMBER", "Track", "TrackNumber", "WM/TrackNumber"),
    "disc": ("discnumber", "DISCNUMBER", "Disc", "WM/PartOfSet"),
    "year": ("date", "DATE", "Year", "YEAR", "WM/Year", "originaldate"),
    "genre": ("genre", "GENRE", "Genre", "WM/Genre"),
}


def _fill_mapping(meta: AudioMeta, tags) -> None:
    lower = {}
    try:
        for key in tags.keys():
            lower.setdefault(str(key).casefold(), key)
    except Exception:  # noqa: BLE001
        return

    def fetch(candidates: tuple[str, ...]) -> str:
        for cand in candidates:
            real = lower.get(cand.casefold())
            if real is None:
                continue
            try:
                value = _first(tags[real])
            except Exception:  # noqa: BLE001
                continue
            if value:
                return value
        return ""

    meta.title = fetch(_PROBES["title"])
    meta.artist = fetch(_PROBES["artist"])
    meta.album = fetch(_PROBES["album"])
    meta.album_artist = fetch(_PROBES["album_artist"])
    meta.track = _to_int(fetch(_PROBES["track"]))
    meta.disc = _to_int(fetch(_PROBES["disc"]))
    meta.year = _year_from(fetch(_PROBES["year"]))
    meta.genre = fetch(_PROBES["genre"])


# ---------------------------------------------------------------------------
# 文件名/目录名猜测
# ---------------------------------------------------------------------------
_YEAR_DIR = re.compile(r"^\s*[\[\(]?(\d{4})[\]\)]?\s*")
# 目录名前缀日期：优先 8 位 YYYYMMDD，其次 6 位 YYMMDD，右侧严禁紧跟数字。
# 旧版 \d{6} 无右边界，曾把 "20250827 新专辑" 截成 "202508" + "27 新专辑"
# （年份错判 2020、专辑名残缺，识别机制.md 弱点 #1）。
_DATE_PREFIX_RE = re.compile(
    r"^\s*[\[\(]?"
    r"(?:(?P<y4>(?:19|20)\d{2})(?P<m4>0[1-9]|1[0-2])(?P<d4>0[1-9]|[12]\d|3[01])"
    r"|(?P<y2>\d{2})(?P<m2>0[1-9]|1[0-2])(?P<d2>0[1-9]|[12]\d|3[01]))"
    r"(?!\d)[\]\)]?\s*[-_.]?\s*"
)
_TRACK_PREFIX = re.compile(r"^\s*(\d{1,3})\s*[-–—._)]\s*(.+)$")
_DASH_SPLIT = re.compile(r"\s+[-–—]\s+")


def _prefix_year(m: re.Match) -> int | None:
    """从 _DATE_PREFIX_RE 的匹配结果解析年份。"""
    if m.group("y4"):
        return int(m.group("y4"))
    if m.group("y2"):
        y2 = int(m.group("y2"))
        return (2000 + y2) if y2 < 70 else (1900 + y2)
    return None


def year_from_text(text: str) -> str:
    """从目录名解析年份，支持 ``[250827]``、``(2019)``、``2019`` 等。"""
    if not text:
        return ""
    m = _DATE_PREFIX_RE.match(text)
    if m:
        year = _prefix_year(m)
        if year and 1900 <= year <= 2100:
            return str(year)
    m = _YEAR_DIR.match(text)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2100:
            return str(year)
    m = re.search(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)", text)
    if m:
        return m.group(1)
    return ""


def extract_dir_year_and_clean_name(dir_name: str) -> tuple[int | None, str]:
    """从目录名提取前缀日期年份，并返回剥离日期后的干净目录名。

    匹配 ``[20250827]``/``20250827``/``[250827]`` 等前缀日期；无日期前缀时
    尝试裸 4 位年份，目录名原样返回（年份属于名称的一部分时不强行拆分）。
    """
    if not dir_name:
        return None, ""
    m = _DATE_PREFIX_RE.match(dir_name)
    if not m:
        year_text = year_from_text(dir_name)
        return (int(year_text) if year_text else None), dir_name.strip()
    year = _prefix_year(m)
    cleaned = dir_name[m.end():].strip()
    return year, (cleaned or dir_name.strip())


def guess_from_filename(path: str | Path) -> dict[str, object]:
    """从文件名猜曲号、艺术家、歌曲名。"""
    stem = Path(path).stem.strip()
    result: dict[str, object] = {"track": None, "artist": "", "title": ""}
    m = _TRACK_PREFIX.match(stem)
    rest = stem
    if m:
        result["track"] = int(m.group(1))
        rest = m.group(2).strip()
    parts = _DASH_SPLIT.split(rest)
    if len(parts) >= 3:
        result["artist"] = parts[0].strip()
        result["title"] = " - ".join(parts[1:]).strip()
    elif len(parts) == 2:
        result["artist"] = parts[0].strip()
        result["title"] = parts[1].strip()
    else:
        result["title"] = rest.strip()
    return result
