"""重复隔离（非破坏性）：优选规则、目录忽略、移动/日志/改名、占用跳过、撤销。

任务书 docs/code_artifardtgct.md：查重后把组内多余副本安全移入来源根目录的
「a重复歌曲」，保留最佳版本原位；绝不删除，可撤销；扫描/查重忽略隔离目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.domain.models import SourceRoot, Unit, UnitKind  # noqa: E402
from app.operations import fsops  # noqa: E402
from app.paths import DataPaths  # noqa: E402
from app.scan.isolation import (append_isolation_log, isolation_dir,  # noqa: E402
                                select_keep, unique_isolation_name)
from app.scan.scanner import Scanner  # noqa: E402
from app.services import AppService  # noqa: E402
from tests.fixtures import make_wav  # noqa: E402

ISO_DIR = "a重复歌曲"


# ----------------------------------------------------------------------
# 素材辅助
# ----------------------------------------------------------------------
def _track(tmp_path: Path, rel: str, *, fmt: str = "flac", bitrate: int = 0,
           sample_rate: int = 0, bits: int = 0, size: int = 0, mtime: int = 0,
           title: str = "晴天") -> Unit:
    path = tmp_path / rel
    unit = Unit(kind=UnitKind.TRACK.value, path=str(path), name=Path(path).stem)
    unit.title = title
    unit.fmt = fmt
    unit.bitrate = bitrate
    unit.sample_rate = sample_rate
    unit.bits = bits
    unit.size = size
    if mtime:
        unit.sig = {os.path.normcase(str(path)): [size, mtime]}
    return unit


def _album(tmp_path: Path, rel: str, *, size: int, files: int, lossless: bool = True,
           mtime: int = 0) -> Unit:
    base = tmp_path / rel
    audio = [str(base / f"{i:02d}.{'flac' if lossless else 'mp3'}") for i in range(1, files + 1)]
    unit = Unit(kind=UnitKind.ALBUM.value, path=str(base), name=Path(base).name)
    unit.boundary_path = str(base)
    unit.audio_files = audio
    unit.file_count = files
    unit.size = size
    if mtime:
        unit.sig = {os.path.normcase(str(base)): [size, mtime]}
    return unit


def build_service_with_duplicates(tmp_path: Path, copies: int = 2,
                                  name: str = "晴天.wav"):
    """真实链路：登记来源 → 扫描 → 查重，返回 (service, source_root)。"""
    src = tmp_path / "音乐库"
    for index in range(copies):
        sub = src / f"目录{index}"
        sub.mkdir(parents=True)
        make_wav(sub / name, seconds=0.2, freq=440)
    service = AppService(DataPaths(root=tmp_path / "data"))
    service.register_sources([str(src)])
    service.rescan()
    check = service.run_duplicate_check()
    assert check["exact"] == 1
    return service, src


# ----------------------------------------------------------------------
# Step 3：组内优选（Keep vs Isolate）
# ----------------------------------------------------------------------
def test_lossless_wins_over_lossy(tmp_path):
    flac = _track(tmp_path, "a.flac", fmt="flac", bitrate=900000, size=100)
    mp3 = _track(tmp_path, "b.mp3", fmt="mp3", bitrate=320000, size=100)
    keep, isolates = select_keep([mp3, flac])
    assert keep is flac and isolates == [mp3]


def test_higher_bitrate_wins_within_same_format(tmp_path):
    hi = _track(tmp_path, "hi.mp3", fmt="mp3", bitrate=320000, size=300)
    lo = _track(tmp_path, "lo.mp3", fmt="mp3", bitrate=128000, size=120)
    keep, isolates = select_keep([lo, hi])
    assert keep is hi and isolates == [lo]


def test_sample_rate_and_bits_break_ties(tmp_path):
    better = _track(tmp_path, "better.flac", fmt="flac", bitrate=900000,
                    sample_rate=48000, bits=24, size=100)
    worse = _track(tmp_path, "worse.flac", fmt="flac", bitrate=900000,
                   sample_rate=44100, bits=16, size=100)
    keep, isolates = select_keep([worse, better])
    assert keep is better


def test_metadata_completeness_wins(tmp_path):
    probe = {os.path.normcase(str(tmp_path / "rich.flac")): (1, 1, 5),
             os.path.normcase(str(tmp_path / "bare.flac")): (0, 0, 0)}
    rich = _track(tmp_path, "rich.flac", fmt="flac", bitrate=900000, size=100)
    bare = _track(tmp_path, "bare.flac", fmt="flac", bitrate=900000, size=100)
    keep, isolates = select_keep([bare, rich], probe=lambda p: probe.get(
        os.path.normcase(p), (0, 0, 0)))
    assert keep is rich and isolates == [bare]


def test_size_then_older_mtime_fallback(tmp_path):
    big = _track(tmp_path, "big.flac", fmt="flac", size=200, mtime=1700000000)
    small = _track(tmp_path, "small.flac", fmt="flac", size=100, mtime=1700000000)
    keep, isolates = select_keep([small, big])
    assert keep is big and isolates == [small]
    # 体积一致：修改时间较早者优先
    old = _track(tmp_path, "old.flac", fmt="flac", size=100, mtime=1600000000)
    new = _track(tmp_path, "new.flac", fmt="flac", size=100, mtime=1700000000)
    keep2, isolates2 = select_keep([new, old])
    assert keep2 is old and isolates2 == [new]


def test_user_preferred_keep_respected(tmp_path):
    auto_keep = _track(tmp_path, "auto.flac", fmt="flac", bitrate=900000, size=200)
    chosen = _track(tmp_path, "chosen.flac", fmt="flac", bitrate=320000, size=100)
    keep, isolates = select_keep([auto_keep, chosen], preferred=chosen.id)
    assert keep is chosen and isolates == [auto_keep]


def test_album_group_ranked_by_lossless_ratio_and_size(tmp_path):
    lossless = _album(tmp_path, "无损", size=1000, files=3, lossless=True)
    lossy = _album(tmp_path, "有损", size=800, files=3, lossless=False)
    keep, isolates = select_keep([lossy, lossless])
    assert keep is lossless and isolates == [lossy]


# ----------------------------------------------------------------------
# 隔离目录：重名冲突与日志
# ----------------------------------------------------------------------
def test_unique_isolation_name_appends_dup_suffix(tmp_path):
    dest = tmp_path / ISO_DIR
    dest.mkdir()
    (dest / "song.mp3").write_bytes(b"x")
    used: set[str] = set()
    assert unique_isolation_name(dest, "song.mp3", used) == "song_dup1.mp3"
    assert unique_isolation_name(dest, "song.mp3", used) == "song_dup2.mp3"
    assert unique_isolation_name(dest, "other.mp3", used) == "other.mp3"


def test_isolation_log_line_format(tmp_path):
    dest = tmp_path / ISO_DIR
    assert append_isolation_log(dest, "晴天 (128kbps).mp3",
                                r"D:\Music\Old\晴天.mp3", r"D:\Music\Jay\晴天.flac")
    text = (dest / "isolation_log.txt").read_text(encoding="utf-8")
    assert "移入文件: \"晴天 (128kbps).mp3\"" in text
    assert "原路径: \"D:\\Music\\Old\\晴天.mp3\"" in text
    assert "保留版本: \"D:\\Music\\Jay\\晴天.flac\"" in text


# ----------------------------------------------------------------------
# Step 2：扫描/查重忽略隔离目录
# ----------------------------------------------------------------------
def test_scan_ignores_isolation_dir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_wav(src / "normal.wav", seconds=0.2)
    make_wav(src / ISO_DIR / "隔离文件.wav", seconds=0.2)
    outcome = Scanner().scan_source(SourceRoot(path=str(src)))
    paths = [u.path for u in outcome.units] + [a.path for a in outcome.archives]
    assert any(p.endswith("normal.wav") for p in paths)
    assert all(ISO_DIR not in p for p in paths)


def test_scan_skips_source_root_named_isolation(tmp_path):
    root = tmp_path / ISO_DIR
    root.mkdir()
    make_wav(root / "x.wav", seconds=0.2)
    outcome = Scanner().scan_source(SourceRoot(path=str(root)))
    assert outcome.units == [] and outcome.archives == []
    assert any("隔离" in e for e in outcome.errors)


# ----------------------------------------------------------------------
# Step 4：服务层隔离执行
# ----------------------------------------------------------------------
def _originals(src: Path, name: str = "晴天.wav") -> list[Path]:
    """来源内（隔离目录之外）仍留在原位的副本。"""
    return [p for p in src.rglob(name) if ISO_DIR not in p.parts]


def test_isolate_moves_copies_and_writes_log(tmp_path):
    service, src = build_service_with_duplicates(tmp_path)
    out = service.isolate_duplicates()
    assert out["moved"] == 1
    iso = src / ISO_DIR
    assert iso.is_dir()
    moved = [p for p in iso.iterdir() if p.name != "isolation_log.txt"]
    assert len(moved) == 1 and moved[0].suffix == ".wav"
    # 保留版本仍在来源目录（仅剩一份）
    assert len(_originals(src)) == 1
    log = (iso / "isolation_log.txt").read_text(encoding="utf-8")
    assert "移入文件:" in log and "原路径:" in log and "保留版本:" in log
    # 状态更新：重复组清空、副本单元已移除、历史可追溯
    assert service.state.duplicate_groups == []
    runs = service.history.list_runs()
    assert any(op.op == "duplicate_move" for run in runs for op in run.ops)
    assert out["isolation_dirs"] == [str(iso)]


def test_isolate_same_name_conflict_renamed_dup1(tmp_path):
    service, src = build_service_with_duplicates(tmp_path, copies=3)
    out = service.isolate_duplicates()
    assert out["moved"] == 2
    iso = src / ISO_DIR
    names = sorted(p.name for p in iso.iterdir() if p.suffix == ".wav")
    assert names == ["晴天.wav", "晴天_dup1.wav"]
    assert len(_originals(src)) == 1


def test_isolate_respects_manual_keep_override(tmp_path):
    service, src = build_service_with_duplicates(tmp_path)
    group = service.state.duplicate_groups[0]
    members = [service.state.unit_by_id(uid) for uid in group.unit_ids]
    auto_keep = service.duplicate_isolation_groups()[0]["keep"]
    other = next(u for u in members if u.id != auto_keep.id)
    out = service.isolate_duplicates(keep_overrides={group.id: other.id})
    assert out["moved"] == 1
    assert not Path(auto_keep.path).exists(), "被切换掉的自动保留项应改为隔离"
    assert Path(other.path).exists(), "手动指定的保留项必须留在原位"
    assert len(_originals(src)) == 1


def test_isolate_occupied_file_skipped_and_group_survives(tmp_path, monkeypatch):
    service, src = build_service_with_duplicates(tmp_path)
    group_id = service.state.duplicate_groups[0].id
    real_move = fsops.move_file

    def locked_move(src_path, dst_path, progress=None, cancel=None, verify=True):
        if "晴天" in Path(src_path).name:
            raise PermissionError(32, "另一个程序正在使用此文件，进程无法访问。")
        return real_move(src_path, dst_path, progress, cancel, verify)

    monkeypatch.setattr(fsops, "move_file", locked_move)
    out = service.isolate_duplicates()
    assert out["moved"] == 0
    assert len(out["failures"]) == 1
    assert "晴天" in out["failures"][0]
    # 文件原位保留、组保留可重试、徽标数量不变
    assert len(list(src.rglob("晴天.wav"))) == 2
    assert len(service.state.duplicate_groups) == 1
    assert service.state.duplicate_groups[0].id == group_id


def test_undo_isolation_restores_original_files(tmp_path):
    service, src = build_service_with_duplicates(tmp_path)
    out = service.isolate_duplicates()
    iso = src / ISO_DIR
    assert out["moved"] == 1
    record = next(r for r in service.history.list_runs()
                  if any(op.op == "duplicate_move" for op in r.ops))
    undo_record = service.undo_service.apply(service.undo_service.build(record))
    assert undo_record.counts.get("success") == 1
    # 两份副本都回到来源目录
    assert len(list(src.rglob("晴天.wav"))) == 2
    # 隔离日志保留（审计记录不随撤销删除），目录未误删
    assert (iso / "isolation_log.txt").is_file()


def test_isolation_groups_skip_missing_members(tmp_path):
    service, src = build_service_with_duplicates(tmp_path)
    # 手动删掉一份副本并标记失联 → 组只剩一个有效成员 → 不再可隔离
    victim = service.duplicate_isolation_groups()[0]["isolates"][0]
    Path(victim.path).unlink()
    victim.status = "missing"
    assert service.duplicate_isolation_groups() == []


# ----------------------------------------------------------------------
# Step 5：确认弹窗（离屏）
# ----------------------------------------------------------------------
pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.duplicate_isolation_dialog import (DuplicateIsolationDialog,  # noqa: E402
                                               GroupCard)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_dialog_builds_groups_and_counts(qapp, tmp_path):
    service, src = build_service_with_duplicates(tmp_path)
    dlg = DuplicateIsolationDialog(service)
    assert "发现 1 首重复可隔离歌曲" in dlg.title.text()
    assert dlg.btn_isolate.text() == f"一键移入「{ISO_DIR}」（1 首）"
    cards = [dlg.list_layout.itemAt(i).widget() for i in range(dlg.list_layout.count())]
    cards = [c for c in cards if isinstance(c, GroupCard)]
    assert len(cards) == 1
    assert len(cards[0].group["members"]) == 2


def test_dialog_click_row_swaps_keep(qapp, tmp_path):
    service, src = build_service_with_duplicates(tmp_path)
    dlg = DuplicateIsolationDialog(service)
    card = next(w for i, w in ((i, dlg.list_layout.itemAt(i).widget())
                               for i in range(dlg.list_layout.count()))
                if isinstance(w, GroupCard))
    group_id = card.group["id"]
    other = next(u for u in card.group["members"] if u.id != card.keep_id)
    card._set_keep(other.id)
    assert dlg.keep_by_group[group_id] == other.id
    # 服务层同样尊重弹窗里的切换结果
    groups = service.duplicate_isolation_groups(dlg.keep_by_group)
    assert groups[0]["keep"].id == other.id


def test_isolation_dir_helper_points_to_source_root(tmp_path):
    assert isolation_dir(tmp_path / "音乐") == tmp_path / "音乐" / ISO_DIR
