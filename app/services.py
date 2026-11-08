"""应用服务层：把扫描、计划、执行、重复、历史与撤销串起来，供界面调用。

全自动零干预流水线：所有扫描项自动流转（无策略开关、无解压引擎），
唯一例外是来源失联 → 跳过并写入报告。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Settings, SettingsStore
from .domain import naming
from .domain.models import (OpKind, PlanItem, PlanStatus, SourceRoot,
                            Unit, UnitKind, UnitStatus)
from .operations.executor import Executor, RunSummary
from .operations.history import HistoryStore, RunRecord
from .operations.undo import UndoPlan, UndoService
from .paths import DataPaths
from .planning.plan import Plan, PlanBuilder, PlanOptions
from .planning.review import summarize as group_summarize
from .scan.duplicates import detect_duplicates
from .scan.isolation import (append_isolation_log, isolation_dir, metadata_probe,
                             select_keep, unique_isolation_name)
from .task_control import raise_if_cancelled
from .scan.lyrics_recognizer import (available_encodings, describe_structure,
                                     find_pending_partners, load_lyrics,
                                     rank_candidates, TrackCandidate)
from .scan.scanner import Scanner, dedupe_sources, merge_units

ProgressFn = Callable[[str, int, int], None]

# 计划保留的程序用途目录名，不作为艺术家目录复用候选
PROGRAM_TARGET_DIRS = ("重复文件", "原始压缩包")


def _lyrics_matches_track(att: dict, track_stem: str, title_key: str) -> bool:
    """判断专辑级歌词附件是否明确属于某首曲目（文件名主干/owner_hint 匹配）。"""
    stem = Path(att.get("path", "")).stem.strip().casefold()
    hint = (att.get("owner_hint") or "").strip().casefold()
    targets = {k for k in (track_stem, title_key) if k}
    if hint and hint in targets:
        return True
    if stem and stem in targets:
        return True
    return bool(title_key) and len(title_key) >= 2 and title_key in stem


def _noop(message: str, current: int, total: int) -> None:
    return None


class AppService:
    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths
        self.settings_store = SettingsStore(paths.settings_file)
        self.settings: Settings = self.settings_store.settings
        from .storage.state import StateStore
        self.state_store = StateStore(paths.state_file, paths.state_backup)
        self.state = self.state_store.load()
        # 旧状态迁移：user_checked 为空的单元按全自动规则应用默认勾选
        self.apply_default_selection()
        self.history = HistoryStore(paths.history_dir)
        self.undo_service = UndoService(self.history)
        self.scanner = Scanner()
        self.last_plan: Plan | None = None
        self.last_run: RunSummary | None = None
        self.startup_notes: list[str] = []

    # ------------------------------------------------------------------
    # 设置与状态
    # ------------------------------------------------------------------
    def save_settings(self) -> None:
        self.settings_store.save(self.settings)

    def save_state(self) -> None:
        self.state_store.save(self.state)

    def startup_check(self) -> list[str]:
        """打开时检查来源变化与未完成任务。"""
        from .storage.state import source_changes
        notes = list(source_changes(self.state))
        for run in self.history.interrupted():
            notes.append(f"发现未完成的操作记录：{run.run_id}（可在历史中查看并恢复/撤销）")
        if not self.paths.portable:
            notes.append(f"程序目录不可写，数据已保存到：{self.paths.root}")
        self.startup_notes = notes
        return notes

    # ------------------------------------------------------------------
    # 来源与扫描
    # ------------------------------------------------------------------
    def register_sources(self, paths: list[str]) -> dict:
        """登记来源（去重、校验是目录），但不扫描。"""
        added = 0
        skipped: list[str] = []
        existing = {os.path.normcase(os.path.normpath(s.path)) for s in self.state.sources}
        for raw in paths:
            path = str(Path(raw).resolve())
            key = os.path.normcase(os.path.normpath(path))
            if key in existing:
                skipped.append(f"重复来源已忽略：{path}")
                continue
            if not Path(path).is_dir():
                skipped.append(f"不是文件夹：{path}")
                continue
            self.state.sources.append(SourceRoot(path=path, added_at=datetime.now().isoformat(
                timespec="seconds"), scanned=False))
            existing.add(key)
            added += 1
        self.state.sources, notes = dedupe_sources(self.state.sources)
        skipped.extend(notes)
        if added:
            self.save_state()
        return {"added": added, "skipped": skipped}

    def has_pending_scan(self) -> bool:
        """是否存在已登记但尚未扫描的来源（等待确认目标）。"""
        return any(not s.scanned for s in self.state.sources)

    def add_sources(self, paths: list[str], progress: ProgressFn = _noop) -> dict:
        """兼容入口：登记并立即扫描（现有调用方/端到端脚本使用）。"""
        result = self.register_sources(paths)
        if result["added"]:
            self.rescan(progress)
        return {"added": result["added"], "skipped": result["skipped"]}

    def remove_source(self, source_id: str) -> None:
        self.state.sources = [s for s in self.state.sources if s.id != source_id]
        self.state.units = [u for u in self.state.units if u.source_id != source_id]
        self.save_state()

    def drop_units(self, unit_ids: list[str]) -> int:
        """整理完成后把已处理的单元从列表移除（不删磁盘文件，仅清列表条目）。
        同时清掉随父整夹处理的子曲目与关联失联项。"""
        if not unit_ids:
            return 0
        dropped = set(unit_ids)
        for unit in self.state.units:
            if unit.is_track and unit.status == UnitStatus.CHILD.value:
                for album_id in unit_ids:
                    album = self.state.unit_by_id(album_id)
                    if album is not None and album.is_album and \
                            self._track_in_album(track=unit, album=album):
                        dropped.add(unit.id)
                        break
        before = len(self.state.units)
        self.state.units = [u for u in self.state.units if u.id not in dropped]
        self.state.loose = [u for u in self.state.loose if u.id not in dropped]
        for group in self.state.duplicate_groups:
            group.unit_ids = [uid for uid in group.unit_ids if uid not in dropped]
        self.state.duplicate_groups = [g for g in self.state.duplicate_groups
                                       if len(g.unit_ids) >= 2]
        removed = before - len(self.state.units)
        if removed:
            self.save_state()
        return removed

    def clear_all(self) -> int:
        """清空整理列表与来源（不删除任何磁盘文件，历史记录保留）。"""
        removed = len(self.state.sources) + len(self.state.units) + len(self.state.loose)
        self.state.sources = []
        self.state.units = []
        self.state.loose = []
        self.state.duplicate_groups = []
        self.state.notes = []
        self.save_state()
        return removed

    def rescan(self, progress: ProgressFn = _noop, cancel=None) -> dict:
        sources, notes = dedupe_sources(self.state.sources)
        self.state.sources = sources
        fresh: list[Unit] = []
        loose: list[Unit] = []
        errors: list[str] = list(notes)
        total = max(len(sources), 1)
        for index, source in enumerate(sources, start=1):
            # 取消安全点：来源之间检查。此时尚未合并任何单元、未写 scanned
            # 标记（scanned 统一在合并成功后置位），取消后内存与磁盘状态一致。
            raise_if_cancelled(cancel)
            progress(f"扫描 {Path(source.path).name}…", index - 1, total)
            outcome = self.scanner.scan_source(source)
            fresh.extend(outcome.units)
            fresh.extend(outcome.archives)
            loose.extend(outcome.loose_attachments)
            errors.extend(outcome.errors)
            progress(f"完成 {Path(source.path).name}（{len(outcome.units)} 项）", index, total)
        merged, merge_notes = merge_units(self.state.units, fresh)
        errors.extend(merge_notes)
        self._preserve_manual_attachments(self.state.units, merged)
        self.state.units = merged
        self.state.loose = loose
        for source in sources:
            source.scanned = True
        # 识别机制重构：散曲元数据在扫描期逐首直读定稿（tag → 目录名 → 兜底），
        # 不再进行跨来源散曲专辑聚合与 7 级艺术家推断（album_infer/artist_infer 已删）。
        self.state.last_scan_at = datetime.now().isoformat(timespec="seconds")
        self.state.apply_artist_map()
        # 新增项目按全自动规则应用默认勾选；用户显式勾选/取消的保持不变
        self.apply_default_selection(merged)
        self.state.notes = errors
        self.save_state()
        return {
            "albums": len([u for u in merged if u.is_album]),
            "tracks": len([u for u in merged if u.is_track]),
            "archives": len([u for u in merged if u.is_archive]),
            "loose": len(loose),
            "errors": errors,
        }

    def _preserve_manual_attachments(self, old_units: list[Unit], new_units: list[Unit]) -> None:
        old_map = {u.id: u for u in old_units}
        for unit in new_units:
            old = old_map.get(unit.id)
            if old is None:
                continue
            known = {os.path.normcase(a.get("path", "")) for a in unit.attachments}
            for att in old.attachments:
                if att.get("confidence") != "manual":
                    continue
                ikey = os.path.normcase(att.get("path", ""))
                if ikey in known:
                    continue
                if Path(att.get("path", "")).exists():
                    unit.attachments.append(dict(att))
                    known.add(ikey)

    # ------------------------------------------------------------------
    # 勾选
    # ------------------------------------------------------------------
    def set_checked(self, units: list[Unit], checked: bool, explicit: bool = True) -> None:
        """设置勾选。用户操作（explicit=True）同时记录 user_checked，
        重扫/重新分组时不再覆盖。"""
        for unit in units:
            unit.checked = checked
            if explicit:
                unit.user_checked = checked
            if unit.is_album and not checked:
                for track in self.state.tracks:
                    if self._track_in_album(track, unit):
                        track.checked = False
                        if explicit:
                            track.user_checked = False

    def apply_default_selection(self, units: list[Unit] | None = None) -> int:
        """全自动默认勾选：除来源失联外，所有单元 100% 默认勾选。

        用户显式勾选/取消过（user_checked 非 None）的一律保持。
        """
        units = self.state.units if units is None else units
        changed = 0
        for unit in units:
            if unit.status == UnitStatus.MISSING.value:
                if unit.checked:
                    changed += 1
                unit.checked = False
                continue
            if unit.user_checked is not None:
                unit.checked = unit.user_checked
                continue
            if not unit.checked:
                unit.checked = True
                changed += 1
        return changed

    def group_summary(self) -> dict:
        """统一分组计数（主列表、整理中心共用）。"""
        return group_summarize(self.state.units)

    # ------------------------------------------------------------------
    # 目标目录：统一验证 / 保存 / 变更重算
    # ------------------------------------------------------------------
    def validate_target(self, path: str) -> dict:
        """统一目标验证：目标选择器、手输、引导确认与执行前共用同一规则。

        - 目标不能为空、必须是可使用的文件夹（存在或可创建）；
        - 检查全部已登记来源（不只本批）：相同或互为祖先/后代一律拒绝，
          按 Windows 路径语义比较（大小写、末尾分隔符、`..`、联接/符号链接同址）；
        - 可写性用临时文件探测并立即清理，不留测试文件。
        返回 {"ok": bool, "reason": str, "exists": bool}；不修改任何配置。
        """
        path = (path or "").strip()
        if not path:
            return {"ok": False, "reason": "目标目录未选择", "exists": False}
        target = Path(path)
        exists = target.exists()
        if exists and not target.is_dir():
            return {"ok": False, "reason": f"目标不是文件夹：{path}", "exists": True}
        for source in self.state.sources:
            if naming.paths_related(path, source.path):
                same = (os.path.normcase(os.path.normpath(naming.real_path(path)))
                        == os.path.normcase(os.path.normpath(naming.real_path(source.path))))
                reason = (f"目标不能与来源相同：{source.path}" if same else
                          f"目标与来源互相包含（会造成自我复制/递归扫描）：{source.path}")
                return {"ok": False, "reason": reason, "exists": exists}
        if not exists:
            probe = target.parent
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            if not os.access(probe, os.W_OK):
                return {"ok": False, "reason": f"目标上级目录不可写：{probe}", "exists": False}
            return {"ok": True, "reason": "目标不存在，确认后将创建", "exists": False}
        try:
            probe_file = target / ".target_write_test"
            probe_file.write_text("", encoding="utf-8")
            probe_file.unlink()
        except OSError as exc:
            return {"ok": False, "reason": f"目标不可写：{exc.strerror or exc}", "exists": True}
        return {"ok": True, "reason": "", "exists": True}

    def set_target(self, path: str) -> dict:
        """验证并保存目标；路径变化时重算受影响内容。

        验证失败不覆盖最后一次有效配置。
        """
        result = self.validate_target(path)
        if not result["ok"]:
            return result
        path = str(Path(path).resolve()) if path else ""
        changed = (os.path.normcase(os.path.normpath(path))
                   != os.path.normcase(os.path.normpath(self.settings.target_dir)))
        if changed:
            self.settings.target_dir = path
            self.save_settings()
            self.apply_target_change()
        return {"ok": True, "reason": result.get("reason", ""),
                "exists": result["exists"], "changed": changed}

    def apply_target_change(self) -> dict:
        """更换目标后：使旧计划失效（计划永远重建），保留用户编辑与显式勾选。

        识别机制重构后不再有依赖目标目录的艺术家推断建议，无需重估。
        """
        self.last_plan = None
        self.save_state()
        return {"plan_invalidated": True}

    # ------------------------------------------------------------------
    # 艺术家目录解析：已有目录精确复用 / 新建
    # ------------------------------------------------------------------
    @staticmethod
    def _program_dir_keys() -> set[str]:
        return {name.casefold() for name in PROGRAM_TARGET_DIRS}

    def resolve_artist_dir(self, artist: str) -> tuple[str, bool]:
        """解析整理艺术家对应的目标一级目录名。

        返回 (目录名, 是否复用已有目录)。按 Windows 语义大小写不敏感精确匹配，
        不受展示数量限制；程序用途目录（重复文件/原始压缩包）不参与复用；
        同名非目录条目不自动合并，返回清洗名由预览提示冲突。
        """
        artist = (artist or "").strip()
        sanitized = naming.sanitize_artist(artist)
        if not artist:
            return sanitized, False
        root = self.settings.target_dir
        if not root or not Path(root).is_dir():
            return sanitized, False
        want = {artist.casefold(), sanitized.casefold()}
        excluded = self._program_dir_keys()
        try:
            entries = sorted(Path(root).iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            return sanitized, False
        for entry in entries:
            key = entry.name.casefold()
            if key in excluded:
                continue
            if key in want:
                if entry.is_dir():
                    return entry.name, True
                return sanitized, False  # 与现有文件重名：按新建路径提示冲突
        return sanitized, False

    @staticmethod
    def _track_in_album(track: Unit, album: Unit) -> bool:
        boundary = os.path.normcase(os.path.normpath(album.boundary_path or album.path))
        key = os.path.normcase(os.path.normpath(os.path.dirname(track.path)))
        return key == boundary or key.startswith(boundary + os.sep)

    def update_unit_fields(self, unit: Unit, values: dict) -> None:
        for field, value in values.items():
            if not hasattr(unit, field):
                continue
            setattr(unit, field, value)
            unit.origins[field] = "user"
        self._refresh_status(unit)

    def _refresh_status(self, unit: Unit) -> None:
        """全自动双态机：专辑/散曲/压缩包一律 READY，失联保留 MISSING。"""
        if unit.status == UnitStatus.MISSING.value:
            return
        if unit.is_album or unit.is_track or unit.is_archive:
            unit.status = UnitStatus.READY.value

    def confirm_boundary(self, unit: Unit, path: str | None = None) -> None:
        """用户改选边界（全自动模式下边界始终生效，改选即生效）。"""
        if path:
            unit.boundary_path = path
            unit.name = Path(path).name
        unit.boundary_confirmed = True
        self._refresh_status(unit)

    def unconfirm_boundary(self, unit: Unit) -> None:
        """全自动模式下取消确认没有意义：立即恢复 READY 并保留自动边界。"""
        unit.boundary_confirmed = True
        self._refresh_status(unit)

    def set_album_mode(self, unit: Unit, mode: str) -> None:
        unit.mode_hint = mode
        if mode == "tracks":
            unit.checked = False
            self.ensure_track_units(unit)
        self.save_state()

    def ensure_track_units(self, album: Unit) -> int:
        """把专辑内部音频拆成散曲单元（用户选择"按散曲整理"时）。"""
        created = 0
        created_units: list[Unit] = []
        from .scan.audio import read_audio, guess_from_filename
        existing = {os.path.normcase(u.path) for u in self.state.tracks}
        for path in album.audio_files:
            if os.path.normcase(path) in existing:
                continue
            meta = read_audio(path)
            unit = Unit(kind=UnitKind.TRACK.value, source_id=album.source_id, path=path,
                        name=Path(path).stem)
            unit.title = meta.title
            unit.artist = meta.artist
            unit.album = meta.album or album.album
            unit.album_artist = meta.album_artist or album.album_artist
            unit.track = meta.track
            unit.disc = meta.disc
            unit.year = meta.year or album.year
            unit.genre = meta.genre or album.genre
            unit.duration = meta.duration
            unit.fmt = meta.fmt
            unit.bitrate = meta.bitrate
            unit.sample_rate = meta.sample_rate
            unit.bits = meta.bits
            unit.size = meta.size
            guessed = guess_from_filename(path)
            for field, value, origin in (
                ("title", meta.title or str(guessed.get("title") or ""), "tag"),
                ("artist", meta.artist or str(guessed.get("artist") or ""), "tag"),
                ("track", meta.track or guessed.get("track"), "tag"),
                ("album", meta.album or album.album, "tag"),
            ):
                if value:
                    setattr(unit, field, value)
                    unit.origins[field] = origin
            # 附件归属：只有能明确对应到这首曲子的歌词才挂到曲目上。
            # 专辑级附件（封面/扫图/日志/未归属歌词）保留在专辑单元，
            # 由计划层随专辑目录一次性搬运——否则移动模式下第一首搬走后
            # 其余曲目报"来源不存在"，复制模式下每首各复制一份封面。
            track_stem = Path(path).stem.strip().casefold()
            title_key = (meta.title or unit.title or "").strip().casefold()
            for att in album.attachments:
                if not att.get("selected") or att.get("role") != "lyrics":
                    continue
                if _lyrics_matches_track(att, track_stem, title_key):
                    unit.attachments.append(dict(att))
            self._refresh_status(unit)
            self.state.units.append(unit)
            created_units.append(unit)
            existing.add(os.path.normcase(path))
            created += 1
        if created:
            self.apply_default_selection(created_units)
            self.save_state()
        return created

    def link_attachment(self, unit: Unit, path: str, role: str = "") -> None:
        from .scan.attachments import classify
        role = role or classify(path)
        if role == "lyrics":
            self.link_lyrics(unit, path, selected=True)
            return
        att = {"path": path, "role": role, "selected": True,
               "confidence": "manual", "note": "手动关联", "owner_hint": "手动"}
        unit.attachments = [a for a in unit.attachments
                            if os.path.normcase(a.get("path", "")) != os.path.normcase(path)]
        unit.attachments.append(att)
        self.state.loose = [u for u in self.state.loose
                            if os.path.normcase(u.path) != os.path.normcase(path)]
        self.save_state()

    def clear_attachments(self, unit: Unit) -> int:
        """解除该单元的全部附件关联（详情面板「清空关联」）。

        不删除任何磁盘文件；自动识别的附件重新扫描后可恢复，
        手动关联的需重新关联。返回解除的关联数量。
        """
        removed = len(unit.attachments)
        if removed:
            unit.attachments = []
            self.save_state()
        return removed

    def suggest_artist(self, raw: str) -> str:
        return self.state.artist_map.get(raw.strip(), "")

    # ------------------------------------------------------------------
    # 艺术家目录候选（批量指定界面用）
    # ------------------------------------------------------------------
    def existing_artist_dirs(self, keyword: str = "", limit: int = 30) -> list[dict]:
        """目标目录下已存在的艺术家文件夹（一级子目录），支持关键字过滤。

        排除程序用途目录（重复文件/原始压缩包）；limit 只影响展示，
        不影响精确匹配（resolve_artist_dir 不限量）。
        """
        root = self.settings.target_dir
        if not root or not Path(root).is_dir():
            return []
        needle = keyword.strip().casefold()
        excluded = self._program_dir_keys()
        found: list[dict] = []
        try:
            entries = sorted(Path(root).iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            return []
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name.casefold() in excluded:
                continue
            if needle and needle not in entry.name.casefold():
                continue
            found.append({"name": entry.name, "path": str(entry)})
            if len(found) >= limit:
                break
        return found

    def preview_artist_target(self, artist: str, unit: Unit) -> str:
        """指定整理艺术家后该单元的目标位置预览（不创建任何目录）。"""
        if not self.settings.target_dir:
            return ""
        try:
            artist_dir, _reused = self.resolve_artist_dir(artist)
            if unit.is_album:
                # 专辑层取元数据专辑名（与 naming.album_target_dir 同一规则）
                folder = (unit.album or "").strip() or naming.UNKNOWN_ALBUM
                return str(Path(self.settings.target_dir)
                           / artist_dir / naming.sanitize_album(folder))
            if unit.is_track:
                probe = Unit(kind=UnitKind.TRACK.value, path=unit.path,
                             title=unit.title, artist=unit.artist, album=unit.album,
                             album_artist=artist, track=unit.track, total_discs=unit.total_discs,
                             disc=unit.disc, fmt=unit.fmt)
                return str(naming.track_target_path(self.settings.target_dir, probe,
                                                    self.settings.template,
                                                    self.settings.track_digits,
                                                    artist_dir=artist_dir))
        except naming.NamingError as exc:
            return f"（仍缺必要信息：{exc}）"
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def assign_organize_artist(self, units: list[Unit], artist: str) -> dict:
        """把"整理用专辑艺术家"批量指定给所选单元（仅目录归属，不改标签）。"""
        artist = (artist or "").strip()
        if not artist or not units:
            return {"albums": 0, "tracks": 0, "mapped": 0, "still_missing": []}
        albums = tracks = mapped = 0
        still_missing: list[str] = []
        for unit in units:
            raw_key = (unit.album_artist or unit.artist or unit.name or "").strip()
            if raw_key and self.state.artist_map.get(raw_key) != artist:
                self.state.artist_map[raw_key] = artist
                mapped += 1
            unit.album_artist = artist
            unit.origins["album_artist"] = "user"
            if unit.is_album:
                albums += 1
            elif unit.is_track:
                tracks += 1
            self._refresh_status(unit)
            # 收集仍缺的信息（全自动下只是提醒，不拦截），供界面明示
            leftovers = [m for m in unit.messages if "艺术家" not in m
                         and ("缺少" in m or "未识别" in m)]
            if leftovers:
                still_missing.append(f"{unit.name}：{leftovers[0]}")
        from .storage.state import save_artist_map
        save_artist_map(self.paths.artist_map_file, self.state.artist_map)
        self.save_state()
        return {"albums": albums, "tracks": tracks, "mapped": mapped,
                "still_missing": still_missing}

    # ------------------------------------------------------------------
    # 重复（全自动：保留各份，检测仅提供信息）
    # ------------------------------------------------------------------
    def run_duplicate_check(self, progress: ProgressFn = _noop, cancel=None) -> dict:
        def dup_progress(cur: int, total: int, name: str) -> None:
            progress(f"比较重复：{name}", cur, total)

        report = detect_duplicates(self.state.units, progress=dup_progress, cancel=cancel)
        keep_old = {g.id: g.keep_unit_id for g in self.state.duplicate_groups if g.keep_unit_id}
        for unit in self.state.units:
            unit.duplicate_group = ""
            unit.dup_note = ""
        for group in report.groups:
            group.keep_unit_id = keep_old.get(group.id, "")
            for uid in group.unit_ids:
                unit = self.state.unit_by_id(uid)
                if unit is None:
                    continue
                unit.duplicate_group = group.id
                unit.dup_note = group.note or group.kind
        self.state.duplicate_groups = report.groups
        self.state.duplicates_checked_at = datetime.now().isoformat(timespec="seconds")
        self.save_state()
        return {"exact": report.exact_count, "suspect": report.suspect_count,
                "failures": report.hash_failures}

    # ------------------------------------------------------------------
    # 重复隔离（非破坏性：副本移入来源根的「a重复歌曲」，绝不删除）
    # ------------------------------------------------------------------
    @staticmethod
    def _isolation_display_path(unit: Unit) -> str:
        return unit.boundary_path or unit.path

    def duplicate_isolation_groups(self, keep_overrides: dict[str, str] | None = None,
                                   fresh_probe: bool = True) -> list[dict]:
        """把查重结果整理成隔离分组：每组 1 个保留版本 + 待隔离副本。

        - 组内优选：无损 > 码率/采样率 > 元数据完整度 > 体积/时间（§2.1）；
        - keep_overrides：弹窗中用户手动切换过的保留项（group_id → unit_id）；
        - 失联/文件已不存在的成员剔除，剩余不足 2 个的组不计入。
        """
        keep_overrides = keep_overrides or {}
        cache: dict[str, tuple[int, int, int]] = {}

        def probe(path: str) -> tuple[int, int, int]:
            key = os.path.normcase(os.path.normpath(path))
            if key not in cache:
                cache[key] = metadata_probe(path) if fresh_probe else (0, 0, 0)
            return cache[key]

        units_by_id = {u.id: u for u in self.state.units}
        groups: list[dict] = []
        for group in self.state.duplicate_groups:
            members: list[Unit] = []
            for uid in group.unit_ids:
                unit = units_by_id.get(uid)
                if unit is None or unit.status == UnitStatus.MISSING.value:
                    continue
                if not Path(self._isolation_display_path(unit)).exists():
                    continue
                members.append(unit)
            if len(members) < 2:
                continue
            preferred = keep_overrides.get(group.id, "") or group.keep_unit_id
            keep, isolates = select_keep(members, preferred=preferred, probe=probe)
            groups.append({"id": group.id, "kind": group.kind,
                           "keep": keep, "isolates": isolates,
                           "members": members})
        return groups

    def isolate_duplicates(self, keep_overrides: dict[str, str] | None = None,
                           progress: ProgressFn = _noop, cancel=None) -> dict:
        """执行非破坏性隔离：每组保留最佳版本，其余移入来源根「a重复歌曲」。

        - 通过 Executor 走 DUPLICATE_MOVE 全链路（journal/manifest/历史/撤销）；
        - 目标重名（含批次内撞车）自动追加 _dup1/_dup2，绝不覆盖任何文件；
        - 成功项逐条追加 isolation_log.txt；失败项（文件被占用/失联）保留在
          列表与重复组中，可随时重试。
        """
        groups = self.duplicate_isolation_groups(keep_overrides)
        items: list[PlanItem] = []
        used_names: dict[str, set[str]] = {}
        roots: set[str] = set()
        for group in groups:
            keep = group["keep"]
            keep_path = self._isolation_display_path(keep)
            for unit in group["isolates"]:
                source = next((s for s in self.state.sources
                               if s.id == unit.source_id), None)
                if source is None or not source.path:
                    continue    # 来源已被移除的单元不参与隔离
                iso_dir = isolation_dir(source.path)
                used = used_names.setdefault(os.path.normcase(str(iso_dir)), set())
                if unit.is_album:
                    raw_name = Path(self._isolation_display_path(unit)).name
                else:
                    raw_name = Path(unit.path).name
                target_name = unique_isolation_name(iso_dir, raw_name, used)
                item = PlanItem(
                    op=OpKind.DUPLICATE_MOVE.value, unit_id=unit.id,
                    unit_kind=unit.kind, label=f"重复隔离 · {unit.name}",
                    source=self._isolation_display_path(unit),
                    target=str(iso_dir / target_name), action="move")
                item.extra = {"unit": unit, "kind": group["id"],
                              "group_id": group["id"], "keep_path": keep_path}
                if unit.is_album:
                    item.extra["boundary"] = self._isolation_display_path(unit)
                item.add_message(f"组内保留版本：{keep_path}")
                if not Path(item.source).exists():
                    item.status = PlanStatus.SKIPPED.value
                    item.add_message("来源不存在，本次跳过")
                else:
                    roots.add(source.path)
                items.append(item)

        plan = Plan(items=items, options=PlanOptions(
            target_root=next(iter(roots)) if len(roots) == 1 else "",
            mode="move", verify_hash=self.settings.verify_hash))
        plan.messages.append(
            "非破坏性隔离：多余副本移入来源根目录的「a重复歌曲」，保留最佳版本原位，"
            "全程不删除任何文件；可在历史中撤销。")

        executor = Executor(self.paths.history_dir, verify_hash=self.settings.verify_hash)
        summary = executor.execute(plan, progress=progress, cancel=cancel)

        item_by_id = {i.id: i for i in plan.items}
        moved_ids: list[str] = []
        iso_dirs: set[str] = set()
        log_errors: list[str] = []
        failures: list[str] = []
        group_touched: set[str] = set()
        for result in summary.results:
            item = item_by_id.get(result.item_id)
            if item is None:
                continue
            group_id = str(item.extra.get("group_id") or "")
            if result.status == "success":
                iso_dir = Path(item.target).parent
                iso_dirs.add(str(iso_dir))
                ok = append_isolation_log(
                    iso_dir, Path(item.target).name, item.source,
                    str(item.extra.get("keep_path") or ""))
                if not ok:
                    log_errors.append(f"{Path(item.target).name}：隔离日志写入失败")
                moved_ids.append(str(item.extra.get("unit").id))
                group_touched.add(group_id)
            elif result.status in ("failed", "partial"):
                failures.append(f"{item.label}：{result.message or '移动未完成'}")

        if moved_ids:
            self.drop_units(moved_ids)
        # 已完全消解的组：清掉残余成员身上的重复标记（组已从列表移除）
        surviving = {g.id for g in self.state.duplicate_groups}
        for unit in self.state.units:
            if unit.duplicate_group and unit.duplicate_group not in surviving:
                unit.duplicate_group = ""
                unit.dup_role = ""
                unit.dup_note = ""
        self.save_state()
        return {
            "moved": len(moved_ids),
            "failures": failures,
            "cancelled": summary.counts.get("cancelled", 0),
            "isolation_dirs": sorted(iso_dirs),
            "log_errors": log_errors,
            "summary": summary,
            "remaining_groups": len(self.state.duplicate_groups),
        }

    # ------------------------------------------------------------------
    # 外置歌词：预览、关联、取消关联
    # ------------------------------------------------------------------
    @staticmethod
    def lyrics_of(unit: Unit) -> list[dict]:
        return [a for a in unit.attachments if a.get("role") == "lyrics"]

    def pending_lyrics(self) -> list[Unit]:
        """待关联歌词（多候选 / 多语言 / 依据不足），需用户手动选择。"""
        return [u for u in self.state.loose if u.status == UnitStatus.NEEDS_LINK.value]

    def lyrics_stats(self) -> dict:
        linked = 0
        for unit in self.state.units:
            linked += len(self.lyrics_of(unit))
        pending = self.pending_lyrics()
        in_albums = 0
        for unit in self.state.units:
            if unit.is_album:
                in_albums += len(self.lyrics_of(unit))
        return {"linked": linked, "pending": len(pending), "in_albums": in_albums}

    def lyrics_preview(self, path: str, encoding: str | None = None) -> dict:
        """只读预览：返回解码文本、可用编码、结构与判定依据。"""
        info = load_lyrics(path, encoding, preview_lines=80)
        return {
            "ok": info.ok,
            "path": info.path,
            "text": info.preview if info.ok else "",
            "encoding": info.encoding,
            "encodings": available_encodings(path),
            "has_timestamps": info.has_lrc_timestamps,
            "structure": describe_structure(info),
            "metadata": dict(info.metadata),
            "evidence": list(info.analysis.get("evidence", [])),
            "line_count": int(info.analysis.get("line_count", 0) or 0),
            "error": info.error,
        }

    def lyrics_candidates(self, path: str) -> list[dict]:
        """给手动关联界面：该歌词文件在现有单元中的候选曲目（按匹配分排序）。"""
        candidates: list[TrackCandidate] = []
        for unit in self.state.units:
            if unit.is_track:
                candidates.append(TrackCandidate(
                    key=unit.id, name=unit.name, title=unit.title, artist=unit.artist,
                    album=unit.album, track=unit.track, disc=unit.disc, path=unit.path))
            elif unit.is_album:
                for track in unit.virtual_tracks:
                    number = track.get("number")
                    candidates.append(TrackCandidate(
                        key=f"{unit.id}:cue:{track.get('cue', '')}:{number}",
                        name=f"{int(number):02d}" if number else "",
                        title=str(track.get("title") or ""),
                        artist=str(track.get("performer") or unit.album_artist or ""),
                        album=unit.album, track=int(number) if number else None,
                        path=unit.boundary_path or unit.path, is_virtual=True))
        info = load_lyrics(path)
        if not info.ok:
            return []
        matches = rank_candidates(info, candidates)
        return find_pending_partners(matches, limit=8)

    def resolve_lyrics_target(self, key: str) -> tuple[Unit | None, str]:
        """把候选 key 解析成可关联的单元。

        实体曲目 → 该曲目；CUE 虚拟曲目（albumid:cue:...）→ 所属整轨专辑，
        只记录建议，不拆分音频。
        """
        unit = self.state.unit_by_id(key)
        if unit is not None:
            return unit, (unit.title or unit.name)
        marker = ":cue:"
        if marker in key:
            album_id, cue_part = key.split(marker, 1)
            album = self.state.unit_by_id(album_id)
            if album is not None:
                return album, f"CUE 曲目 {cue_part}"
        return None, ""

    def link_lyrics(self, unit: Unit, path: str, selected: bool = True) -> dict:
        """把歌词关联到指定歌曲/专辑（手动，仅记录关系，不改动歌词内容）。"""
        info = load_lyrics(path)
        att = info.to_attachment(
            confidence="manual", note="手动关联", evidence=["由你手动指定关联曲目"],
            score=0.0, link_status="manual", selected=selected,
            owner_hint=unit.title or unit.name)
        key = os.path.normcase(os.path.normpath(path))
        unit.attachments = [a for a in unit.attachments
                            if os.path.normcase(os.path.normpath(a.get("path", ""))) != key]
        unit.attachments.append(att)
        self.state.loose = [u for u in self.state.loose
                            if os.path.normcase(os.path.normpath(u.path)) != key]
        if unit.status_enum.is_pending and unit.kind != "attachment":
            self._refresh_status(unit)
        self.save_state()
        return att

    def unlink_lyrics(self, unit: Unit, path: str) -> None:
        key = os.path.normcase(os.path.normpath(path))
        unit.attachments = [a for a in unit.attachments
                            if os.path.normcase(os.path.normpath(a.get("path", ""))) != key]
        self.save_state()

    # ------------------------------------------------------------------
    # 计划与执行
    # ------------------------------------------------------------------
    def build_plan(self) -> Plan:
        options = PlanOptions(
            target_root=self.settings.target_dir,
            mode=self.settings.mode,
            template=self.settings.template,
            digits=self.settings.track_digits,
            verify_hash=self.settings.verify_hash,
        )
        options.artist_resolver = self.resolve_artist_dir
        builder = PlanBuilder(options)
        plan = builder.build(self.state.units,
                             source_roots=[s.path for s in self.state.sources])
        self._sync_plan_findings(plan)
        self.last_plan = plan
        return plan

    def _sync_plan_findings(self, plan: Plan) -> None:
        """把计划检查新发现的异常（不可写阻止/跳过原因）回写到单元消息。

        分组与整理中心随后即可看到同一份结论。旧的检查结论先清除，
        只保留最新一次。
        """
        findings: dict[str, list[str]] = {}
        for item in plan.items:
            if item.unit_id and item.status in ("conflict", "blocked", "skipped") \
                    and item.messages:
                findings.setdefault(item.unit_id, []).append(
                    "计划检查：" + "；".join(item.messages[:2]))
        for unit in self.state.units:
            if any(m.startswith("计划检查：") for m in unit.messages):
                unit.messages = [m for m in unit.messages if not m.startswith("计划检查：")]
        for unit_id, msgs in findings.items():
            unit = self.state.unit_by_id(unit_id)
            if unit is not None:
                unit.messages.extend(msgs[:3])

    def execute_plan(self, plan: Plan, progress=None, cancel=None) -> RunSummary:
        executor = Executor(self.paths.history_dir, verify_hash=self.settings.verify_hash)
        summary = executor.execute(plan, progress=progress, cancel=cancel)
        self.last_run = summary
        self._mark_done(plan, summary)
        self.save_state()
        return summary

    def _mark_done(self, plan: Plan, summary: RunSummary) -> None:
        done_ids = {r.item_id for r in summary.results if r.status in ("success", "partial")}
        for item in plan.items:
            if item.id not in done_ids:
                continue
            unit = self.state.unit_by_id(item.unit_id)
            if unit is None:
                continue
            if item.op in ("album_move", "track_move", "duplicate_move", "archive_archive"):
                unit.status = UnitStatus.DONE.value
                unit.checked = False
            unit.last_target = item.target

    def history_runs(self) -> list[RunRecord]:
        return self.history.list_runs()

    def build_undo(self, record: RunRecord) -> UndoPlan:
        return self.undo_service.build(record)

    def apply_undo(self, plan: UndoPlan, progress=None, cancel=None) -> RunRecord:
        record = self.undo_service.apply(plan, progress=progress, cancel=cancel)
        self.save_state()
        return record

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        units = self.state.units
        checked = [u for u in units if u.checked]
        pending = [u for u in units if u.status_enum.is_pending
                   and u.kind != UnitKind.ATTACHMENT.value]
        lyrics = self.lyrics_stats()
        return {
            "total": len(units),
            "checked": len(checked),
            "pending": len(pending),
            "unlinked": len(self.state.loose),
            "duplicates": len(self.state.duplicate_groups),
            "duplicates_checked": bool(self.state.duplicates_checked_at),
            "archives": len([u for u in units if u.is_archive]),
            "groups": self.group_summary(),
            "lyrics_linked": lyrics["linked"],
            "lyrics_pending": lyrics["pending"],
            "lyrics_in_albums": lyrics["in_albums"],
        }
