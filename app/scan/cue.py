"""CUE 解析与引用修正。

要求：只修正必需的文件引用，不写回用户编辑的曲名/艺术家；保留原编码与换行；
无法可靠保持编码时标记待手动处理，不悄悄转码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FILE_RE = re.compile(r'^\s*FILE\s+(?:"(?P<quoted>[^"]*)"|(?P<bare>\S+))\s+(?P<type>\S+)', re.IGNORECASE)
_TRACK_RE = re.compile(r"^\s*TRACK\s+(?P<no>\d{1,3})\s+(?P<mode>\S+)", re.IGNORECASE)
_TITLE_RE = re.compile(r'^\s*TITLE\s+(?:"(?P<quoted>[^"]*)"|(?P<bare>.+?))\s*$', re.IGNORECASE)
_PERF_RE = re.compile(r'^\s*PERFORMER\s+(?:"(?P<quoted>[^"]*)"|(?P<bare>.+?))\s*$', re.IGNORECASE)
_INDEX_RE = re.compile(r"^\s*INDEX\s+(?P<idx>\d{1,2})\s+(?P<mm>\d+):(?P<ss>\d+):(?P<ff>\d+)", re.IGNORECASE)

BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig", True),
    (b"\xff\xfe", "utf-16-le", True),
    (b"\xfe\xff", "utf-16-be", True),
)


@dataclass
class CueTrack:
    number: int = 0
    title: str = ""
    performer: str = ""
    index: str = "00:00:00"
    start: float = 0.0
    file_reference: str = ""


@dataclass
class CueSheet:
    path: str = ""
    encoding: str = "utf-8"
    newline: str = "\n"
    bom: bool = False
    text: str = ""
    album_title: str = ""
    album_performer: str = ""
    files: list[str] = field(default_factory=list)
    tracks: list[CueTrack] = field(default_factory=list)
    error: str = ""
    encoding_uncertain: bool = False

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    @property
    def is_single_image(self) -> bool:
        return len(self.tracks) > 1 and len(self.files) == 1

    def resolve_files(self) -> list[str]:
        """把 FILE 引用解析为绝对路径（同名文件,大小写不敏感兜底）。"""
        base = Path(self.path).parent
        resolved: list[str] = []
        try:
            entries = {p.name.casefold(): p for p in base.iterdir() if p.is_file()}
        except OSError:
            entries = {}
        for ref in self.files:
            candidate = base / ref
            if candidate.is_file():
                resolved.append(str(candidate))
                continue
            hit = entries.get(Path(ref).name.casefold())
            if hit is not None:
                resolved.append(str(hit))
        return resolved

    def missing_files(self) -> list[str]:
        resolved = {Path(p).name.casefold() for p in self.resolve_files()}
        return [ref for ref in self.files if Path(ref).name.casefold() not in resolved]


def _score_text(text: str) -> float:
    """给一段解码结果打分，越高越像正常文本（思路同歌词模块的多编码评分）。

    关键区分：GB 字节被 CP932 误解码时会产出大量半角假名；
    正确的日文解码以全角假名为主。
    """
    if not text:
        return -1.0
    bad = 0
    kana = 0
    hankana = 0
    total = len(text)
    for ch in text:
        code = ord(ch)
        if ch == "\ufffd":
            bad += 3
        elif code < 32 and ch not in "\r\n\t":
            bad += 2
        elif 0xE000 <= code <= 0xF8FF:        # 私有区：误码常见
            bad += 2
        elif code in (0x00A0, 0x25A1):        # 常见误码方块
            bad += 1
        elif 0x3040 <= code <= 0x30FF:
            kana += 1
        elif 0xFF66 <= code <= 0xFF9D:
            hankana += 1
    score = -bad / max(total, 1)
    if kana / max(total, 1) > 0.02:
        score += 0.10        # 全角假名 → 强烈指向正确的日文解码
    # 半角假名集中在非 ASCII 字符里且完全没有全角假名
    # → GB 字节被当 CP932 解出的典型乱码（ASCII 头部会稀释全文占比，按非 ASCII 计）
    wide = sum(1 for ch in text if ord(ch) > 0x7F)
    if hankana and kana == 0 and hankana / max(wide, 1) > 0.5:
        score -= 0.30
    return score


def _big5_trail_ratio(raw: bytes) -> float:
    """Big5 字节特征：双字节对的第二字节约四成落在 0x40-0x7E（ASCII 区）；
    GB2312 的双字节对第二字节恒 ≥ 0xA1。用于在 gb18030/big5 之间裁决。"""
    pairs = 0
    ascii_trails = 0
    i = 0
    n = len(raw)
    while i < n - 1:
        b = raw[i]
        if 0xA1 <= b <= 0xF9:
            nxt = raw[i + 1]
            if 0x40 <= nxt <= 0x7E or 0xA1 <= nxt <= 0xFC:
                pairs += 1
                if nxt <= 0x7E:
                    ascii_trails += 1
                i += 2
                continue
        i += 1
    return ascii_trails / pairs if pairs else 0.0


def _decode(raw: bytes) -> tuple[str, str, bool, bool]:
    """返回 (text, encoding, has_bom, uncertain)。

    非 UTF-8 时不再"第一个能解码者赢"：GB18030 几乎能严格解码所有 SJIS
    字节流，旧逻辑会把日文 CUE 静默解成乱码并流入目录名。现在对候选编码
    逐一严格解码并打分；分差不足或存在坏字符时标记 uncertain。
    """
    for bom, enc, _flag in BOMS:
        if raw.startswith(bom):
            body = raw[len(bom):]
            try:
                return body.decode(enc), enc, True, False
            except UnicodeDecodeError:
                break
    try:
        return raw.decode("utf-8"), "utf-8", False, False
    except UnicodeDecodeError:
        pass
    candidates: list[tuple[float, str, str]] = []
    for enc in ("cp932", "gb18030", "big5"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        candidates.append((_score_text(text), enc, text))
    if not candidates:
        return raw.decode("latin-1"), "latin-1", False, True
    # gb18030 / big5 同为 0 分难分高下时，用字节特征裁决（GB2312 第二字节恒 ≥0xA1）。
    # 仅当 cp932 已被排除在榜首之外时才裁决，避免把日文 SJIS 误判成 Big5。
    scores = {enc: s for s, enc, _t in candidates}
    cp932_score = scores.get("cp932")
    if {"gb18030", "big5"} <= set(scores) and \
            (cp932_score is None or cp932_score < max(scores["gb18030"], scores["big5"]) - 0.01):
        ratio = _big5_trail_ratio(raw)
        adjusted: list[tuple[float, str, str]] = []
        for s, enc, t in candidates:
            if enc == "big5" and ratio > 0.2:
                s += 0.15
            elif enc == "gb18030" and ratio < 0.05:
                s += 0.15
            adjusted.append((s, enc, t))
        candidates = adjusted
    # 稳定次序：分数相同时保持插入序（cp932 优先，面向日文抓轨）
    candidates.sort(key=lambda item: -item[0])
    best_score, best_enc, best_text = candidates[0]
    uncertain = False
    if best_score < 0.0:
        uncertain = True      # 有坏字符/私有区 → 解码不可信
    elif len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.03:
        uncertain = True      # 多个候选难分高下（如纯汉字的 SJIS vs GB）
    return best_text, best_enc, False, uncertain


def parse_cue(path: str | Path) -> CueSheet:
    p = Path(path)
    sheet = CueSheet(path=str(p))
    try:
        raw = p.read_bytes()
    except OSError as exc:
        sheet.error = f"无法读取 CUE：{exc.strerror or exc}"
        return sheet
    text, encoding, bom, uncertain = _decode(raw)
    sheet.encoding = encoding
    sheet.bom = bom
    sheet.encoding_uncertain = uncertain
    sheet.text = text
    sheet.newline = "\r\n" if b"\r\n" in raw else "\n"
    _parse_text(sheet)
    if uncertain:
        sheet.error = (f"CUE 编码无法可靠确认（当前按 {encoding} 解读），"
                       "专辑名/艺术家不用于命名，改写引用前需手动确认")
    return sheet


def _unquote(match: re.Match[str]) -> str:
    quoted = match.groupdict().get("quoted")
    if quoted is not None:
        return quoted
    return (match.groupdict().get("bare") or "").strip().strip('"')


def _parse_text(sheet: CueSheet) -> None:
    current_track: CueTrack | None = None
    current_file = ""
    for line in sheet.text.splitlines():
        if not line.strip():
            continue
        m = _FILE_RE.match(line)
        if m:
            name = _unquote(m)
            if name and name not in sheet.files:
                sheet.files.append(name)
            current_file = name
            continue
        m = _TRACK_RE.match(line)
        if m:
            current_track = CueTrack(number=int(m.group("no")), file_reference=current_file)
            sheet.tracks.append(current_track)
            continue
        m = _TITLE_RE.match(line)
        if m:
            value = _unquote(m)
            if current_track is not None:
                if not current_track.title:
                    current_track.title = value
            elif not sheet.album_title:
                sheet.album_title = value
            continue
        m = _PERF_RE.match(line)
        if m:
            value = _unquote(m)
            if current_track is not None:
                if not current_track.performer:
                    current_track.performer = value
            elif not sheet.album_performer:
                sheet.album_performer = value
            continue
        m = _INDEX_RE.match(line)
        if m and current_track is not None and m.group("idx") == "01":
            mm, ss, ff = int(m.group("mm")), int(m.group("ss")), int(m.group("ff"))
            current_track.index = f"{mm:02d}:{ss:02d}:{ff:02d}"
            current_track.start = mm * 60 + ss + ff / 75.0


def _file_line_style(text: str) -> bool:
    """返回 True 表示原有 FILE 行使用引号。"""
    for line in text.splitlines():
        m = _FILE_RE.match(line)
        if m:
            return m.groupdict().get("quoted") is not None
    return True


def rewrite_file_references(sheet: CueSheet, mapping: dict[str, str]) -> tuple[str, list[str]]:
    """按映射改写 FILE 引用，返回 (新文本, 变更说明列表)。

    ``mapping`` 键为原引用（原样），值为新的相对引用。只改 FILE 行，其他内容逐字保留。
    """
    if not mapping:
        return sheet.text, []
    use_quotes = _file_line_style(sheet.text)
    changes: list[str] = []
    out_lines: list[str] = []
    for line in sheet.text.splitlines(keepends=True):
        m = _FILE_RE.match(line)
        if not m:
            out_lines.append(line)
            continue
        old = _unquote(m)
        if old not in mapping:
            out_lines.append(line)
            continue
        new = mapping[old]
        ending = line[len(line.rstrip("\r\n")):]
        ftype = m.group("type") or "WAVE"
        if use_quotes or " " in new:
            new_line = f'FILE "{new}" {ftype}{ending}'
        else:
            new_line = f"FILE {new} {ftype}{ending}"
        out_lines.append(new_line)
        changes.append(f"{old} → {new}")
    return "".join(out_lines), changes


def encode_cue(text: str, encoding: str, bom: bool, newline: str) -> bytes:
    if newline == "\r\n":
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    data = text.encode(encoding, errors="strict")
    if bom and encoding == "utf-8-sig":
        data = b"\xef\xbb\xbf" + text.encode("utf-8")
    elif bom and encoding.startswith("utf-16"):
        data = text.encode(encoding)
    return data
