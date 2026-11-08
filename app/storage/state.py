"""工作状态持久化：来源、单元、勾选、编辑、映射、压缩包进度、未完成任务。

使用原子写入 + 上一版备份；重新打开时并用路径/大小/修改时间检查来源变化。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..config import atomic_write_text
from ..domain.models import DuplicateGroup, SourceRoot, Unit
from ..domain.naming import walk_pruned

STATE_VERSION = 1


@dataclass
class AppState:
    version: int = STATE_VERSION
    sources: list[SourceRoot] = field(default_factory=list)
    units: list[Unit] = field(default_factory=list)          # album / track / archive
    loose: list[Unit] = field(default_factory=list)          # 未关联附件
    artist_map: dict[str, str] = field(default_factory=dict)
    duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    selected_source: str = ""
    last_scan_at: str = ""
    notes: list[str] = field(default_factory=list)
    scan_signature: str = ""
    # 重复检测状态：空=从未检测（界面不得显示“无重复”）
    duplicates_checked_at: str = ""

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": STATE_VERSION,
            "sources": [s.to_dict() for s in self.sources],
            "units": [u.to_dict() for u in self.units],
            "loose": [u.to_dict() for u in self.loose],
            "artist_map": self.artist_map,
            "duplicate_groups": [g.to_dict() for g in self.duplicate_groups],
            "selected_source": self.selected_source,
            "last_scan_at": self.last_scan_at,
            "notes": self.notes,
            "scan_signature": self.scan_signature,
            "duplicates_checked_at": self.duplicates_checked_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppState":
        state = cls()
        state.sources = [SourceRoot(**{k: v for k, v in s.items()
                                       if k in SourceRoot.__dataclass_fields__})
                         for s in data.get("sources", [])]
        state.units = [Unit.from_dict(u) for u in data.get("units", [])]
        state.loose = [Unit.from_dict(u) for u in data.get("loose", [])]
        state.artist_map = dict(data.get("artist_map", {}) or {})
        state.duplicate_groups = [DuplicateGroup.from_dict(g)
                                  for g in data.get("duplicate_groups", [])]
        state.selected_source = data.get("selected_source", "")
        state.last_scan_at = data.get("last_scan_at", "")
        state.notes = list(data.get("notes", []) or [])
        state.scan_signature = data.get("scan_signature", "")
        state.duplicates_checked_at = data.get("duplicates_checked_at", "")
        return state

    # ------------------------------------------------------------------
    @property
    def albums(self) -> list[Unit]:
        return [u for u in self.units if u.is_album]

    @property
    def tracks(self) -> list[Unit]:
        return [u for u in self.units if u.is_track]

    @property
    def archives(self) -> list[Unit]:
        return [u for u in self.units if u.is_archive]

    def unit_by_id(self, unit_id: str) -> Unit | None:
        for unit in self.units:
            if unit.id == unit_id:
                return unit
        for unit in self.loose:
            if unit.id == unit_id:
                return unit
        return None

    def apply_artist_map(self) -> int:
        """把映射应用到专辑/散曲的专辑艺术家（不写回文件标签）。"""
        changed = 0
        for unit in self.units:
            if not (unit.album_artist or "").strip():
                continue
            target = self.artist_map.get(unit.album_artist.strip())
            if target and target != unit.album_artist:
                unit.album_artist = target
                unit.origins["album_artist"] = "user"
                changed += 1
        return changed


class StateStore:
    def __init__(self, path: Path, backup: Path | None = None) -> None:
        self.path = Path(path)
        self.backup = Path(backup) if backup else self.path.with_suffix(".bak.json")

    def load(self) -> AppState:
        if not self.path.is_file():
            return AppState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return AppState.from_dict(data)
        except (OSError, ValueError, TypeError):
            # 主文件损坏时尝试备份，再退回空状态；原文件保留为 .corrupt
            try:
                if self.backup.is_file():
                    data = json.loads(self.backup.read_text(encoding="utf-8"))
                    return AppState.from_dict(data)
            except (OSError, ValueError, TypeError):
                pass
            try:
                self.path.replace(self.path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return AppState()

    def save(self, state: AppState) -> None:
        text = json.dumps(state.to_dict(), ensure_ascii=False, indent=1)
        # 上一版整体滚为备份（原子改名），避免每次保存都多读+多写一份大文件
        if self.path.is_file():
            try:
                os.replace(self.path, self.backup)
            except OSError:
                pass
        atomic_write_text(self.path, text)

    def clear(self) -> None:
        for path in (self.path, self.backup):
            try:
                path.unlink()
            except OSError:
                pass


def source_changes(state: AppState) -> list[str]:
    """检查来源目录是否缺失/新增/变化（只在打开或重扫时提示）。"""
    notes: list[str] = []
    for source in state.sources:
        path = Path(source.path)
        if not path.exists():
            notes.append(f"来源已不存在：{source.path}")
        elif not path.is_dir():
            notes.append(f"来源不是目录：{source.path}")
    for unit in state.units:
        if unit.is_album:
            boundary = Path(unit.boundary_path or unit.path)
            if not boundary.is_dir():
                notes.append(f"专辑目录已不存在：{unit.name}")
                continue
            if unit.sig:
                changed = _signature_changed(boundary, unit.sig)
                if changed:
                    notes.append(f"{unit.name}：内部文件有变化（{changed} 个），需重新确认")
        elif unit.is_track:
            if not Path(unit.path).is_file():
                notes.append(f"曲目已不存在：{unit.name}")
        elif unit.is_archive:
            if not Path(unit.path).is_file():
                notes.append(f"压缩包已不存在：{unit.name}")
    return notes


def _signature_changed(root: Path, sig: dict[str, list]) -> int:
    changed = 0
    current = {}
    for dirpath, _dirnames, filenames in walk_pruned(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            current[os.path.normcase(full)] = [st.st_size, int(st.st_mtime)]
    for key, value in sig.items():
        if key not in current or current[key][0] != value[0] or current[key][1] != value[1]:
            changed += 1
    for key in current:
        if key not in sig:
            changed += 1
    return changed


def save_artist_map(path: Path, mapping: dict[str, str]) -> None:
    atomic_write_text(path, json.dumps(mapping, ensure_ascii=False, indent=2))


def stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")
