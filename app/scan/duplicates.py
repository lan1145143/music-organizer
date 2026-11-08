"""重复检测：字节内容（确定重复）。

不遍历现有目标音乐库；不使用音频指纹。疑似重复（标签+时长聚类）已随
识别机制重构废除——同内容才算重复，一律保留各份继续整理，不要求确认。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.models import DuplicateGroup, Unit, UnitKind
from ..task_control import raise_if_cancelled

CHUNK = 1024 * 1024


def hash_file(path: str | Path, progress=None, base: int = 0, total: int = 0) -> str:
    digest = hashlib.sha256()
    done = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
            done += len(block)
            if progress is not None:
                progress(base + done, total)
    return digest.hexdigest()


def audio_signature(unit: Unit, hasher=hash_file) -> str:
    """单元音频内容的组合签名（整夹为所有音频哈希排序后合并）。"""
    if unit.is_track:
        try:
            return hasher(unit.path)
        except OSError:
            return ""
    hashes = []
    for path in sorted(unit.audio_files):
        try:
            hashes.append(hasher(path))
        except OSError:
            continue
    # 按哈希集合排序后拼接：两副本文件名不同（跨来源抓轨常态）时
    # 逐文件哈希集合相同，按路径排序会让拼接顺序不同而判不出重复
    digest = hashlib.sha256("\n".join(sorted(hashes)).encode("ascii"))
    return digest.hexdigest()


def _audio_total_size(unit: Unit) -> int:
    total = 0
    for path in unit.audio_files:
        try:
            total += os.path.getsize(path)
        except OSError:
            continue
    return total


def _describe(unit: Unit) -> str:
    bits = [unit.fmt.upper() or "?", f"{unit.duration:.0f}s"]
    if unit.bitrate:
        bits.append(f"{unit.bitrate // 1000}kbps")
    if unit.sample_rate:
        bits.append(f"{unit.sample_rate / 1000:g}kHz")
    if unit.bits:
        bits.append(f"{unit.bits}bit")
    return " · ".join(bits)


@dataclass
class DupReport:
    groups: list[DuplicateGroup] = field(default_factory=list)
    hash_failures: list[str] = field(default_factory=list)

    @property
    def exact_count(self) -> int:
        return sum(1 for g in self.groups if g.kind == "exact")

    @property
    def suspect_count(self) -> int:
        return 0  # 疑似重复检测已废除（旧界面字段兼容）


def detect_duplicates(units: list[Unit], hasher=hash_file, progress=None,
                      cancel=None) -> DupReport:
    """对参与扫描的单元做确定重复分组。

    预筛策略（识别机制.md 弱点 #9）：
    - 散曲：先按文件大小分组，仅同大小 ≥2 才计算 SHA-256；
    - 整夹：先按 (音频文件数, 音频总字节数) 复合键分组，仅同键 ≥2 才逐文件哈希。
    组 ID = 内容哈希前缀（跨启动稳定；旧 suspect 的进程内 hash() 已随其废除）。
    """
    report = DupReport()
    candidates = [u for u in units if u.kind in (UnitKind.ALBUM.value, UnitKind.TRACK.value)]
    candidates = [u for u in candidates if not u.status_enum.value == "child"]

    # --- 预筛：散曲按大小、整夹按 (文件数, 总字节) 复合键 ---
    by_size: dict[int, list[Unit]] = {}
    by_album_key: dict[tuple[int, int], list[Unit]] = {}
    for unit in candidates:
        if unit.is_track:
            if unit.size > 0:
                by_size.setdefault(unit.size, []).append(unit)
        else:
            key = (len(unit.audio_files), _audio_total_size(unit))
            by_album_key.setdefault(key, []).append(unit)

    exact_groups: dict[str, list[Unit]] = {}
    total = len(candidates)
    for index, unit in enumerate(candidates, start=1):
        # 取消安全点：逐文件哈希之间检查（哈希可能耗时数秒/文件）
        raise_if_cancelled(cancel)
        if progress is not None:
            progress(index, total, unit.name)
        if unit.is_track:
            peers = by_size.get(unit.size, [])
            if len(peers) < 2:
                continue
        else:
            key = (len(unit.audio_files), _audio_total_size(unit))
            if len(by_album_key.get(key, [])) < 2:
                continue
        try:
            signature = audio_signature(unit, hasher)
        except OSError as exc:
            report.hash_failures.append(f"{unit.path}: {exc}")
            continue
        if signature:
            exact_groups.setdefault(signature, []).append(unit)

    used: set[str] = set()
    for signature, members in exact_groups.items():
        if len(members) < 2:
            continue
        ids = [m.id for m in members]
        if any(i in used for i in ids):
            continue
        used.update(ids)
        group = DuplicateGroup(kind="exact", id=f"exact-{signature[:12]}", unit_ids=ids,
                               key=signature)
        for member in members:
            note = ""
            if member.is_album:
                note = "整组重复：仅提示，默认保留完整专辑，不抽走单曲"
            group.detail.append(
                f"{'专辑' if member.is_album else '散曲'}｜{member.display_album or member.name}"
                f"｜{_describe(member)}｜{member.path}{('｜' + note) if note else ''}"
            )
        if any(m.is_album for m in members):
            group.note = "整组重复：默认保留完整专辑，是否整组移到重复文件由用户决定"
        report.groups.append(group)
    return report
