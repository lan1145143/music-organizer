"""整理计划：从勾选项生成可校验、可预览、可执行的计划。

计划独立于界面，可单独生成与测试；预览与实际执行使用同一份计划对象。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..domain import naming
from ..domain.models import (OpKind, PlanItem, PlanStatus, Unit, UnitKind, UnitStatus)
from ..scan.audio import AUDIO_EXTS as AUDIO_SUFFIXES


@dataclass
class PlanOptions:
    target_root: str = ""
    mode: str = "move"          # move | copy
    template: str = "A"
    digits: int = 2
    verify_hash: bool = True
    duplicate_dir_name: str = "重复文件"
    archive_dir_name: str = "原始压缩包"
    # 解析整理艺术家 → (真实目标目录名, 是否复用已有目录)；由服务层注入。
    # 精确匹配不受候选展示数量限制；未注入时按清洗规则生成目录名。
    artist_resolver: Callable[[str], tuple[str, bool]] | None = None

    @property
    def action(self) -> str:
        return "复制" if self.mode == "copy" else "移动"


@dataclass
class Plan:
    items: list[PlanItem] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    options: PlanOptions = field(default_factory=PlanOptions)
    total_bytes: int = 0

    # ------------------------------------------------------------------
    @property
    def ready_items(self) -> list[PlanItem]:
        return [i for i in self.items if i.selected and i.status == PlanStatus.READY.value]

    def counts(self) -> dict[str, int]:
        data = {"ready": 0, "conflict": 0, "blocked": 0, "skipped": 0, "selected": 0}
        for item in self.items:
            data[item.status] = data.get(item.status, 0) + 1
            if item.selected:
                data["selected"] += 1
        return data

    def bytes_for(self, item: PlanItem) -> int:
        try:
            src = Path(item.source)
            if src.is_file():
                return src.stat().st_size
            total = 0
            for dirpath, _dn, filenames in naming.walk_pruned(src):
                for name in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, name))
                    except OSError:
                        pass
            return total
        except OSError:
            return 0

    def recalculate_conflicts(self) -> None:
        """按勾选状态重算计划内目标（全自动消解：不拦截，唯一目标键顺序自动改名）。

        逐项取消后保持一致性：目标键冲突的项自动追加 (2)、(3)…，
        绝不覆盖任何已有文件。
        """
        seen: dict[str, str] = {}
        for item in self.items:
            if not item.selected:
                item.status = PlanStatus.SKIPPED.value
                continue
            if item.status in (PlanStatus.SKIPPED.value, PlanStatus.BLOCKED.value):
                continue  # 不执行的项目不占用目标键
            target = Path(item.target)
            key = naming.normalize_key(item.target)
            if key in seen:
                alt = naming.unique_name(target, set(seen.keys()))
                item.target = str(alt)
                item.add_message(f"与另一项计划目标相同，自动改名为 {Path(item.target).name}"
                                 "（不覆盖）")
            seen[naming.normalize_key(item.target)] = item.id


class PlanBuilder:
    def __init__(self, options: PlanOptions) -> None:
        self.options = options
        self.messages: list[str] = []
        self.excluded: list[dict] = []

    # ------------------------------------------------------------------
    def build(self, units: list[Unit], log: dict[str, str] | None = None,
              source_roots: list[str] | None = None) -> Plan:
        plan = Plan(options=self.options)
        log = log or {}
        target_root = Path(self.options.target_root) if self.options.target_root else None
        if target_root is None or not str(target_root).strip():
            plan.messages.append("尚未选择目标目录")
            return plan
        try:
            target_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            plan.messages.append(f"目标目录不可用：{exc.strerror or exc}")
            return plan
        if not os.access(target_root, os.W_OK):
            plan.messages.append(f"目标目录不可写：{target_root}")

        # 目标不得设在任何来源目录内部（避免递归扫描/自我复制）
        roots = [str(r) for r in (source_roots or [])]
        if not roots:
            for unit in units:
                source_dir = unit.boundary_path or (os.path.dirname(unit.path)
                                                    if unit.is_track else unit.path)
                if source_dir and source_dir not in roots:
                    roots.append(source_dir)
        for root in roots:
            if root and _inside_tree(target_root, root):
                plan.messages.append(
                    f"目标目录位于来源内部，禁止执行（会造成自我复制）：{target_root}")
                return plan

        albums = [u for u in units if u.is_album and u.status != "missing"]
        tracks = [u for u in units if u.is_track and u.status != "missing"]

        items: list[PlanItem] = []

        # 1) 整夹专辑（含连带附属资料）
        for album in albums:
            if not album.checked:
                continue
            item = self._album_item(album, target_root)
            if item is not None:
                items.append(item)

        # 2) 散曲（父专辑整夹时标为随夹处理）
        for track in tracks:
            parent_album = self._parent_album(track, albums)
            if parent_album is not None and parent_album.checked and parent_album.mode_hint == "album":
                track.status = UnitStatus.CHILD.value
                track.add_message("随父文件夹整体处理，不单独执行")
                continue
            if not track.checked:
                continue
            item = self._track_item(track, target_root)
            if item is not None:
                items.append(item)
            # CUE 引用修正（若 CUE 随本次移动且引用路径改变）。散曲本次不执行
            # （失联跳过/被阻止）时一并跳过：改写会把引用指向曲目未到达的目标
            if item is not None and item.status == PlanStatus.READY.value:
                items.extend(self._cue_fix_items(track, target_root))

        # 2.5) 整夹已改为按散曲整理（mode_hint="tracks" 且未勾选整夹）时，
        # 其专辑级附件（封面/扫图/日志/未归属歌词）只随专辑目录搬运一次：
        # 挂到该专辑第一个可执行散曲项上，不复制给每一首。
        for album in albums:
            if album.checked or album.mode_hint != "tracks":
                continue
            atts = [a for a in album.attachments if a.get("selected")]
            if not atts:
                continue
            boundary = album.boundary_path or album.path
            first = next((it for it in items
                          if it.op == OpKind.TRACK_MOVE.value and it.selected
                          and it.status == PlanStatus.READY.value
                          and _inside_boundary(it.source, boundary)), None)
            if first is None:
                continue
            existing = {os.path.normcase(a.get("path", ""))
                        for a in (first.extra.get("attachments") or [])}
            added = [a for a in atts
                     if os.path.normcase(a.get("path", "")) not in existing]
            if not added:
                continue
            first.extra["attachments"] = (first.extra.get("attachments") or []) + added
            names = "、".join(Path(a["path"]).name for a in added[:6])
            more = f" 等 {len(added)} 项" if len(added) > 6 else ""
            first.details.append(
                f"专辑级附件 {len(added)} 项随专辑目录整理：{names}{more}")

        # 3) 重复项处理（全部按普通目标流转，不要求确认保留项）
        for unit in units:
            if unit.dup_role != "duplicate" or not unit.checked:
                continue
            item = self._duplicate_item(unit, target_root)
            if item is not None:
                items.append(item)

        # 4) 压缩包：原包自动归档——同目录音频有唯一归属目录时随音频走，
        #    否则归入“原始压缩包/”（全自动兜底）
        # 已被整夹连带打包的文件不重复生成归档项（避免双重搬运）
        bundled_paths: set[str] = set()
        for done_item in items:
            for companion in (done_item.extra.get("companions") or []):
                key = os.path.normcase(os.path.normpath(companion))
                bundled_paths.add(key)
                bundled_paths.add(key + os.sep)
        track_album_dirs: dict[str, set[str]] = {}
        for done_item in items:
            if done_item.op != OpKind.TRACK_MOVE.value or not done_item.selected:
                continue
            src_dir = os.path.normcase(os.path.dirname(done_item.source))
            album_dir = os.path.normcase(str(Path(done_item.target).parent))
            track_album_dirs.setdefault(src_dir, set()).add(album_dir)
        for archive in units:
            if not archive.is_archive or not archive.checked:
                continue
            arch_key = os.path.normcase(os.path.normpath(archive.path))
            if any(arch_key == p or arch_key.startswith(p) for p in bundled_paths):
                continue
            item = self._archive_item(archive, target_root, track_album_dirs)
            if item is not None:
                items.append(item)

        # 5) 未勾选/跳过项记录（用于预览中说明本次排除的内容）
        # 5.1 待关联/未勾选的歌词：明确列出，保证不会被顺带移动
        for unit in units:
            if unit.kind == UnitKind.ATTACHMENT.value:
                if unit.status == UnitStatus.NEEDS_LINK.value:
                    self.excluded.append({
                        "unit_id": unit.id, "name": unit.name, "status": "歌词待关联",
                        "reason": "；".join(unit.messages) or "待你手动选择对应歌曲",
                    })
                continue
            if unit.is_track:
                unselected = [a for a in unit.attachments
                              if a.get("role") == "lyrics" and not a.get("selected")]
                if unselected:
                    self.excluded.append({
                        "unit_id": unit.id, "name": unit.name, "status": "歌词未勾选",
                        "reason": "、".join(Path(a["path"]).name for a in unselected[:4])
                                  + "：未勾选，保留在来源目录",
                    })
            elif unit.is_album and unit.mode_hint == "tracks":
                # 整夹按散曲整理时：未勾选的专辑级歌词同样不会顺带移动
                unselected = [a for a in unit.attachments
                              if a.get("role") == "lyrics" and not a.get("selected")]
                if unselected:
                    self.excluded.append({
                        "unit_id": unit.id, "name": unit.name, "status": "歌词未勾选",
                        "reason": "、".join(Path(a["path"]).name for a in unselected[:4])
                                  + "：未勾选，保留在来源目录",
                    })
        for unit in units:
            if unit.kind == UnitKind.ATTACHMENT.value:
                continue
            if unit.status_enum.is_pending and unit.checked:
                self.excluded.append({
                    "unit_id": unit.id, "name": unit.name, "status": "来源失联",
                    "reason": "；".join(unit.messages) or "来源失联，本次跳过",
                })
            elif not unit.checked and unit.status != UnitStatus.CHILD.value:
                self.excluded.append({
                    "unit_id": unit.id, "name": unit.name, "status": "未勾选",
                    "reason": "未勾选，不参与本次整理",
                })

        for index, item in enumerate(items):
            item.order = index
        self._post_validate(items)
        # 6) 被跳过的勾选项（失联/目标同源等）：明确写入排除，不静默消失
        skipped_ids = {e["unit_id"] for e in self.excluded}
        for item in items:
            if item.selected and item.status == PlanStatus.SKIPPED.value                     and item.unit_id not in skipped_ids:
                self.excluded.append({
                    "unit_id": item.unit_id, "name": item.label,
                    "status": "已跳过",
                    "reason": "；".join(item.messages) or "该项本次不执行",
                })
                skipped_ids.add(item.unit_id)
        plan.items = items
        plan.excluded = self.excluded
        plan.messages.extend(self.messages)
        plan.total_bytes = sum(plan.bytes_for(i) for i in items if i.selected)
        plan.recalculate_conflicts()
        plan.messages.extend(self._space_messages(plan, target_root))
        return plan

    # ------------------------------------------------------------------
    def _album_item(self, album: Unit, target_root: Path) -> PlanItem | None:
        source = Path(album.boundary_path or album.path)
        item = PlanItem(op=OpKind.ALBUM_MOVE.value, unit_id=album.id,
                        unit_kind=UnitKind.ALBUM.value, label=f"整夹 · {album.name}",
                        source=str(source), action=self.options.mode)
        item.extra = {"unit": album}
        # 全自动：边界一律视为已采用；失联 → 跳过并报告（不阻断整体）
        if not source.is_dir():
            item.status = PlanStatus.SKIPPED.value
            item.add_message("来源目录不存在或已变化，本次跳过")
            return item

        target = self._album_target(album, target_root)
        item.target = str(target)
        item.details = self._album_details(source)
        artist_dir_name, artist_reused = self._artist_dir(album)
        item.extra["artist_dir"] = {"name": artist_dir_name, "reused": artist_reused}
        item.details.insert(0, "艺术家目录：" + (
            f"使用已有目录“{artist_dir_name}”" if artist_reused
            else f"将新建“{artist_dir_name}”（确认执行时创建）"))
        # 专辑目录动态重命名：目标专辑层强制取自元数据专辑名（方案 §1）；
        # 与来源目录名不同时在预览明示，内部内容仍原样搬运
        source_folder = source.name
        target_folder = Path(item.target).name
        if target_folder != source_folder:
            item.details.insert(
                1, f"专辑目录重命名：{source_folder} → {target_folder}（元数据专辑名）")
        if not (album.album or "").strip():
            item.add_message("未识别到专辑名，目标目录将使用“未分类专辑”")
        # 连带打包：整夹同级/上级的分散附属资料随整夹一起移入目标目录
        # （原 manual_note 拦截项 → 全自动归并；歧义资料不连带，保留在来源）
        companions, companion_notes = self._companion_materials(album)
        if companions:
            item.extra["companions"] = companions
            names = "、".join(Path(p).name for p in companions[:6])
            more = f" 等 {len(companions)} 项" if len(companions) > 6 else ""
            item.details.append(f"连带附属资料 {len(companions)} 项：{names}{more}（原样随整夹移动）")
            item.messages.extend(companion_notes)
        if not (album.album_artist or "").strip():
            item.add_message("整理艺术家未指定，将放入“未知艺术家”")
        album_lyrics = [a for a in album.attachments if a.get("role") == "lyrics"]
        if album_lyrics:
            item.details.append(
                f"外置歌词 {len(album_lyrics)} 个：随整夹原样保留（不改写、不转码、不拆分）")
        item.extra.update({
            "boundary": str(source),
            "album_artist": album.album_artist,
            "file_count": album.file_count,
            "content_note": "内部名称与层级保持原样",
        })
        self._check_target(item, source, album.boundary_path or album.path)
        return item

    def _companion_materials(self, album: Unit) -> tuple[list[str], list[str]]:
        """整夹同级/上级的分散附属资料（原“资料分散”拦截项 → 连带打包）。

        只连带**无歧义**的附件类文件（图片/日志/文本/校验文件等），
        不连带目录、不连带音频（音频已由扫描归入各自单元）、
        不连带其它专辑的资料（同级存在其它音频目录时整个上级跳过）。
        返回 (文件路径列表, 提示列表)。
        """
        boundary = Path(album.boundary_path or album.path)
        parent = boundary.parent
        if not parent.is_dir():
            return [], []
        # 上级还包含其它"带音频的目录"时不自动扩大：歧义，保留原位
        try:
            sibling_audio_dirs = [e for e in parent.iterdir()
                                  if e.is_dir() and e != boundary and any(
                                      f.suffix.lower() in AUDIO_SUFFIXES
                                      for f in e.iterdir() if f.is_file())]
        except OSError:
            return [], []
        if sibling_audio_dirs:
            return [], ["上级存在其它音频目录，分散资料不自动连带（保留原位）"]
        notes: list[str] = []
        companions: list[str] = []
        try:
            for entry in sorted(parent.iterdir()):
                if entry == boundary:
                    continue
                if entry.is_file():
                    if entry.suffix.lower() in AUDIO_SUFFIXES or entry.suffix.lower() == ".cue":
                        continue  # 音频/CUE 由散曲单元单独处理
                    companions.append(str(entry))
        except OSError:
            return [], notes
        if companions:
            notes.append(f"连带附属资料 {len(companions)} 项随整夹移动")
        return companions, notes

    def _album_target(self, album: Unit, target_root: Path) -> Path:
        if album.target_override:
            return Path(album.target_override)
        artist_dir, _reused = self._artist_dir(album)
        return naming.album_target_dir(target_root, album, self.options.template,
                                       artist_dir)

    def _artist_dir(self, unit: Unit) -> tuple[str, bool]:
        """整理艺术家对应的真实目标目录名（已有目录复用其原名）。"""
        artist = naming.display_artist(unit)
        if self.options.artist_resolver is not None and artist.strip():
            try:
                name, reused = self.options.artist_resolver(artist)
                if name:
                    return name, bool(reused)
            except Exception:  # noqa: BLE001 - 解析失败退回清洗名
                pass
        return naming.sanitize_artist(artist), False

    def _track_item(self, track: Unit, target_root: Path) -> PlanItem | None:
        item = PlanItem(op=OpKind.TRACK_MOVE.value, unit_id=track.id,
                        unit_kind=UnitKind.TRACK.value,
                        label=f"{track.artist or '未知'} - {track.title or track.name}",
                        source=track.path, action=self.options.mode)
        item.extra = {"unit": track}
        if track.dup_role == "duplicate":
            return self._duplicate_item(track, target_root)
        try:
            target = (Path(track.target_override) if track.target_override
                      else naming.track_target_path(
                          target_root, track, self.options.template,
                          self.options.digits,
                          artist_dir=self._artist_dir(track)[0]))
            item.target = str(target)
        except naming.NamingError as exc:
            item.status = PlanStatus.BLOCKED.value
            item.add_message(str(exc))
            return item
        artist_dir_name, artist_reused = self._artist_dir(track)
        item.extra["artist_dir"] = {"name": artist_dir_name, "reused": artist_reused}
        item.details.append("艺术家目录：" + (
            f"使用已有目录“{artist_dir_name}”" if artist_reused
            else f"将新建“{artist_dir_name}”（确认执行时创建）"))
        # 专辑来源说明（元数据直读）：进预览详情，用户可直接改字段
        album_name = (track.album or "").strip() or naming.UNKNOWN_ALBUM
        if (track.album or "").strip() and album_name != naming.UNKNOWN_ALBUM:
            origin = (track.origins or {}).get("album", "")
            label = {"tag": "标签", "filename": "目录名"}.get(origin, "自动识别")
            evidence = [e for e in (track.evidence or [])
                        if e.startswith(("专辑", "曲目", "目录", "整理艺术家", "年份"))]
            line = f"专辑识别：{album_name}（{label}）"
            if evidence:
                line += "｜依据：" + "；".join(evidence[:2])
            item.details.append(line)
        else:
            item.details.append("专辑识别：未能识别专辑，将放入“未分类专辑”目录")
        if not Path(track.path).is_file():
            item.status = PlanStatus.SKIPPED.value
            item.add_message("来源文件不存在或已变化，本次跳过")
        attachments = [a for a in track.attachments if a.get("selected")]
        lyrics = [a for a in attachments if a.get("role") == "lyrics"]
        others = [a for a in attachments if a.get("role") != "lyrics"]
        if others:
            item.details.append(f"关联附件 {len(others)} 个：" + "、".join(
                Path(a["path"]).name for a in others[:6]))
        item.extra["attachments"] = others + lyrics
        swatch = [a for a in track.attachments
                  if a.get("role") == "lyrics" and not a.get("selected")]
        if swatch:
            item.add_message(f"有 {len(swatch)} 个歌词未勾选，本次不会随歌曲移动，"
                             "将保留在来源目录")
        planned_lyrics: list[dict] = []
        if lyrics:
            album_dir = Path(item.target).parent
            planned = planned_lyrics
            taken: set[str] = set()
            for att in lyrics:
                src = Path(att.get("path", ""))
                dst = album_dir / src.name
                renamed = False
                if dst.exists() or naming.normalize_key(str(dst)) in taken:
                    alt = naming.unique_name(dst, taken)
                    renamed = alt != dst
                    dst = alt
                taken.add(naming.normalize_key(str(dst)))
                enc = att.get("lyrics_encoding", "") or "未知编码"
                planned.append({
                    "source": str(src), "target": str(dst),
                    "encoding": enc,
                    "has_timestamps": bool(att.get("has_lrc_timestamps")),
                    "renamed": renamed,
                    "link_status": att.get("link_status", ""),
                })
                item.details.append(f"歌词来源：{src}")
                item.details.append(f"歌词目标：{dst}（{enc}{'，LRC 时间轴' if att.get('has_lrc_timestamps') else ''}）")
                if renamed:
                    item.add_message(
                        f"歌词目标已存在同名文件：{src.name}，将改名为 {dst.name}，不覆盖")
            item.extra["lyrics"] = planned
        item.extra.setdefault("lyrics", planned_lyrics)
        if item.status != PlanStatus.BLOCKED.value:
            self._check_target(item, Path(track.path), None)
        return item

    def _cue_fix_items(self, track: Unit, target_root: Path) -> list[PlanItem]:
        """散曲移动后如需改变 CUE 引用的相对路径，生成关联修正计划。"""
        items: list[PlanItem] = []
        cues = [a for a in track.attachments if a.get("selected") and a.get("role") == "cue"]
        for att in cues:
            cue_path = att.get("path", "")
            if not cue_path or not Path(cue_path).is_file():
                continue
            try:
                cue_target = (target_root.joinpath(
                    *naming.album_dir_parts(track, self.options.template,
                                            artist_dir=self._artist_dir(track)[0]))
                              / Path(cue_path).name)
                track_target = naming.track_target_path(
                    target_root, track, self.options.template, self.options.digits,
                    artist_dir=self._artist_dir(track)[0])
            except naming.NamingError:
                continue
            old_rel = os.path.relpath(track.path, Path(cue_path).parent)
            new_rel = os.path.relpath(track_target, cue_target.parent)
            if old_rel.replace("\\", "/") == new_rel.replace("\\", "/"):
                continue
            item = PlanItem(op=OpKind.CUE_FIX.value, unit_id=track.id,
                            unit_kind=UnitKind.TRACK.value,
                            label=f"CUE 引用修正 · {Path(cue_path).name}",
                            source=cue_path, target=str(cue_target), action=self.options.mode)
            item.add_message(f"FILE 引用改为：{new_rel}")
            item.extra = {"cue": cue_path, "mapping": {old_rel: new_rel}}
            item.details.append(f"原引用：{old_rel}")
            item.details.append(f"新引用：{new_rel}")
            items.append(item)
        return items

    def _duplicate_item(self, unit: Unit, target_root: Path) -> PlanItem | None:
        target_dir = target_root / self.options.duplicate_dir_name
        if unit.is_album:
            target = target_dir / Path(unit.boundary_path or unit.path).name
        else:
            target = target_dir / Path(unit.path).name
        item = PlanItem(op=OpKind.DUPLICATE_MOVE.value, unit_id=unit.id, unit_kind=unit.kind,
                        label=f"重复项 · {unit.name}", source=unit.path,
                        target=str(target), action=self.options.mode)
        item.extra = {"unit": unit, "kind": unit.duplicate_group}
        if unit.dup_role != "duplicate":
            item.selected = False
            item.status = PlanStatus.SKIPPED.value
            item.add_message("未选择为重复项")
            return item
        if not (Path(unit.path).exists() or Path(unit.boundary_path or unit.path).exists()):
            item.status = PlanStatus.SKIPPED.value
            item.add_message("来源不存在，本次跳过")
            return item
        item.add_message("按用户选择移入目标“重复文件”目录，执行后提醒")
        self._check_target(item, Path(unit.boundary_path or unit.path), None)
        if item.status != PlanStatus.BLOCKED.value:
            item.target = str(naming.unique_name(Path(item.target), self._plan_targets()))
        return item

    def _plan_targets(self) -> set[str]:
        return set()

    def _archive_item(self, archive: Unit, target_root: Path,
                      track_album_dirs: dict[str, set[str]] | None = None) -> PlanItem | None:
        """压缩包全自动：原包作为附属文件直接移动/复制，不拆解内容。

        归属规则：与同目录音频的唯一目标专辑目录一致 → 随音频放入该目录；
        否则归入“原始压缩包/”兜底目录。分卷整组随所在目录音频/兜底走。
        """
        target_dir = target_root / self.options.archive_dir_name
        note = "原包自动归档到“原始压缩包”（不拆解内容，原样保留）"
        if track_album_dirs:
            src_dir = os.path.normcase(os.path.dirname(archive.path))
            dirs = track_album_dirs.get(src_dir) or set()
            if len(dirs) == 1:
                target_dir = Path(next(iter(dirs)))
                note = "原包随同目录音频整理（放入其专辑目录，原样保留）"
        item = PlanItem(op=OpKind.ARCHIVE_ARCHIVE.value, unit_id=archive.id,
                        unit_kind=UnitKind.ARCHIVE.value, label=f"原包 · {archive.name}",
                        source=archive.path,
                        target=str(target_dir / Path(archive.path).name),
                        action=self.options.mode)
        item.extra = {"unit": archive, "note": note}
        item.add_message(note)
        if not Path(archive.path).is_file():
            item.status = PlanStatus.SKIPPED.value
            item.add_message("压缩包不存在或已变化，本次跳过")
            return item
        self._check_target(item, Path(archive.path), None)
        return item

    # ------------------------------------------------------------------
    def _parent_album(self, track: Unit, albums: list[Unit]) -> Unit | None:
        key = os.path.normcase(os.path.dirname(track.path))
        for album in albums:
            boundary = os.path.normcase(os.path.normpath(album.boundary_path or album.path))
            if key == boundary or key.startswith(boundary + os.sep):
                return album
        return None

    def _album_details(self, source: Path) -> list[str]:
        details: list[str] = []
        try:
            entries = sorted(source.iterdir(), key=lambda p: (p.is_file(), p.name.casefold()))
        except OSError:
            return details
        for entry in entries[:40]:
            suffix = "/" if entry.is_dir() else ""
            details.append(f"{entry.name}{suffix}")
        if len(entries) > 40:
            details.append(f"…共 {len(entries)} 项")
        return details

    def _check_target(self, item: PlanItem, source: Path, boundary: str | None) -> None:
        """目标校验全自动消解：唯一拒绝的是"目标父级不可写"。

        - 目标已存在同名项 → 自动改名 (2)、(3)…（绝不覆盖）；
        - 路径超长 → 自动截断文件名主干（保留扩展名与防重名后缀）；
        - 目标与来源相同 → 跳过该项（无需整理）。
        """
        target = Path(item.target)
        boundary_path = os.path.normcase(os.path.normpath(boundary)) if boundary else ""
        target_norm = os.path.normcase(os.path.normpath(str(target)))
        src_norm = os.path.normcase(os.path.normpath(str(source)))
        if boundary_path and (target_norm == boundary_path
                              or target_norm.startswith(boundary_path + os.sep)):
            item.status = PlanStatus.BLOCKED.value
            item.add_message("目标不得设在待整理的源文件夹内")
            return
        if src_norm == target_norm:
            item.status = PlanStatus.SKIPPED.value
            item.add_message("目标与来源相同，无需整理")
            return
        # 超长路径自动截断（不再阻止）
        fitted = naming.fit_target_path(target)
        if str(fitted) != str(target):
            item.add_message(f"目标路径超长，已自动截断文件名：{fitted.name}")
            item.target = str(fitted)
            target = fitted
            target_norm = os.path.normcase(os.path.normpath(str(target)))
        # 目标已存在同名项 → 自动改名（不覆盖）
        if target.exists():
            alt = naming.unique_name(target)
            if alt != target:
                item.add_message(f"目标已存在同名项：自动改名为 {alt.name}（不覆盖）")
                item.target = str(alt)
                target = alt
        # 权限（唯一保留的硬性拒绝）
        parent = target.parent
        probe = parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not os.access(probe, os.W_OK):
            item.status = PlanStatus.BLOCKED.value
            item.add_message(f"目标位置不可写：{probe}")

    def _post_validate(self, items: list[PlanItem]) -> None:
        """计划内冲突全自动消解：

        - 父/子来源重叠（同一来源被两项覆盖）→ 完整整夹优先，自动剔除子级项；
        - 目标键冲突由 recalculate_conflicts 顺序自动改名。
        """
        # 父/子重叠：整夹（目录）优先于其内部散曲
        demoted: set[str] = set()
        for item in items:
            if not item.selected or item.op != OpKind.ALBUM_MOVE.value:
                continue
            src_key = naming.normalize_key(item.source)
            for other in items:
                if other is item or not other.selected:
                    continue
                other_src = naming.normalize_key(other.source)
                if other_src.startswith(src_key + "\\"):
                    demoted.add(other.id)
        for item in items:
            if item.id in demoted:
                item.selected = False
                item.status = PlanStatus.SKIPPED.value
                item.add_message("已被同范围的整夹计划覆盖，自动去重（整夹优先）")
                self.excluded.append({
                    "unit_id": item.unit_id, "name": item.label,
                    "status": "自动去重",
                    "reason": "与其所在整夹的移动计划重叠，按整夹优先自动剔除",
                })

    def _space_messages(self, plan: Plan, target_root: Path) -> list[str]:
        messages: list[str] = []
        need = 0
        for item in plan.ready_items:
            source = Path(item.source)
            same_volume = _same_volume(source, target_root)
            if self.options.mode == "copy" or not same_volume:
                need += plan.bytes_for(item)
        if need:
            try:
                free = shutil.disk_usage(target_root).free
            except OSError:
                return messages
            if free < need:
                messages.append(
                    f"目标磁盘空间不足：需要约 {need / 1024 ** 3:.2f} GB，可用 {free / 1024 ** 3:.2f} GB")
            elif free < need * 1.2:
                messages.append(
                    f"目标磁盘空间接近上限：需要约 {need / 1024 ** 3:.2f} GB，可用 {free / 1024 ** 3:.2f} GB")
        return messages


def _inside_boundary(source: str, boundary: str | None) -> bool:
    """source 是否位于整夹边界内（含相等），用于专辑级附件的归属判断。"""
    if not boundary:
        return False
    key = os.path.normcase(os.path.normpath(source))
    bnd = os.path.normcase(os.path.normpath(boundary))
    return key == bnd or key.startswith(bnd + os.sep)


def _same_volume(source: Path, target: Path) -> bool:
    try:
        drive_source = Path(os.path.abspath(source)).drive.casefold()
        drive_target = Path(os.path.abspath(target)).drive.casefold()
        return drive_source == drive_target
    except (OSError, ValueError):
        return False


def _inside_tree(path: Path, root: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False
