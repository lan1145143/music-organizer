"""程序路径与便携数据目录解析。

免安装交付：数据默认放在程序目录下的 ``data/``。若该位置不可写
（例如放在只读介质或被策略保护），回退到 ``%LOCALAPPDATA%\\音乐整理工具\\data``
并在界面明确提示，避免静默丢失历史与设置。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "音乐整理工具"
APP_ID = "MusicOrganizer"


def app_dir() -> Path:
    """程序所在目录（源码运行时为项目根目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".write_test", delete=True):
            return True
    except OSError:
        return False


def _fallback_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME / "data"


class DataPaths:
    """便携数据目录布局。"""

    def __init__(self, root: Path | None = None, portable: bool | None = None) -> None:
        if root is None:
            candidate = app_dir() / "data"
            if portable is False:
                root = _fallback_root()
                self.portable = False
            elif portable is True:
                root = candidate
                self.portable = True
            elif _is_writable(candidate):
                root = candidate
                self.portable = True
            else:
                root = _fallback_root()
                self.portable = False
        else:
            self.portable = portable if portable is not None else True
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.root / "settings.json"
        self.state_file = self.root / "state.json"
        self.state_backup = self.root / "state.bak.json"
        self.artist_map_file = self.root / "artist_map.json"
        self.history_dir = self.root / "history"
        self.workspace_dir = self.root / "workspaces"
        self.log_dir = self.root / "logs"
        self.background_dir = self.root / "backgrounds"
        for d in (self.history_dir, self.workspace_dir, self.log_dir, self.background_dir):
            d.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        kind = "便携目录" if self.portable else "回退目录（程序目录不可写）"
        return f"{self.root}（{kind}）"
