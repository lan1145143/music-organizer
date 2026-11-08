"""扫描：把一个来源目录解析成整理单元（专辑整夹 / 散曲 / 压缩包 / 未关联附件）。

识别机制重构后的极简流水线：
1. 单次 os.walk 构建内存目录树（DirNode，弱点 #9：目录只遍历一遍）；
2. 抓轨整夹识别（album_detect）；
3. 边界外音频逐首独立直读元数据定稿（_track_unit，不做任何跨曲目聚合）；
4. 同目录附件与 .lrc 歌词确定性绑定；
5. 边界外压缩包 → archive 单元；未认领附件 → 未关联附件单元。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from ..domain.models import Attachment, SourceRoot, Unit, UnitKind, UnitStatus
from ..domain.naming import UNKNOWN_ALBUM, UNKNOWN_ARTIST, UNKNOWN_TITLE, prune_junctions
from .album_detect import (AlbumCandidate, detect_candidates, read_metas,
                           resolve_album_artist)
from .attachments import album_attachments, classify, is_attachment_candidate, suggest_for_tracks
from .audio import AudioMeta, extract_dir_year_and_clean_name, guess_from_filename, is_audio
from .constants import is_archive_file, is_isolation_dirname, multivolume_stem
from .lyrics_recognizer import (LyricsInfo, TrackCandidate, candidate_options, load_lyrics,
                                match_lyrics_in_dir)

ProgressFn = Callable[[str, int, int], None]


@dataclass
class ScanOutcome:
    units: list[Unit] = field(default_factory=list)
    loose_attachments: list[Unit] = field(default_factory=list)
    archives: list[Unit] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class DirNode:
    """内存目录树节点：一次 os.walk 的完整结果，供各识别阶段复用。"""

    path: str
    files: list[str] = field(default_factory=list)
    subdirs: dict[str, "DirNode"] = field(default_factory=dict)

    def iter_files(self) -> Iterator[str]:
        yield from self.files
        for sub in self.subdirs.values():
            yield from sub.iter_files()

    def has_audio(self) -> bool:
        return any(is_audio(f) for f in self.files)


def build_dir_tree(root: str | Path) -> tuple[DirNode, dict[str, DirNode]]:
    """单次 os.walk 构建目录树，返回 (根节点, normcase 路径 → 节点)。

    「a重复歌曲」隔离目录整棵剪枝：已隔离的重复文件不参与后续扫描/查重
    （任务书 §4.3，防止再次扫入算作重复项）。
    """
    root = Path(root)
    nodes: dict[str, DirNode] = {}
    root_node = DirNode(path=str(root))
    nodes[os.path.normcase(os.path.normpath(str(root)))] = root_node
    for dirpath, dirnames, filenames in os.walk(root):
        prune_junctions(dirpath, dirnames)   # 联接环会让同一文件按多层路径重复枚举
        dirnames[:] = [d for d in dirnames if not is_isolation_dirname(d)]
        dirnames.sort()
        key = os.path.normcase(os.path.normpath(dirpath))
        node = nodes.get(key)
        if node is None:  # 防御：异常文件系统下父目录未先访问
            node = DirNode(path=dirpath)
            nodes[key] = node
        node.files = [str(Path(dirpath) / name) for name in sorted(filenames)]
        for name in dirnames:
            child = DirNode(path=str(Path(dirpath) / name))
            node.subdirs[name] = child
            nodes[os.path.normcase(str(Path(dirpath) / name))] = child
    return root_node, nodes


def identity_key(unit: Unit) -> str:
    """稳定标识：不绑定表格行号，移动/重扫后仍可追踪。"""
    return f"{unit.kind}|{os.path.normcase(os.path.normpath(unit.path))}"


def file_signature(paths: list[str]) -> dict[str, list]:
    sig: dict[str, list] = {}
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        sig[os.path.normcase(path)] = [st.st_size, int(st.st_mtime)]
    return sig


def is_archive_name(name: str) -> bool:
    """压缩包（单包或分卷）判定；口径统一见 constants.is_archive_file。"""
    return is_archive_file(name)


def multivolume_paths(archive_files: list[str]) -> set[str]:
    """真分卷组的成员路径集合：同目录同主干分卷 ≥2 个才算分卷组
    （弱点 #8：单个 .part1.rar 不再被误标为分卷）。"""
    groups: dict[tuple[str, str], list[str]] = {}
    for path in archive_files:
        stem = multivolume_stem(Path(path).name)
        if stem is None:
            continue
        key = (os.path.normcase(str(Path(path).parent)), stem.casefold())
        groups.setdefault(key, []).append(path)
    out: set[str] = set()
    for members in groups.values():
        if len(members) >= 2:
            out.update(os.path.normcase(m) for m in members)
    return out


def _dir_size(paths: list[str]) -> int:
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            continue
    return total


class Scanner:
    def __init__(self, progress: ProgressFn | None = None) -> None:
        self.progress = progress or (lambda msg, cur, total: None)

    def scan_source(self, source: SourceRoot) -> ScanOutcome:
        root = Path(source.path)
        outcome = ScanOutcome()
        if not root.is_dir():
            outcome.errors.append(f"来源不存在或不是目录：{source.path}")
            return outcome
        if is_isolation_dirname(root.name):
            # 用户手动把隔离目录登记为来源：整目录跳过，绝不让已隔离文件回流
            outcome.errors.append(
                f"来源为重复隔离目录，已跳过（隔离文件不参与扫描）：{source.path}")
            return outcome

        self.progress("扫描目录…", 0, 1)
        root_node, nodes = build_dir_tree(root)
        audio_files: list[str] = []
        cue_files: list[str] = []
        archive_files: list[str] = []
        all_files: list[str] = []
        for node in nodes.values():
            for name in node.files:
                all_files.append(name)
                if is_audio(name):
                    audio_files.append(name)
                elif name.lower().endswith(".cue"):
                    cue_files.append(name)
                elif is_archive_name(name):
                    archive_files.append(name)

        if not all_files:
            outcome.errors.append(f"来源为空目录：{source.path}")
            return outcome

        self.progress(f"读取音频信息（{len(audio_files)} 个文件）…", 0, max(len(audio_files), 1))
        metas = read_metas(audio_files, progress=lambda cur, total, path: self.progress(
            f"读取标签：{Path(path).name}", cur, total))

        candidates = detect_candidates(root, audio_files, cue_files, metas)
        album_units: list[Unit] = []
        album_roots: list[str] = []
        for cand in candidates:
            unit = self._album_unit(source, cand, metas, nodes)
            album_units.append(unit)
            album_roots.append(cand.path)

        covered = set()
        for unit in album_units:
            for f in unit.audio_files:
                covered.add(os.path.normcase(f))

        # 散曲：不被任何专辑边界覆盖的音频，逐首独立直读定稿
        loose_audio = [f for f in audio_files if os.path.normcase(f) not in covered]
        loose_by_dir: dict[str, list[str]] = {}
        for f in loose_audio:
            loose_by_dir.setdefault(str(Path(f).parent), []).append(f)

        track_units: list[Unit] = []
        unassoc: list[Unit] = []
        for directory, files in sorted(loose_by_dir.items()):
            tracks = [self._track_unit(source, f, metas.get(f)) for f in sorted(files)]
            confirmed, unknown = suggest_for_tracks(files, directory)
            by_stem = {Path(a.path).stem.casefold(): a for a in confirmed
                       if a.role not in ("cover",)}
            shared_covers = [a for a in confirmed if a.role == "cover"]
            for track in tracks:
                stem = Path(track.path).stem.casefold()
                own = [a for a in confirmed if Path(a.path).stem.casefold() == stem]
                track.attachments = [a.to_dict() for a in own]
                if not own and shared_covers and len({t.album for t in tracks}) == 1:
                    track.attachments.extend(a.to_dict() for a in shared_covers)
            for att in unknown:
                unassoc.append(self._attachment_unit(source, att))
            for att in confirmed:
                # 未被任何曲目收下的确定附件，作为未关联附件保留，避免静默丢弃
                if att.role == "cover" and shared_covers:
                    continue
                if Path(att.path).stem.casefold() not in by_stem:
                    unassoc.append(self._attachment_unit(source, att))
            # 外置歌词：同目录三级确定性匹配，决定自动关联或待关联
            lyrics_links, lyrics_pending = self._link_lyrics(
                source, directory, tracks, nodes)
            for track in tracks:
                track.attachments.extend(lyrics_links.get(track.id, []))
            unassoc.extend(lyrics_pending)
            track_units.extend(tracks)

        # 压缩包：专辑边界内部的压缩包不单独处理（随整夹保留）
        archive_units: list[Unit] = []
        norm_roots = [os.path.normcase(os.path.normpath(r)) + os.sep for r in album_roots]
        multivols = multivolume_paths(archive_files)
        for path in archive_files:
            p_norm = os.path.normcase(os.path.normpath(path))
            if any(p_norm.startswith(r) for r in norm_roots):
                continue
            archive_units.append(
                self._archive_unit(source, path, multivolume=p_norm in multivols))

        # 未关联附件：专辑边界外的散落附件
        # 已被曲目收下（自动关联/手动关联）的附件不再重复进入待关联列表
        claimed = {os.path.normcase(os.path.normpath(a.get("path", "")))
                   for unit in track_units for a in unit.attachments}
        # 已进待关联列表的路径集合（normcase+normpath 统一口径）：
        # 万级文件的库里逐个 × 列表扫描是数千万次比较，集合化后 O(1)
        unassoc_keys = {os.path.normcase(os.path.normpath(u.path)) for u in unassoc}

        def add_unassoc(unit: Unit) -> None:
            unassoc.append(unit)
            unassoc_keys.add(os.path.normcase(os.path.normpath(unit.path)))

        for path in all_files:
            if not is_attachment_candidate(path):
                continue
            p_norm = os.path.normcase(os.path.normpath(path))
            if any(p_norm.startswith(r) for r in norm_roots):
                continue
            if is_audio(path):
                continue
            if p_norm in claimed:
                continue
            if p_norm in unassoc_keys:
                continue
            att = Attachment(path=path, role=classify(path), selected=False,
                             confidence="none", note="未关联，未确认前不会移动", owner_hint="")
            if att.role == "lyrics":
                att.is_lyrics = True
                info = load_lyrics(path)
                if info.ok:
                    att.lyrics_encoding = info.encoding
                    att.lyrics_preview = info.preview
                    att.has_lrc_timestamps = info.has_lrc_timestamps
                    att.lyrics_metadata = dict(info.metadata)
                att.note = "歌词待关联：同目录没有可对应的曲目，未确认前不会移动"
                unit = self._attachment_unit(source, att)
                unit.status = UnitStatus.NEEDS_LINK.value
                unit.messages = [att.note]
                add_unassoc(unit)
                continue
            add_unassoc(self._attachment_unit(source, att))

        outcome.units = album_units + track_units
        outcome.loose_attachments = unassoc
        outcome.archives = archive_units
        lyrics_linked = sum(1 for u in track_units for a in u.attachments
                            if a.get("role") == "lyrics")
        lyrics_in_albums = sum(1 for u in album_units for a in u.attachments
                               if a.get("role") == "lyrics")
        outcome.stats = {
            "audio": len(audio_files),
            "albums": len(album_units),
            "tracks": len(track_units),
            "archives": len(archive_units),
            "loose_files": len(unassoc),
            "files": len(all_files),
            "lyrics_linked": lyrics_linked,
            "lyrics_in_albums": lyrics_in_albums,
            "lyrics_pending": len([u for u in unassoc
                                   if u.status == UnitStatus.NEEDS_LINK.value]),
        }
        return outcome

    # ------------------------------------------------------------------
    def _album_unit(self, source: SourceRoot, cand: AlbumCandidate,
                    metas: dict[str, AudioMeta],
                    nodes: dict[str, DirNode] | None = None) -> Unit:
        unit = Unit(kind=UnitKind.ALBUM.value, source_id=source.id, path=cand.path,
                    name=Path(cand.path).name)
        unit.boundary_path = cand.path
        unit.album = cand.album_title
        # 归属艺术家：确定性提取，绝不产生“群星”（重构方案 §3.2）
        artist, artist_origin, artist_evidence = resolve_album_artist(
            cand.audio_files, metas, cand.album_artist)
        unit.album_artist = artist
        if artist and artist_evidence:
            unit.evidence.append(artist_evidence)
        unit.year = cand.year
        unit.genre = cand.genre
        unit.total_discs = max(cand.total_discs, 1)
        unit.disc = cand.disc
        unit.audio_files = list(cand.audio_files)
        unit.cue_files = list(cand.cue_paths)
        unit.evidence = unit.evidence + list(cand.evidence)
        unit.attachments = [a.to_dict() for a in album_attachments(cand.path)]
        # 边界内全部文件取自内存目录树（弱点 #9：不再 rglob 重复遍历）
        files: list[str] = []
        if nodes is not None:
            node = nodes.get(os.path.normcase(os.path.normpath(cand.path)))
            if node is not None:
                files = list(node.iter_files())
        if not files and Path(cand.path).is_dir():
            files = [str(p) for p in Path(cand.path).rglob("*") if p.is_file()]
        unit.file_count = len(files)
        unit.size = _dir_size(files)
        unit.sig = file_signature(files)
        # 专辑时长 = 子曲目时长之和（弱点 #5 配套：详情页展示正常）
        duration = 0.0
        for f in cand.audio_files:
            meta = metas.get(f)
            if meta is not None:
                duration += meta.duration
        unit.duration = duration
        for issue in cand.issues:
            unit.add_message(issue)
        # 全自动零干预：边界一律自动采用（原"依据充分才自动/否则需确认"的
        # 策略分支已废除）。推荐结果仍记录在 auto_boundary 供详情展示。
        recommended = (not cand.incomplete and not cand.dispersed and not cand.issues
                       and (cand.cue_paths or len(cand.audio_files) >= 3))
        unit.auto_boundary = True
        unit.boundary_confirmed = True
        unit.status = UnitStatus.READY.value
        if recommended:
            unit.add_message("已自动采用推荐整夹边界（全自动模式，可在详情调整）")
        else:
            unit.add_message("已按最小合理边界自动采用整夹范围（全自动模式）")
        if cand.dispersed:
            # 不再拦截：分散资料由计划阶段连带打包（歧义资料保留原位）
            unit.manual_note = cand.dispersed_note
            unit.add_message("上级有分散资料，将按连带规则自动处理（不拦截）")
        unit.origins = {
            "album": cand.album_origin or "",
            "album_artist": artist_origin if artist else "",
            "year": "tag" if cand.year else "filename",
        }
        if not (unit.album or "").strip():
            unit.add_message("未识别到专辑名（无 CUE TITLE 且标签缺失），"
                             "目标目录将使用“未分类专辑”（全自动模式）")
        # CUE 虚拟曲目（整轨不切歌）；detect 阶段已解析过，直接复用避免二次读盘
        for item in cand.single_image_cues:
            unit.virtual_tracks.extend({
                "cue": item["cue"], **track,
            } for track in item["tracks"])
        # 整夹内的歌词：随夹原样保留；整轨 CUE 可给出虚拟曲目关联建议（不拆分音频）
        self._annotate_album_lyrics(unit)
        if not unit.album_artist:
            unit.add_message("缺少专辑艺术家，将放入“未知艺术家”（全自动模式）")
        return unit

    def _track_unit(self, source: SourceRoot, path: str, meta: AudioMeta | None) -> Unit:
        """散曲单元：一次性直读元数据定稿（Tag → 文件名/目录名 → 兜底值），
        之后不再有任何跨曲目修改（重构方案 §2）。"""
        unit = Unit(kind=UnitKind.TRACK.value, source_id=source.id, path=path,
                    name=Path(path).stem)
        guessed = guess_from_filename(path)
        dir_year, _clean_dir_name = extract_dir_year_and_clean_name(Path(path).parent.name)
        unit.origins = {}
        if meta is not None:
            unit.duration = meta.duration
            unit.fmt = meta.fmt
            unit.bitrate = meta.bitrate
            unit.sample_rate = meta.sample_rate
            unit.bits = meta.bits
            unit.size = meta.size
            if meta.error:
                unit.add_message(meta.error)

        # 1. 艺术家：Tag(artist) → Tag(album_artist) → 文件名 → 未知艺术家
        meta_artist = (meta.artist if meta else "") or ""
        meta_album_artist = (meta.album_artist if meta else "") or ""
        fn_artist = str(guessed["artist"] or "")
        if meta_artist.strip():
            unit.artist = meta_artist.strip()
            unit.origins["artist"] = "tag"
        elif meta_album_artist.strip():
            unit.artist = meta_album_artist.strip()
            unit.origins["artist"] = "tag"
        elif fn_artist.strip():
            unit.artist = fn_artist.strip()
            unit.origins["artist"] = "filename"
        else:
            unit.artist = UNKNOWN_ARTIST

        # 2. 专辑：Tag(album) → 未分类专辑（专辑目录名强制取自元数据；
        #    不再用父目录名冒充——基于元数据动态重命名专辑目录方案 §3.3）
        meta_album = (meta.album if meta else "") or ""
        if meta_album.strip():
            unit.album = meta_album.strip()
            unit.origins["album"] = "tag"
        else:
            unit.album = UNKNOWN_ALBUM

        # 3. 歌名、曲号、年份直读（每首歌独立保留自身信息）
        meta_title = (meta.title if meta else "") or ""
        fn_title = str(guessed["title"] or "")
        if meta_title.strip():
            unit.title = meta_title.strip()
            unit.origins["title"] = "tag"
        elif fn_title.strip():
            unit.title = fn_title.strip()
            unit.origins["title"] = "filename"
        else:
            unit.title = Path(path).stem or UNKNOWN_TITLE

        meta_track = meta.track if meta else None
        fn_track = guessed.get("track")
        if meta_track is not None:
            unit.track = meta_track
            unit.origins["track"] = "tag"
        elif fn_track:
            unit.track = int(fn_track)
            unit.origins["track"] = "filename"
        if meta is not None and meta.disc is not None:
            unit.disc = meta.disc
            unit.origins["disc"] = "tag"
        meta_year = (meta.year if meta else "") or ""
        if meta_year.strip():
            unit.year = meta_year.strip()
            unit.origins["year"] = "tag"
        elif dir_year:
            unit.year = str(dir_year)
            unit.origins["year"] = "filename"
        if meta is not None and (meta.genre or "").strip():
            unit.genre = meta.genre.strip()
            unit.origins["genre"] = "tag"
        if meta is not None and meta.album_artist and meta.album_artist.strip():
            unit.album_artist = meta.album_artist.strip()
            unit.origins["album_artist"] = "tag"

        try:
            unit.size = unit.size or os.path.getsize(path)
        except OSError:
            pass
        unit.sig = file_signature([path])
        unit.file_count = 1
        # 全自动：缺艺术家不再拦截，走兜底值（未知艺术家），只提醒
        if unit.artist == UNKNOWN_ARTIST:
            unit.add_message("缺少艺术家，已用兜底值（全自动模式）")
        unit.status = UnitStatus.READY.value
        return unit

    def _attachment_unit(self, source: SourceRoot, att: Attachment) -> Unit:
        unit = Unit(kind=UnitKind.ATTACHMENT.value, source_id=source.id, path=att.path,
                    name=Path(att.path).name)
        unit.attachments = [att.to_dict()]
        unit.status = UnitStatus.MANUAL.value
        unit.add_message(att.note)
        try:
            unit.size = os.path.getsize(att.path)
        except OSError:
            pass
        return unit

    # ------------------------------------------------------------------
    # 外置歌词
    # ------------------------------------------------------------------
    def _lyrics_files_for(self, node: DirNode, loose_dirs: set[str],
                          is_root: bool = True) -> list[str]:
        """收集该散曲目录下的 .lrc（含无音频的 Lyrics/ 子目录）。

        有音频的子目录属于另一组曲目或专辑，不跨组关联（直接剪枝）。
        基于内存目录树工作，不再重复 os.walk（弱点 #9）。
        """
        out: list[str] = []
        here = os.path.normcase(os.path.normpath(node.path))
        if not is_root:
            if here in loose_dirs or node.has_audio():
                return out
        out.extend(f for f in node.files if f.lower().endswith(".lrc"))
        for sub in node.subdirs.values():
            out.extend(self._lyrics_files_for(sub, loose_dirs, is_root=False))
        return out

    @staticmethod
    def _candidates_of(tracks: list[Unit]) -> list[TrackCandidate]:
        return [TrackCandidate(key=t.id, name=t.name, title=t.title, artist=t.artist,
                               album=t.album, track=t.track, disc=t.disc, path=t.path)
                for t in tracks]

    def _link_lyrics(self, source: SourceRoot, directory: str, tracks: list[Unit],
                     nodes: dict[str, DirNode]) -> tuple[dict[str, list[dict]], list[Unit]]:
        """为散曲判定歌词归属：同目录三级确定性匹配自动关联，否则“待关联”。"""
        linked: dict[str, list[dict]] = {}
        pending: list[Unit] = []
        if not tracks:
            return linked, pending
        node = nodes.get(os.path.normcase(os.path.normpath(directory)))
        if node is None:
            return linked, pending
        loose_audio = {os.path.normcase(os.path.normpath(str(Path(t.path).parent)))
                       for t in tracks}
        lrc_paths = sorted(self._lyrics_files_for(node, loose_audio))
        if not lrc_paths:
            return linked, pending
        infos = [load_lyrics(p) for p in lrc_paths]
        candidates = self._candidates_of(tracks)
        bindings = match_lyrics_in_dir(infos, candidates)
        for info, binding in zip(infos, bindings):
            if not info.ok:
                att = info.to_attachment(confidence="pending", note=info.error,
                                         link_status="pending", selected=False)
                pending.append(self._pending_lyrics_unit(source, att, info.error, None))
                continue
            if binding.candidate is not None:
                level_label = {1: "同名", 2: "去曲号同名", 3: "唯一标题"}.get(
                    binding.level, "确定性")
                # 任务书 AC1：自动识别并关联的附件（含 .lrc）默认勾选；
                # 用户取消勾选后由计划层排除（“歌词未勾选”），绝不顺带移动。
                att = info.to_attachment(
                    confidence="high",
                    note=f"自动关联（同目录{level_label}匹配，默认随歌曲移动，"
                         "取消勾选则保留原位）：" + "；".join(binding.evidence),
                    evidence=binding.evidence, link_status="auto", selected=True,
                    owner_hint=binding.candidate.title or binding.candidate.name)
                linked.setdefault(binding.candidate.key, []).append(att)
                continue
            if binding.claimed_by:
                reason = (f"该曲目已有一份歌词（{binding.claimed_by}），"
                          "本文件可能是另一语言版本，需手动确认")
            else:
                reason = ("同目录内没有可确定对应的歌曲（同名/去曲号/唯一标题均未命中），"
                          "需手动关联")
            att = info.to_attachment(
                confidence="pending", note=reason,
                evidence=list(binding.evidence), link_status="pending", selected=False)
            att["lyrics_candidates"] = candidate_options(candidates)
            pending.append(self._pending_lyrics_unit(source, att, reason, info))
        return linked, pending

    def _pending_lyrics_unit(self, source: SourceRoot, att: dict, reason: str,
                             info: LyricsInfo | None = None) -> Unit:
        unit = Unit(kind=UnitKind.ATTACHMENT.value, source_id=source.id,
                    path=att["path"], name=Path(att["path"]).name)
        unit.attachments = [att]
        unit.status = UnitStatus.NEEDS_LINK.value
        text = f"歌词待关联：{reason}"
        if info is not None and info.ok:
            text += f"（{info.encoding}｜{info.analysis.get('line_count', 0)} 行）"
        unit.add_message(text)
        try:
            unit.size = os.path.getsize(att["path"])
        except OSError:
            pass
        return unit

    def _annotate_album_lyrics(self, unit: Unit) -> None:
        """整夹内歌词：原样保留；整轨 CUE 专辑按同目录三级匹配给出建议。"""
        lyrics_atts = [a for a in unit.attachments if a.get("role") == "lyrics"]
        if not lyrics_atts:
            return
        virtual = unit.virtual_tracks or []
        cues: list[TrackCandidate] = []
        for track in virtual:
            number = track.get("number")
            cues.append(TrackCandidate(
                key=f"cue:{track.get('cue', '')}:{number}",
                name=f"{int(number):02d}" if number else "",
                title=str(track.get("title") or ""),
                artist=str(track.get("performer") or unit.album_artist or ""),
                album=unit.album,
                track=int(number) if number else None,
                is_virtual=True))
        # 三级确定性匹配（与散曲同一套规则；候选为同目录整轨 CUE 虚拟曲目）
        infos = [load_lyrics(a["path"]) for a in lyrics_atts]
        bindings = match_lyrics_in_dir(infos, cues)
        for att, info, binding in zip(lyrics_atts, infos, bindings):
            if not info.ok:
                att["note"] = f"位于专辑目录内，随整夹原样保留；{info.error}"
                continue
            att["is_lyrics"] = True
            att["lyrics_encoding"] = info.encoding
            att["lyrics_preview"] = info.preview
            att["has_lrc_timestamps"] = info.has_lrc_timestamps
            att["lyrics_metadata"] = dict(info.metadata)
            if not cues:
                att["note"] = ("位于专辑目录内，随整夹原样保留"
                               f"（编码 {info.encoding}，不改写、不转码）")
                att["link_status"] = "album"
                continue
            if binding.candidate is not None:
                virtual_title = binding.candidate.title or binding.candidate.name
                att["owner_hint"] = f"CUE 曲目 {virtual_title}"
                att["match_evidence"] = list(binding.evidence)
                att["match_score"] = float(binding.level)
                att["match_confidence"] = "high"
                att["link_status"] = "auto"
                att["note"] = f"建议对应 CUE 曲目 {virtual_title}（随整夹原样保留，不拆分音频）"
                unit.evidence.append(
                    f"歌词 {Path(att['path']).name} → CUE 曲目 {virtual_title}"
                    f"（{'；'.join(binding.evidence[:2])}）")
            elif binding.claimed_by:
                att["match_confidence"] = "pending"
                att["link_status"] = "pending"
                att["note"] = (f"歌词与 CUE 曲目的对应待确认："
                               f"该曲目已有一份歌词（{binding.claimed_by}）")
                att["lyrics_candidates"] = candidate_options(cues)
                unit.evidence.append(
                    f"歌词 {Path(att['path']).name}：对应 CUE 曲目已有歌词，待确认")
            else:
                att["note"] = "整轨专辑内歌词，未能对应到具体虚拟曲目，随夹保留"
                att["link_status"] = "pending"
                att["lyrics_candidates"] = candidate_options(cues)
                unit.evidence.append(
                    f"歌词 {Path(att['path']).name}：未能对应到具体虚拟曲目，随夹保留")

    def _archive_unit(self, source: SourceRoot, path: str, multivolume: bool = False) -> Unit:
        unit = Unit(kind=UnitKind.ARCHIVE.value, source_id=source.id, path=path,
                    name=Path(path).name)
        try:
            unit.size = os.path.getsize(path)
        except OSError:
            pass
        unit.archive = {
            "mode": "archive_only",     # 全自动：一律归档原包（解压引擎已移除）
            "status": "pending",
            "multivolume": multivolume,
            "password": False,
            "extracted": "",
            "nested": [],
            "match": "",
            "message": "",
            "reason": "",
        }
        if multivolume:
            unit.add_message("分卷压缩包：整组自动归档（不拆解、不移动内部单个分卷）")
        else:
            unit.add_message("压缩包将自动归档到“原始压缩包”目录（不解压、不改名）")
        unit.status = UnitStatus.READY.value
        return unit


def _inside(path: str, directory: str) -> bool:
    """path 是否位于 directory 内（不含相等）。纯字符串前缀判断，
    避免逐文件 resolve()（Win 上极慢，3700 文件规模曾占 50s+）。"""
    p = os.path.normcase(os.path.normpath(path))
    d = os.path.normcase(os.path.normpath(directory))
    if p == d:
        return False
    return p.startswith(d + os.sep)


def dedupe_sources(sources: list[SourceRoot]) -> tuple[list[SourceRoot], list[str]]:
    """去掉重复与嵌套来源，避免重复任务。"""
    kept: list[SourceRoot] = []
    notes: list[str] = []
    seen: set[str] = set()
    for source in sorted(sources, key=lambda s: len(Path(s.path).parts)):
        key = os.path.normcase(os.path.normpath(source.path))
        if key in seen:
            notes.append(f"重复来源已忽略：{source.path}")
            continue
        if any(_inside(source.path, k.path) for k in kept):
            container = next(k for k in kept if _inside(source.path, k.path))
            notes.append(f"嵌套来源已忽略：{source.path}（已包含在 {container.path} 中）")
            continue
        seen.add(key)
        kept.append(source)
    return kept, notes


USER_EDITABLE = ("title", "artist", "album", "album_artist", "track", "disc", "year", "genre")


def merge_units(previous: list[Unit], fresh: list[Unit]) -> tuple[list[Unit], list[str]]:
    """重扫后合并：保留用户编辑/勾选/边界确认，来源变化时要求重新确认。

    返回 (合并后的单元列表, 提示列表)。上次扫描存在、本次消失的单元
    会作为“失联”单元保留（标记 missing），供明确报告而不是静默消失。
    """
    prev_map = {identity_key(u): u for u in previous}
    notes: list[str] = []
    merged: list[Unit] = []
    fresh_keys = {identity_key(u) for u in fresh}
    # 失联单元：上次有、这次无（文件被移动/删除/重命名）
    for key, old in prev_map.items():
        if key in fresh_keys or old.status == "missing":
            continue
        if old.kind == UnitKind.ARCHIVE.value:
            old.archive["status"] = "missing"
        old.status = "missing"
        old.checked = False
        old.messages = [m for m in old.messages if not m.startswith("来源已失联")]
        old.add_message("来源已失联（文件被移动、重命名或删除），需重新扫描定位")
        notes.append(f"{old.name}：来源已失联，请确认文件去向")
        merged.append(old)
    for unit in fresh:
        old = prev_map.get(identity_key(unit))
        if old is None:
            merged.append(unit)
            continue
        changed = bool(old.sig) and old.sig != unit.sig
        unit.id = old.id
        unit.checked = bool(old.checked)
        # 用户显式勾选/取消必须跨重扫保留（None=系统默认）：漏拷会导致
        # apply_default_selection 把用户明确取消的项自动勾回
        unit.user_checked = old.user_checked
        unit.boundary_confirmed = bool(old.boundary_confirmed) and not changed
        unit.mode_hint = old.mode_hint or unit.mode_hint
        unit.manual_note = old.manual_note
        unit.target_override = old.target_override
        unit.last_target = old.last_target
        unit.dup_role = old.dup_role
        unit.duplicate_group = old.duplicate_group
        for field_name in USER_EDITABLE:
            value = getattr(old, field_name, None)
            if old.origins.get(field_name) == "user" and value not in ("", None):
                setattr(unit, field_name, value)
                unit.origins[field_name] = "user"
        if old.archive:
            unit.archive.update({k: v for k, v in old.archive.items() if v not in (None, "", [])})
        selected = {os.path.normcase(a.get("path", "")): a.get("selected", True)
                    for a in old.attachments}
        for att in unit.attachments:
            key = os.path.normcase(att.get("path", ""))
            if key in selected:
                att["selected"] = selected[key]
        if changed:
            unit.add_message("来源文件有变化，需重新确认")
            notes.append(f"{unit.name}：来源文件有变化，已标记重新确认")
        merged.append(unit)
    return merged, notes
