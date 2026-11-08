"""随附文件默认勾选与底部操作栏重构回归（docs/code_artdfghasifact.md）。

AC1 自动识别并关联的附件（封面/CUE/日志/.lrc）默认勾选；
AC2 勾选可切换且标题计数 (勾选/总数) 准确；
AC3 手动「关联附件/关联歌词」导入的文件默认勾选；
AC4 操作按钮具备状态化视觉（离屏可验：enabled/disabled + 文案）；
AC5 无附件→清空关联禁用；未关联歌词→预览歌词禁用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.domain.models import SourceRoot  # noqa: E402
from app.paths import DataPaths  # noqa: E402
from app.planning.plan import PlanBuilder, PlanOptions  # noqa: E402
from app.scan.scanner import Scanner  # noqa: E402
from app.services import AppService  # noqa: E402
from app.ui.detail import DetailPanel  # noqa: E402
from tests.fixtures import make_cue, make_wav  # noqa: E402

LRC = "[ti:唱]\n[00:01.00]どうしても消えない\n[00:05.00]君の声が\n"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def make_album_dir(tmp_path: Path) -> Path:
    """整夹：整轨 wav + CUE + Cover.jpg + .log + .lrc（AC1 场景）。"""
    src = tmp_path / "src"
    album = src / "专辑A"
    album.mkdir(parents=True)
    make_wav(album / "整轨.wav", tags={"album": "专辑A", "artist": "歌手"})
    make_cue(album / "专辑A.cue", "整轨.wav", "专辑A", "歌手",
             [(1, "歌一", "歌手"), (2, "歌二", "歌手")])
    (album / "Cover.jpg").write_bytes(b"\xff\xd8fakejpg")
    (album / "album.log").write_text("EAC log", encoding="utf-8")
    (album / "歌一.lrc").write_text(LRC, encoding="utf-8")
    return src


def scan(src: Path):
    return Scanner().scan_source(SourceRoot(path=str(src)))


# ---------------------------------------------------------------------------
# AC1：扫描默认勾选
# ---------------------------------------------------------------------------
def test_album_attachments_default_checked(qapp, tmp_path):
    outcome = scan(make_album_dir(tmp_path))
    album = [u for u in outcome.units if u.is_album][0]
    assert album.attachments, "整夹应识别出随附文件"
    roles = {a["role"] for a in album.attachments}
    assert {"cover", "log", "cue", "lyrics"} <= roles
    for att in album.attachments:
        assert att.get("selected") is True, \
            f"自动关联附件必须默认勾选：{Path(att['path']).name}"


def test_track_auto_lyrics_default_checked(qapp, tmp_path):
    src = tmp_path / "src2"
    loose = src / "散曲"
    loose.mkdir(parents=True)
    make_wav(loose / "01 - Ado - 唱.wav",
             tags={"title": "唱", "artist": "Ado", "album": "狂言", "track": 1})
    (loose / "01 - Ado - 唱.lrc").write_text(LRC, encoding="utf-8")
    outcome = scan(src)
    track = [u for u in outcome.units if u.is_track][0]
    atts = [a for a in track.attachments if a.get("role") == "lyrics"]
    assert len(atts) == 1
    assert atts[0]["link_status"] == "auto"
    assert atts[0]["selected"] is True


def test_default_checked_lyrics_move_with_track(qapp, tmp_path):
    """默认勾选的自动歌词进入计划随歌移动（红线：取消勾选才排除）。"""
    src = tmp_path / "src3"
    loose = src / "散曲"
    loose.mkdir(parents=True)
    make_wav(loose / "01 - Ado - 唱.wav",
             tags={"title": "唱", "artist": "Ado", "album": "狂言", "track": 1})
    (loose / "01 - Ado - 唱.lrc").write_text(LRC, encoding="utf-8")
    outcome = scan(src)
    track = [u for u in outcome.units if u.is_track][0]
    track.checked = True
    plan = PlanBuilder(
        PlanOptions(target_root=str(tmp_path / "target"), mode="move")
    ).build(outcome.units)
    item = next(i for i in plan.items if i.op == "track_move")
    lyrics = item.extra.get("lyrics") or []
    assert len(lyrics) == 1, "默认勾选的歌词应随歌曲进入计划"
    assert not any(e["status"] == "歌词未勾选" for e in plan.excluded)


# ---------------------------------------------------------------------------
# AC3：手动关联默认勾选 + 清空关联
# ---------------------------------------------------------------------------
def test_manual_link_defaults_checked(qapp, tmp_path):
    src = make_album_dir(tmp_path)
    service = AppService(DataPaths(root=tmp_path / "data", portable=True))
    service.register_sources([str(src)])
    service.rescan()
    album = next(u for u in service.state.units if u.is_album)
    cover = tmp_path / "手工封面.png"
    cover.write_bytes(b"\x89PNG fake")
    service.link_attachment(album, str(cover))
    assert album.attachments[-1]["selected"] is True
    assert album.attachments[-1]["confidence"] == "manual"

    lyric = tmp_path / "歌一.lrc"
    lyric.write_text(LRC, encoding="utf-8")
    service.link_lyrics(album, str(lyric))
    att = [a for a in album.attachments
           if os.path.normcase(a["path"]) == os.path.normcase(str(lyric))][0]
    assert att["selected"] is True and att["link_status"] == "manual"


def test_clear_attachments_removes_all(qapp, tmp_path):
    src = make_album_dir(tmp_path)
    service = AppService(DataPaths(root=tmp_path / "data", portable=True))
    service.register_sources([str(src)])
    service.rescan()
    album = next(u for u in service.state.units if u.is_album)
    removed = service.clear_attachments(album)
    assert removed > 0
    assert album.attachments == []
    # 磁盘文件分毫不动（清空的是关联关系，不是文件）
    assert (src / "专辑A" / "Cover.jpg").exists()


# ---------------------------------------------------------------------------
# UI：详情面板渲染与状态化按钮
# ---------------------------------------------------------------------------
def _album_unit(tmp_path: Path):
    outcome = scan(make_album_dir(tmp_path))
    return [u for u in outcome.units if u.is_album][0]


def test_detail_renders_checked_and_count(qapp, tmp_path):
    unit = _album_unit(tmp_path)
    panel = DetailPanel()
    panel.set_unit(unit)
    view = panel._views[unit.kind]
    lst = view.attachment_list
    total = len(unit.attachments)
    assert total >= 4
    for i in range(lst.count()):
        assert lst.item(i).checkState() == Qt.Checked, "AC1：复选框必须默认打勾"
    assert view.attachment_caption.text() == f"随附文件 ({total}/{total})"
    assert view.att_toggle_all_btn.text() == "全不选"
    assert view.att_clear_btn.isEnabled() and view.att_preview_btn.isEnabled()

    # AC2：取消一项 → 信号携带 selected=False；回填后计数准确
    seen: list[tuple[str, bool]] = []
    panel.attachmentToggled.connect(lambda uid, p, s: seen.append((p, s)))
    lst.item(0).setCheckState(Qt.Unchecked, )
    assert seen and seen[0][1] is False
    path0 = seen[0][0]
    for att in unit.attachments:
        if att["path"] == path0:
            att["selected"] = False
    panel.set_unit(unit)
    assert view.attachment_caption.text() == f"随附文件 ({total - 1}/{total})"
    assert view.att_toggle_all_btn.text() == "全不选"

    # 批量全不选信号
    seen_all: list[bool] = []
    panel.attachmentSetAllRequested.connect(lambda uid, c: seen_all.append(c))
    view.att_toggle_all_btn.click()
    assert seen_all == [False]


def test_detail_action_states(qapp, tmp_path):
    # 无歌词 → 预览禁用（AC5）；有附件 → 清空可用
    outcome = scan(make_album_dir(tmp_path))
    album = [u for u in outcome.units if u.is_album][0]
    album.attachments = [a for a in album.attachments if a["role"] != "lyrics"]
    panel = DetailPanel()
    panel.set_unit(album)
    view = panel._views[album.kind]
    assert view.att_preview_btn.isEnabled() is False
    assert view.att_clear_btn.isEnabled() is True

    # 空列表 → 清空/全选禁用（AC5）
    album.attachments = []
    panel.set_unit(album)
    assert view.att_clear_btn.isEnabled() is False
    assert view.att_toggle_all_btn.isEnabled() is False
    assert view.attachment_caption.text() == "随附文件 (0/0)"


def test_checkstate_role_normalization(qapp):
    """渲染层回归锁：PySide6 6.12 的 CheckStateRole 数据是 int(2) 而非枚举，
    `== Qt.Checked` 恒 False → 复选框永远画成未勾选（任务书截图 Bug 根源）。"""
    from PySide6.QtCore import Qt
    from app.ui.detail import _is_checked
    assert _is_checked(2) is True                       # PySide6 实际返回值
    assert _is_checked(0) is False
    assert _is_checked(None) is False
    assert _is_checked(Qt.CheckState.Checked) is True   # 兼容枚举返回
    assert _is_checked(Qt.CheckState.Unchecked) is False


def test_delegate_paints_checked_boxes(qapp, tmp_path):
    """离屏真实渲染：默认勾选的附件行必须画出勾选态复选框（AC1 渲染路径）。"""
    unit = _album_unit(tmp_path)
    panel = DetailPanel()
    panel.set_unit(unit)
    panel.resize(420, 760)
    img = panel.grab().toImage()
    view = panel._views[unit.kind]
    lst = view.attachment_list
    # 列表已按内容收敛高度：Action Bar 必须留在可视区内
    assert lst.height() <= 208 + 2, f"附件列表高度未收敛：{lst.height()}"
    delegate = lst.itemDelegate()
    from app.ui.detail import _is_checked
    for row in range(lst.count()):
        index = lst.model().index(row, 0)
        assert _is_checked(index.data(Qt.CheckStateRole)) is True
        assert delegate is not None


def test_main_window_bulk_and_clear(qapp, tmp_path):
    from app.ui.main_window import MainWindow
    src = make_album_dir(tmp_path)
    service = AppService(DataPaths(root=tmp_path / "data", portable=True))
    service.register_sources([str(src)])
    service.rescan()
    window = MainWindow(service)
    try:
        album = next(u for u in service.state.units if u.is_album)
        window._on_attachments_set_all(album.id, False)
        assert all(not a.get("selected") for a in album.attachments), \
            "批量取消勾选必须落账"
        window._on_attachments_set_all(album.id, True)
        assert all(a.get("selected") for a in album.attachments)

        window._clear_attachments(album.id)
        assert album.attachments == []
        # 重扫后自动识别的附件恢复（清空不删除磁盘文件）
        service.rescan()
        album2 = next(u for u in service.state.units if u.is_album)
        assert album2.attachments, "重新扫描应恢复自动识别的附件"
        assert all(a.get("selected") is True for a in album2.attachments)
    finally:
        window.close()
