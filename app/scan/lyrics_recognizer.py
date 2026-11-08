"""外置歌词文件识别与歌曲关联（确定性匹配版）。

歌词扩展名**只认 .lrc**：`.txt` 一律按 info 附件处理，不再做内容嗅探
（旧版对每个 .txt 读盘打分的启发式判定已废除）。

自动关联只允许**同目录三级确定性匹配**（同名 / 去曲号同名 / 唯一标题），
满足任意一级即绑定；其余一律进入“待关联”，绝不自动移动。
打分制（90 分门槛、加减分表）已废除——`rank_candidates` 仅剩手动关联
界面的排序参考职责，不做任何自动决策。

只读：本模块只读取与解码用于预览和判断，不改写文件、不转码、不写音频标签。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from .constants import LYRICS_EXTS

# 支持查看/切换的编码（顺序即自动检测优先级）
SUPPORTED_ENCODINGS = ("utf-8-sig", "utf-8", "utf-16", "utf-16-le", "utf-16-be",
                       "gb18030", "big5", "shift_jis", "cp932")

# 编码的界面显示名
ENCODING_LABELS = {
    "utf-8-sig": "UTF-8 BOM",
    "utf-8": "UTF-8",
    "utf-16": "UTF-16",
    "utf-16-le": "UTF-16 LE",
    "utf-16-be": "UTF-16 BE",
    "gb18030": "GB18030",
    "big5": "Big5（繁体中文）",
    "shift_jis": "Shift_JIS",
    "cp932": "CP932（日文 Windows）",
    "latin-1": "Latin-1（兜底）",
}

# LRC 时间戳：[mm:ss.xx] / [mm:ss.xxx] / [mm:ss]
LRC_TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{2,3}))?\]")
# LRC 元数据标签：[ti:], [ar:], [al:], [by:], [offset:] 等
LRC_META = re.compile(r"^\[(ti|ar|al|au|by|re|ve|offset|length|tr|yr|year|total|genre)\s*:(.*)\]$",
                      re.IGNORECASE)


# ---------------------------------------------------------------------------
# 编码检测与读取
# ---------------------------------------------------------------------------
def _score_decoded(text: str) -> float:
    """给一段解码结果打分，越高越像正常文本（用于在 GB18030 / CP932 之间选择）。"""
    if not text:
        return -1.0
    bad = 0
    for ch in text:
        code = ord(ch)
        if ch == "\ufffd":
            bad += 3
        elif code < 32 and ch not in "\r\n\t":
            bad += 2
        elif 0xE000 <= code <= 0xF8FF:       # 私有区
            bad += 2
        elif code in (0x00A0, 0x25A1):        # 常见误码方块
            bad += 1
    return -bad / max(len(text), 1)


def _kana_ratio(text: str) -> float:
    if not text:
        return 0.0
    kana = sum(1 for ch in text if 0x3040 <= ord(ch) <= 0x30FF)
    return kana / len(text)


def decode_bytes(data: bytes) -> tuple[str, str]:
    """按 BOM / 严格解码 / 打分挑选最可信的编码，返回 (文本, 编码)。

    只尝试解码，不写入任何文件。最后的 latin-1 兜底保证总能预览。
    """
    if not data:
        return "", "utf-8"
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        for enc in ("utf-16", "utf-16-le", "utf-16-be"):
            try:
                return data.decode(enc), enc
            except UnicodeDecodeError:
                continue
    # 无 BOM 的 UTF-16：奇数位大量 NUL
    if len(data) >= 4:
        sample = data[:4096]
        zeros = sample.count(0)
        if zeros > len(sample) * 0.3:
            for enc in ("utf-16-le", "utf-16-be"):
                try:
                    text = data.decode(enc)
                except UnicodeDecodeError:
                    continue
                if _score_decoded(text) > -0.25:
                    return text, enc

    candidates: list[tuple[float, str, str]] = []
    for enc in ("utf-8", "gb18030", "big5", "cp932", "shift_jis"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        score = _score_decoded(text)
        if enc in ("cp932", "shift_jis") and _kana_ratio(text) > 0.02:
            score += 0.05   # 出现假名时更像日文编码
        candidates.append((score, text, enc))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        score, text, enc = candidates[0]
        return text, enc
    try:
        return data.decode("utf-8", errors="replace"), "utf-8"
    except LookupError:  # pragma: no cover
        return data.decode("latin-1", errors="replace"), "latin-1"


def read_text(path: str | Path, encoding: str | None = None) -> tuple[str, str, bool]:
    """读取文本，返回 (文本, 实际编码, 是否成功)。

    encoding 给定时按该编码解码（供用户切换编码预览使用），失败则自动检测。
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return "", encoding or "", False
    if encoding:
        try:
            return data.decode(encoding), encoding, True
        except (UnicodeDecodeError, LookupError):
            pass
    text, detected = decode_bytes(data)
    return text, detected, True


# ---------------------------------------------------------------------------
# .lrc 结构解析（轻量：只取头部标签与时间戳计数，结果带缓存）
# ---------------------------------------------------------------------------
def _normalize_meta(raw: dict[str, str]) -> dict[str, str]:
    mapping = {"ti": "title", "ar": "artist", "al": "album", "au": "author",
               "by": "creator", "re": "creator", "ve": "version", "offset": "offset",
               "length": "duration", "tr": "track", "yr": "year", "year": "year",
               "total": "total_tracks", "genre": "genre"}
    out: dict[str, str] = {}
    for key, value in raw.items():
        mapped = mapping.get(key)
        if mapped:
            out[mapped] = value
    return out


def analyze_lyrics(text: str) -> dict[str, Any]:
    """解析 .lrc 结构：头部标签、时间戳计数、行数（不再做“是不是歌词”打分）。"""
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    non_empty = [ln for ln in lines if ln.strip()]
    stamps = LRC_TIMESTAMP.findall(text)
    meta_hits: dict[str, str] = {}
    meta_lines = 0
    for raw in non_empty:
        m = LRC_META.match(raw.strip())
        if m:
            meta_lines += 1
            key = m.group(1).lower()
            value = m.group(2).strip()
            if value:
                meta_hits[key] = value
    return {
        "metadata": _normalize_meta(meta_hits),
        "meta_lines": meta_lines,
        "timestamp_count": len(stamps),
        "has_lrc_timestamps": bool(stamps),
        "line_count": len(lines),
        "lyric_line_count": len(non_empty) - meta_lines,
    }


_info_cache: dict[tuple[str, int, int], "LyricsInfo"] = {}


def is_lyrics_candidate(path: str | Path) -> bool:
    """扩展名层面是否是歌词（只认 .lrc；.txt 一律不算，弱点 #10 口径统一）。"""
    return Path(path).suffix.lower() in LYRICS_EXTS


def is_lyrics_file(path: str | Path) -> bool:
    """是否按歌词文件处理（= .lrc 扩展名；无内容嗅探）。"""
    return is_lyrics_candidate(path)


@dataclass
class LyricsInfo:
    """一个歌词文件的只读信息（供扫描、预览、计划使用）。"""

    path: str = ""
    encoding: str = ""
    text: str = ""
    ok: bool = False
    error: str = ""
    analysis: dict[str, Any] = field(default_factory=dict)
    preview: str = ""
    signature: tuple[int, int] = (0, 0)

    @property
    def metadata(self) -> dict[str, str]:
        return self.analysis.get("metadata", {}) or {}

    @property
    def has_lrc_timestamps(self) -> bool:
        return bool(self.analysis.get("has_lrc_timestamps"))

    @property
    def name(self) -> str:
        return Path(self.path).name

    def to_attachment(self, *, confidence: str = "high", note: str = "",
                      evidence: Iterable[str] = (), score: float = 0.0,
                      link_status: str = "auto", owner_hint: str = "",
                      selected: bool = True) -> dict[str, Any]:
        """转成 Attachment 字典（可直接存进 Unit.attachments）。"""
        from ..domain.models import Attachment
        att = Attachment(
            path=self.path, role="lyrics", selected=selected, confidence=confidence,
            note=note, owner_hint=owner_hint, is_lyrics=True,
            lyrics_encoding=self.encoding, lyrics_preview=self.preview,
            has_lrc_timestamps=self.has_lrc_timestamps,
            lyrics_metadata=dict(self.metadata),
            match_confidence=confidence, match_evidence=list(evidence),
            match_score=float(score), link_status=link_status,
        )
        return att.to_dict()


def load_lyrics(path: str | Path, encoding: str | None = None,
                preview_lines: int = 40) -> LyricsInfo:
    """读取并解析一个歌词文件（只读；自动检测结果按 (路径,大小,mtime) 缓存，
    避免同一文件在一次扫描/预览流程中被反复读盘——识别机制.md 弱点 #9）。"""
    p = Path(path)
    info = LyricsInfo(path=str(p), encoding=encoding or "")
    try:
        st = os.stat(p)
        info.signature = (st.st_size, int(st.st_mtime))
    except OSError as exc:
        info.error = f"无法读取文件信息：{exc}"
        return info
    if not encoding:
        key = (os.path.normcase(str(p)), st.st_size, int(st.st_mtime))
        cached = _info_cache.get(key)
        if cached is not None:
            return cached
    text, enc, ok = read_text(p, encoding)
    if not ok:
        info.error = "文件读取失败"
        return info
    info.text = text
    info.encoding = enc
    info.ok = True
    info.analysis = analyze_lyrics(text)
    lines = text.splitlines()
    info.preview = "\n".join(lines[:preview_lines])
    if len(lines) > preview_lines:
        info.preview += f"\n…（共 {len(lines)} 行）"
    if not encoding:
        cached = LyricsInfo(path=info.path, encoding=info.encoding, ok=True,
                            analysis=info.analysis, preview=info.preview,
                            signature=info.signature)
        if len(_info_cache) > 500:
            _info_cache.clear()
        _info_cache[key] = cached
    return info


def available_encodings(path: str | Path) -> list[str]:
    """列出该文件可成功解码的编码，供界面切换。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    out: list[str] = []
    for enc in SUPPORTED_ENCODINGS:
        try:
            data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        out.append(enc)
    if not out:
        out.append("utf-8")
    return out


def describe_structure(lyrics: LyricsInfo) -> str:
    """一句人话描述歌词文件特征（用于列表/提示）。"""
    if not lyrics.ok:
        return lyrics.error or "无法读取"
    parts = []
    if lyrics.has_lrc_timestamps:
        parts.append("LRC 时间轴")
    meta = lyrics.metadata
    if meta.get("title"):
        parts.append(f"标题标签：{meta['title']}")
    if meta.get("artist"):
        parts.append(f"艺术家标签：{meta['artist']}")
    parts.append(f"编码 {ENCODING_LABELS.get(lyrics.encoding, lyrics.encoding or '未知')}")
    parts.append(f"{lyrics.analysis.get('line_count', 0)} 行")
    return "｜".join(parts)


# ---------------------------------------------------------------------------
# 候选曲目与三级确定性匹配
# ---------------------------------------------------------------------------
@dataclass
class TrackCandidate:
    """参与匹配的曲目（实体文件或 CUE 虚拟曲目）。"""

    key: str = ""
    name: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    track: int | None = None
    disc: int | None = None
    path: str = ""
    is_virtual: bool = False

    @property
    def stem(self) -> str:
        """用于同名比较的文件主干（虚拟曲目退回显示名）。"""
        return Path(self.path).stem if self.path else self.name


def strip_track_prefix(stem: str) -> str:
    """剥离文件名前缀曲号（``01 -``/``01.``/``01_`` 等），无前缀原样返回。"""
    return re.sub(r"^\s*(?:track[ _]?)?\d{1,3}\s*[-_.、 ]\s*", "", stem,
                  flags=re.IGNORECASE)


@dataclass
class LyricsBinding:
    """一条 .lrc 的匹配结论（三级确定性匹配结果）。"""

    info: LyricsInfo
    candidate: TrackCandidate | None = None   # 绑定到的曲目（None=未绑定）
    level: int = 0                            # 命中的级别 1/2/3；0=未命中
    evidence: list[str] = field(default_factory=list)
    claimed_by: str = ""                      # 命中但曲目已被其它歌词占用时记录占用者


def _same_dir(a: str, b: str) -> bool:
    return bool(a) and bool(b) and \
        os.path.normcase(str(Path(a).parent)) == os.path.normcase(str(Path(b).parent))


def match_lyrics_in_dir(lrc_infos: list[LyricsInfo],
                        candidates: list[TrackCandidate]) -> list[LyricsBinding]:
    """同目录三级确定性匹配（重构方案 §4.1，取代 90 分打分制）。

    匹配优先级（命中任意一级即绑定，置信度 high）：
      1. 同名匹配：``lrc.stem`` 与音频文件主干 casefold 后完全一致；
      2. 去曲号前缀同名匹配：剥离前缀曲号后完全一致；
      3. 同目录唯一标题匹配：``[ti:歌名]`` 与某音频 title 归一化后一致，
         且同目录没有第二首同名歌曲。
    每个曲目只被绑定一次；后续命中同一曲目的歌词标记 claimed_by
    （可能是另一语言版本，转待关联）。禁止跨目录乱连——候选由调用方
    限定在同目录（实体曲目或同目录整轨 CUE 虚拟曲目）。
    """
    claimed: dict[str, str] = {}   # candidate.key -> 占用的歌词文件名
    results: list[LyricsBinding] = []

    titles_index: dict[str, list[TrackCandidate]] = {}
    for cand in candidates:
        title = (cand.title or "").strip().casefold()
        if title:
            titles_index.setdefault(title, []).append(cand)

    for info in lrc_infos:
        binding = LyricsBinding(info=info)
        if info.ok:
            stem_cf = Path(info.path).stem.casefold()
            clean_cf = strip_track_prefix(Path(info.path).stem).casefold()
            lrc_dir = str(Path(info.path).parent)
            target: TrackCandidate | None = None
            level = 0
            evidence: list[str] = []
            # 1 级：主干文件名完全一致
            for cand in candidates:
                if cand.stem.casefold() == stem_cf:
                    target, level = cand, 1
                    evidence = ["文件名与歌曲文件同名"]
                    break
            # 2 级：去曲号后文件名唯一一致
            if target is None and clean_cf:
                hits = [c for c in candidates
                        if strip_track_prefix(c.stem).casefold() == clean_cf]
                if len(hits) == 1:
                    target, level = hits[0], 2
                    evidence = ["去掉曲号前缀后文件名相同"]
            # 3 级：LRC [ti:] 与同目录唯一曲名一致
            if target is None:
                lrc_ti = (info.metadata.get("title") or "").strip()
                if lrc_ti:
                    hits = titles_index.get(lrc_ti.casefold(), [])
                    if len(hits) == 1:
                        target, level = hits[0], 3
                        evidence = [f"LRC 标题与曲名一致（{lrc_ti}）"]
            if target is not None:
                owner = claimed.get(target.key)
                if owner is not None:
                    binding.claimed_by = owner
                else:
                    claimed[target.key] = Path(info.path).name
                    binding.candidate = target
                    binding.level = level
                    binding.evidence = evidence
        results.append(binding)
    return results


def candidate_options(candidates: list[TrackCandidate], limit: int = 8) -> list[dict[str, Any]]:
    """待关联歌词的候选清单（同目录全部曲目，供手动选择；无打分）。"""
    out: list[dict[str, Any]] = []
    for cand in candidates[:limit]:
        out.append({
            "key": cand.key,
            "title": cand.title or cand.name,
            "artist": cand.artist,
            "path": cand.path,
            "virtual": cand.is_virtual,
            "score": 0.0,
            "confidence": "none",
            "evidence": [],
        })
    return out


# ---------------------------------------------------------------------------
# 手动关联界面的排序参考（不再是自动决策依据）
# ---------------------------------------------------------------------------
@dataclass
class LyricsMatch:
    candidate: TrackCandidate
    score: float = 0.0
    confidence: str = "none"
    evidence: list[str] = field(default_factory=list)


def _similarity(a: str, b: str) -> float:
    a = (a or "").strip().casefold()
    b = (b or "").strip().casefold()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def rank_candidates(lyrics: LyricsInfo,
                    candidates: Iterable[TrackCandidate]) -> list[LyricsMatch]:
    """给手动关联界面排序候选（同目录/同名/标题接近靠前）。

    score 仅用于界面排序与“匹配度”列展示，**不再承担自动关联判定**——
    自动关联只走 `match_lyrics_in_dir` 的三级确定性匹配。
    """
    lpath = Path(lyrics.path)
    lstem_cf = lpath.stem.casefold()
    lclean_cf = strip_track_prefix(lpath.stem).casefold()
    lrc_title = (lyrics.metadata.get("title") or "").strip()
    matches: list[LyricsMatch] = []
    for cand in candidates:
        score = 0.0
        evidence: list[str] = []
        cand_stem = cand.stem
        cstem_cf = cand_stem.casefold()
        if _same_dir(lpath.parent, cand.path):
            score += 10
            evidence.append("歌词与歌曲位于同一目录")
        if lstem_cf and lstem_cf == cstem_cf:
            score += 50
            evidence.append("文件名与歌曲文件同名")
        else:
            clean = strip_track_prefix(cand_stem).casefold()
            if lclean_cf and lclean_cf == clean:
                score += 35
                evidence.append("去掉曲号前缀后文件名相同")
            elif _similarity(lpath.stem, cand_stem) >= 0.8:
                score += 20
                evidence.append("文件名与歌曲名高度相似")
        title = (cand.title or "").strip()
        if lrc_title and title:
            sim = _similarity(lrc_title, title)
            if sim >= 0.85:
                score += 40
                evidence.append(f"LRC 标题与曲名一致（{lrc_title}）")
            elif sim >= 0.6:
                score += 18
                evidence.append(f"LRC 标题与曲名接近（{lrc_title}）")
        artist = (lyrics.metadata.get("artist") or "").strip()
        if artist and (cand.artist or "").strip():
            sim = _similarity(artist, cand.artist)
            if sim >= 0.85:
                score += 20
                evidence.append(f"LRC 艺术家与曲目艺术家一致（{artist}）")
            elif sim >= 0.6:
                score += 8
                evidence.append(f"LRC 艺术家与曲目艺术家接近（{artist}）")
        if lyrics.has_lrc_timestamps:
            score += 5
        matches.append(LyricsMatch(candidate=cand, score=score,
                                   confidence="none", evidence=evidence))
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches


def find_pending_partners(matches: list[LyricsMatch], limit: int = 8) -> list[dict[str, Any]]:
    """给界面用：手动关联的候选清单（按排序参考分展示，不做自动决策）。"""
    out: list[dict[str, Any]] = []
    for m in matches[:limit]:
        out.append({
            "key": m.candidate.key,
            "title": m.candidate.title or m.candidate.name,
            "artist": m.candidate.artist,
            "path": m.candidate.path,
            "virtual": m.candidate.is_virtual,
            "score": round(m.score, 1),
            "confidence": m.confidence,
            "evidence": list(m.evidence),
        })
    return out
