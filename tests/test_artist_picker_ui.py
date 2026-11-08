"""P0-1 回归：批量指定整理艺术家对话框。

覆盖：打开对话框 → 单一建议预填 → 预览文本正常生成；
异常时预览区必须显示可理解的错误（不允许静默失败）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.domain.models import Unit  # noqa: E402
from app.ui.artist_picker import ArtistPickerDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeService:
    """对话框只用到这三个成员，替身足够。"""

    def __init__(self, target_dir: str = "") -> None:
        self.settings = SimpleNamespace(target_dir=target_dir)

    def existing_artist_dirs(self, keyword: str = "", limit: int = 30) -> list[dict]:
        return []

    def preview_artist_target(self, artist: str, unit: Unit) -> str:
        return str(Path(self.settings.target_dir) / artist / unit.name)


def test_single_suggestion_prefills_and_renders_preview(qapp, tmp_path):
    """单一建议 → 预填 → 各条目预览必须出现在预览区（回归 lines.append 两参数崩溃）。"""
    units = [Unit(kind="album", name="专辑A", artist_suggestion="Ado"),
             Unit(kind="track", name="01 - Ado - 唱", artist_suggestion="Ado")]
    dlg = ArtistPickerDialog(FakeService(str(tmp_path)), units)
    assert dlg.organize_artist() == "Ado"
    text = dlg.preview.toPlainText()
    assert "各条目预览" in text
    assert "专辑A" in text
    assert "01 - Ado - 唱" in text


def test_typed_name_renders_preview(qapp, tmp_path):
    dlg = ArtistPickerDialog(FakeService(str(tmp_path)), [Unit(kind="track", name="曲子")])
    dlg.name_edit.setText("Yoasobi")
    text = dlg.preview.toPlainText()
    assert "整理艺术家：Yoasobi" in text
    assert "曲子" in text


def test_preview_error_is_visible_not_silent(qapp, tmp_path, monkeypatch):
    svc = FakeService(str(tmp_path))
    dlg = ArtistPickerDialog(svc, [Unit(kind="track", name="曲子")])

    def boom(artist, unit):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "preview_artist_target", boom)
    dlg.name_edit.setText("某人")
    text = dlg.preview.toPlainText()
    assert "生成预览时出错" in text
    assert "RuntimeError" in text
    assert "boom" in text
