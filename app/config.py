"""设置持久化（settings.json，原子写入）。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

DEFAULT_ACCENT = "#3B6EA5"


@dataclass
class BackgroundConfig:
    image: str = ""          # 复制到 data/backgrounds 后的文件名
    display: str = "fill"    # fill | fit | stretch
    align: str = "center"    # center | top-left | top | ... | bottom-right
    intensity: float = 0.55  # 背景图不透明度
    mask: float = 0.35       # 主题色遮罩深浅
    panel_opacity: float = 0.90  # 内容面板透明度


@dataclass
class Settings:
    target_dir: str = ""
    mode: str = "move"                  # move | copy
    template: str = "A"                 # A | B
    theme: str = "light"                # light | dark | paper
    accent: str = DEFAULT_ACCENT
    background: BackgroundConfig = field(default_factory=BackgroundConfig)
    window_geometry: str = ""
    splitter_sizes: list[int] = field(default_factory=lambda: [260, 720, 360])
    track_digits: int = 2
    verify_hash: bool = True            # 跨盘移动/复制校验哈希

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        bg = BackgroundConfig(**{k: v for k, v in (data.get("background") or {}).items()
                                 if k in BackgroundConfig.__dataclass_fields__})
        # 旧版 settings.json 可能带已废除的键（rules 策略开关、history_retention、
        # confirm_before_run、workspace_dir_override）：直接忽略，避免未知键报错
        fields = {k: v for k, v in data.items()
                  if k in cls.__dataclass_fields__ and k != "background"}
        obj = cls(**fields)
        obj.background = bg
        return obj

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.settings = self.load()

    def load(self) -> Settings:
        if not self.path.is_file():
            return Settings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return Settings.from_dict(data)
        except (OSError, ValueError):
            # 损坏时保留原文件另存，避免覆盖用户数据
            try:
                self.path.replace(self.path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return Settings()

    def save(self, settings: Settings | None = None) -> None:
        if settings is not None:
            self.settings = settings
        atomic_write_text(self.path, json.dumps(self.settings.to_dict(), ensure_ascii=False, indent=2))
