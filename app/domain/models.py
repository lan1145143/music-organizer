"""领域模型：来源、整理单元（专辑/散曲/压缩包/附件）、计划项、执行结果。"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class UnitKind(str, Enum):
    ALBUM = "album"        # 完整抓轨/专辑整夹
    TRACK = "track"        # 散曲
    ARCHIVE = "archive"    # 独立压缩包
    ATTACHMENT = "attachment"  # 未关联附件（仅待处理列表展示）

    @property
    def label(self) -> str:
        return {
            UnitKind.ALBUM: "专辑整夹",
            UnitKind.TRACK: "散曲",
            UnitKind.ARCHIVE: "压缩包",
            UnitKind.ATTACHMENT: "未关联附件",
        }[self]


class UnitStatus(str, Enum):
    READY = "ready"
    NEEDS_BOUNDARY = "needs_boundary"   # 候选专辑边界待确认
    NEEDS_INFO = "needs_info"           # 缺必需整理信息
    INCOMPLETE = "incomplete"           # 可能不完整（CUE 引用缺失）
    MANUAL = "manual"                   # 资料分散，待手动处理
    BLOCKED = "blocked"                 # 冲突/阻止执行
    CHILD = "child"                     # 随父文件夹整体处理
    DONE = "done"
    MISSING = "missing"                 # 来源失联（文件被移动/删除）
    NEEDS_LINK = "needs_link"           # 歌词待关联（多候选/多语言/依据不足）

    @property
    def label(self) -> str:
        return {
            UnitStatus.READY: "就绪",
            UnitStatus.NEEDS_BOUNDARY: "待确认边界",
            UnitStatus.NEEDS_INFO: "待补全信息",
            UnitStatus.INCOMPLETE: "可能不完整",
            UnitStatus.MANUAL: "待手动处理",
            UnitStatus.BLOCKED: "有冲突",
            UnitStatus.CHILD: "随文件夹整体处理",
            UnitStatus.DONE: "已处理",
            UnitStatus.MISSING: "来源失联",
            UnitStatus.NEEDS_LINK: "待关联",
        }[self]

    @property
    def is_pending(self) -> bool:
        return self in (UnitStatus.NEEDS_BOUNDARY, UnitStatus.NEEDS_INFO,
                        UnitStatus.INCOMPLETE, UnitStatus.MANUAL, UnitStatus.BLOCKED,
                        UnitStatus.MISSING, UnitStatus.NEEDS_LINK)


class PlanStatus(str, Enum):
    READY = "ready"
    CONFLICT = "conflict"
    BLOCKED = "blocked"
    SKIPPED = "skipped"

    @property
    def label(self) -> str:
        return {PlanStatus.READY: "可执行", PlanStatus.CONFLICT: "需处理冲突",
                PlanStatus.BLOCKED: "阻止执行", PlanStatus.SKIPPED: "已跳过"}[self]


class OpKind(str, Enum):
    ALBUM_MOVE = "album_move"        # 整夹移动/复制
    TRACK_MOVE = "track_move"        # 散曲移动/复制
    ATTACHMENT_MOVE = "attachment_move"
    CUE_FIX = "cue_fix"
    ARCHIVE_ARCHIVE = "archive_archive"
    DUPLICATE_MOVE = "duplicate_move"
    LYRICS_MOVE = "lyrics_move"      # 歌词随曲移动/复制

    @property
    def label(self) -> str:
        return {
            OpKind.ALBUM_MOVE: "整夹",
            OpKind.TRACK_MOVE: "散曲",
            OpKind.ATTACHMENT_MOVE: "附件",
            OpKind.CUE_FIX: "CUE 引用修正",
            OpKind.ARCHIVE_ARCHIVE: "原包归档",
            OpKind.DUPLICATE_MOVE: "重复项处理",
            OpKind.LYRICS_MOVE: "歌词附件",
        }[self]


TRACK_FIELDS = ("title", "artist", "album", "album_artist", "track", "disc", "year", "genre")
FIELD_LABELS = {
    "title": "歌曲名", "artist": "歌曲艺术家", "album": "专辑",
    "album_artist": "专辑艺术家", "track": "曲号", "disc": "碟号",
    "year": "年份", "genre": "流派", "duration": "时长", "format": "格式",
}
ORIGIN_LABELS = {"tag": "标签", "cue": "CUE", "filename": "文件名/目录名", "user": "用户编辑", "": "未知"}


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
def _uuid() -> str:
    return uuid.uuid4().hex


@dataclass
class SourceRoot:
    id: str = field(default_factory=_uuid)
    path: str = ""
    added_at: str = ""
    included: bool = True
    # False=已登记但尚未扫描（等待确认目标）；True=已完成至少一次扫描
    scanned: bool = True

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class Attachment:
    path: str = ""
    role: str = "misc"       # cover | scans | log | cue | info | misc
    selected: bool = True
    confidence: str = "high"  # high | medium | none
    note: str = ""
    owner_hint: str = ""     # 关联建议来源说明
    # 外置歌词专用字段（仅读预览，不改写、不转码）
    is_lyrics: bool = False       # 是否为歌词文件
    lyrics_encoding: str = ""     # 检测到的编码
    lyrics_preview: str = ""      # 预览片段
    has_lrc_timestamps: bool = False
    lyrics_metadata: dict = field(default_factory=dict)   # LRC 标签提取结果
    match_confidence: str = ""    # high | medium | low | pending
    match_evidence: list = field(default_factory=list)     # 匹配依据（展示给用户）
    match_score: float = 0.0
    link_status: str = ""         # auto | manual | pending
    lyrics_candidates: list = field(default_factory=list)   # 待关联时的候选曲目

    ROLE_LABELS = {"cover": "主封面", "scans": "扫描图", "log": "抓轨日志",
                   "cue": "CUE", "info": "说明/校验文件", "lyrics": "外置歌词",
                   "misc": "其他附件"}

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Attachment":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Unit:
    id: str = field(default_factory=_uuid)
    kind: str = UnitKind.TRACK.value
    source_id: str = ""
    path: str = ""            # 文件或目录绝对路径
    name: str = ""            # 展示名（默认路径名）
    checked: bool = False
    # None=系统默认（按分组规则自动）；True/False=用户明确勾选/取消，重扫不覆盖
    user_checked: bool | None = None
    status: str = UnitStatus.READY.value
    messages: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    # 整理信息（可编辑，不写回文件）
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track: int | None = None
    disc: int | None = None
    total_discs: int = 1
    year: str = ""
    genre: str = ""
    origins: dict[str, str] = field(default_factory=dict)

    # 媒体信息
    duration: float = 0.0
    fmt: str = ""
    bitrate: int = 0
    sample_rate: int = 0
    bits: int = 0
    size: int = 0
    file_count: int = 0

    # 专辑边界
    boundary_path: str = ""
    candidate_parent: str = ""
    boundary_confirmed: bool = False
    auto_boundary: bool = False   # 扫描时自动采用的推荐边界（用户可改选/取消）
    mode_hint: str = "album"       # album | tracks

    # 整理用专辑艺术家推断（仅目录归属建议；确认值存 album_artist/origins）
    artist_suggestion: str = ""
    artist_suggestion_confidence: str = ""   # high | medium | low | ""
    artist_suggestion_evidence: str = ""
    artist_candidates: list[str] = field(default_factory=list)

    # 散曲专辑识别（字段保留以兼容旧 state.json；直读架构下不再写入，
    # 专辑来源以 origins["album"] 为准）
    album_confidence: str = ""

    # 结构
    attachments: list[dict] = field(default_factory=list)
    virtual_tracks: list[dict] = field(default_factory=list)
    audio_files: list[str] = field(default_factory=list)
    cue_files: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    parent_id: str = ""
    sig: dict[str, list] = field(default_factory=dict)  # 相对路径 -> [size, mtime]

    # 来源与归档
    workspace: str = ""
    archive: dict[str, Any] = field(default_factory=dict)
    duplicate_group: str = ""
    dup_role: str = ""             # keep | duplicate | suspect
    dup_note: str = ""
    manual_note: str = ""
    target_override: str = ""
    last_target: str = ""

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Unit":
        allowed = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in allowed})

    @property
    def kind_enum(self) -> UnitKind:
        return UnitKind(self.kind)

    @property
    def status_enum(self) -> UnitStatus:
        return UnitStatus(self.status)

    @property
    def is_album(self) -> bool:
        return self.kind == UnitKind.ALBUM.value

    @property
    def is_track(self) -> bool:
        return self.kind == UnitKind.TRACK.value

    @property
    def is_archive(self) -> bool:
        return self.kind == UnitKind.ARCHIVE.value

    @property
    def display_album(self) -> str:
        return self.album or ("未分类专辑" if self.is_track else "")

    def add_message(self, text: str) -> None:
        if text not in self.messages:
            self.messages.append(text)

    def size_hint(self) -> str:
        size = self.size
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024.0
        return ""


@dataclass
class PlanItem:
    id: str = field(default_factory=_uuid)
    op: str = OpKind.TRACK_MOVE.value
    unit_id: str = ""
    unit_kind: str = UnitKind.TRACK.value
    label: str = ""
    source: str = ""
    target: str = ""
    action: str = "move"        # move | copy
    selected: bool = True
    status: str = PlanStatus.READY.value
    messages: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)   # 整夹内容摘要/展开项
    depends_on: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @property
    def status_enum(self) -> PlanStatus:
        return PlanStatus(self.status)

    @property
    def op_enum(self) -> OpKind:
        return OpKind(self.op)

    def add_message(self, text: str) -> None:
        if text not in self.messages:
            self.messages.append(text)


@dataclass
class DuplicateGroup:
    id: str = field(default_factory=_uuid)
    kind: str = "exact"          # exact | suspect
    key: str = ""
    unit_ids: list[str] = field(default_factory=list)
    keep_unit_id: str = ""
    note: str = ""
    detail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DuplicateGroup":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class TaskResult:
    item_id: str = ""
    op: str = ""
    status: str = "success"      # success | failed | skipped | partial | cancelled
    source: str = ""
    target: str = ""
    message: str = ""
    created: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    bytes_done: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
