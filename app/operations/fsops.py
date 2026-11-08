"""文件系统基础操作：同盘重命名、跨盘复制校验后删除、复制、校验。

跨盘移动遵循：先整组复制 → 校验通过 → 才开始删除对应源文件；
来源在校验与删除之间发生变化时停止删除并保留已验证目标。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..domain.naming import walk_pruned

CHUNK = 1024 * 1024
ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]


class CancelledError(RuntimeError):
    pass


@dataclass
class TransferResult:
    created: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    bytes_done: int = 0
    verified: bool = True
    message: str = ""
    source_left: list[str] = field(default_factory=list)
    cancelled: bool = False


def same_volume(a: Path, b: Path) -> bool:
    try:
        return Path(os.path.abspath(a)).drive.casefold() == Path(os.path.abspath(b)).drive.casefold()
    except (OSError, ValueError):
        return False


def ensure_parents_recorded(path: Path) -> list[Path]:
    """mkdir -p 并返回本次**新建**的目录（浅→深，已存在的不返回）。

    供执行层记录"本次操作创建了哪些目录"，撤销时据此清理空目录，
    而不是靠推测删除目标父目录。创建失败不在此处报错——
    后续文件写入会自然失败；未建成的目录撤销时按不存在跳过。
    """
    missing: list[Path] = []
    probe = Path(path)
    while not probe.exists():
        missing.append(probe)
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    missing.reverse()
    for directory in missing:
        try:
            directory.mkdir(exist_ok=True)
        except OSError:
            continue
    return missing


def hash_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def file_state(path: str | Path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_size, int(st.st_mtime)


def _file_digest(path: str | Path, cancel: CancelCb | None = None) -> str:
    """流式计算文件 SHA-256（复制校验用：verify_hash 时比对源/目标内容摘要）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if cancel is not None and cancel():
                raise CancelledError()
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _copy_file(src: Path, dst: Path, progress: ProgressCb | None, cancel: CancelCb | None,
               done: int, total: int) -> tuple[int, str]:
    """复制单个文件，返回 (复制的字节数, 源内容 SHA-256)。

    源摘要在读取源文件的同一循环里顺带算出，不增加额外读盘。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        with open(src, "rb") as fh_in, open(dst, "wb") as fh_out:
            copied = 0
            while True:
                if cancel is not None and cancel():
                    raise CancelledError()
                block = fh_in.read(CHUNK)
                if not block:
                    break
                fh_out.write(block)
                digest.update(block)
                copied += len(block)
                if progress is not None:
                    progress(done + copied, total, dst.name)
            fh_out.flush()
            os.fsync(fh_out.fileno())
    except CancelledError:
        # 目标文件是本次调用以 "wb" 新建的半成品：取消时必须清掉，
        # 不能留下说"已清理"却实际存在的部分文件
        try:
            os.remove(dst)
        except OSError:
            pass
        raise
    shutil.copystat(src, dst)
    return copied, digest.hexdigest()


def tree_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in walk_pruned(root):
        dirnames.sort()
        for name in sorted(filenames):
            out.append(Path(dirpath) / name)
    return out


def _remove_created_files(result: "TransferResult", dst: Path) -> None:
    """清掉本次调用已复制的文件（均为新建，可安全删除），dst 根目录本身保留。"""
    for path in result.created:
        if os.path.normcase(path) == os.path.normcase(str(dst)):
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def copy_tree(src: Path, dst: Path, progress: ProgressCb | None = None,
              cancel: CancelCb | None = None, verify: bool = True) -> TransferResult:
    result = TransferResult()
    files = tree_files(src)
    total = sum(p.stat().st_size for p in files if p.exists())
    done = 0
    dst.mkdir(parents=True, exist_ok=True)
    result.created.append(str(dst))
    digests: dict[str, str] = {}   # normcase(目标路径) -> 源内容 SHA-256
    try:
        for src_file in files:
            rel = src_file.relative_to(src)
            target = dst / rel
            try:
                size = src_file.stat().st_size
            except OSError:
                continue
            copied, src_digest = _copy_file(src_file, target, progress, cancel, done, total)
            done += copied
            result.bytes_done += copied
            result.created.append(str(target))
            digests[os.path.normcase(str(target))] = src_digest
            if verify and size != target.stat().st_size:
                raise OSError(f"复制大小不一致：{target}")
    except CancelledError:
        # 清掉本次调用已复制的文件（均为新建，可安全删除），再向上传播
        _remove_created_files(result, dst)
        raise
    if verify:
        try:
            for src_file in files:
                target = dst / src_file.relative_to(src)
                if not target.exists() or target.stat().st_size != src_file.stat().st_size:
                    result.verified = False
                    break
                expected = digests.get(os.path.normcase(str(target)))
                if expected is not None and _file_digest(target, cancel) != expected:
                    result.verified = False
                    result.message = "复制内容校验不一致（哈希）"
                    break
        except CancelledError:
            _remove_created_files(result, dst)
            raise
    return result


def copy_file_op(src: Path, dst: Path, progress: ProgressCb | None = None,
                 cancel: CancelCb | None = None, verify: bool = True) -> TransferResult:
    result = TransferResult()
    size = src.stat().st_size
    copied, src_digest = _copy_file(src, dst, progress, cancel, 0, size)
    result.bytes_done = copied
    result.created.append(str(dst))
    try:
        if verify:
            if not dst.exists() or dst.stat().st_size != size:
                result.verified = False
                result.message = "复制后大小校验不一致"
            elif _file_digest(dst, cancel) != src_digest:
                result.verified = False
                result.message = "复制内容校验不一致（哈希）"
    except CancelledError:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise
    return result


def move_file(src: Path, dst: Path, progress: ProgressCb | None = None,
              cancel: CancelCb | None = None, verify: bool = True) -> TransferResult:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if same_volume(src, dst):
        before = file_state(src)
        os.replace(src, dst)
        result = TransferResult(created=[str(dst)], removed=[str(src)])
        if not dst.exists():
            result.verified = False
            result.message = "移动后目标不存在"
        return result
    # 跨盘：先复制校验，再删除来源
    result = copy_file_op(src, dst, progress, cancel, verify)
    if not result.verified:
        result.message = "复制校验未通过，源文件已保留"
        result.created = [str(dst)]
        return result
    return _delete_sources([src], result, before_ok=True)


def move_tree(src: Path, dst: Path, progress: ProgressCb | None = None,
              cancel: CancelCb | None = None, verify: bool = True,
              expected: dict[str, tuple[int, int]] | None = None) -> TransferResult:
    """移动目录：同盘 rename；跨盘先整组复制并校验，再删除来源。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if same_volume(src, dst):
        os.replace(src, dst)
        created = [str(dst)] + [str(p) for p in tree_files(dst)] if dst.is_dir() else [str(dst)]
        result = TransferResult(created=created, removed=[str(src)])
        if not dst.exists():
            result.verified = False
            result.message = "移动后目标目录不存在"
        return result

    result = copy_tree(src, dst, progress, cancel, verify)
    if not result.verified:
        result.message = "整组复制校验未通过，来源未删除"
        return result
    changed = _changed_files(src, expected)
    if changed:
        result.message = "复制后来源发生变化，已停止删除：" + "、".join(changed[:3])
        result.source_left = [str(src)]
        return result
    return _delete_sources([src], result, before_ok=True)


def _changed_files(root: Path, expected: dict[str, tuple[int, int]] | None) -> list[str]:
    if not expected:
        return []
    changed: list[str] = []
    for path in tree_files(root):
        key = os.path.normcase(str(path))
        record = expected.get(key)
        if record is None:
            continue
        try:
            size, mtime = file_state(path)
        except OSError:
            changed.append(path.name)
            continue
        if size != record[0] or mtime != record[1]:
            changed.append(path.name)
    return changed


def _delete_sources(sources: list[Path], result: TransferResult,
                    before_ok: bool = True) -> TransferResult:
    left: list[str] = []
    for src in sources:
        try:
            if src.is_dir():
                for path in sorted(tree_files(src), reverse=True):
                    try:
                        os.remove(path)
                        result.removed.append(str(path))
                    except OSError:
                        left.append(str(path))
                for dirpath, _dirnames, _files in sorted(walk_pruned(src),
                                                         key=lambda x: -len(x[0])):
                    try:
                        os.rmdir(dirpath)
                    except OSError:
                        left.append(dirpath)
            else:
                try:
                    os.remove(src)
                    result.removed.append(str(src))
                except OSError:
                    left.append(str(src))
        except OSError as exc:
            left.append(f"{src}: {exc}")
    result.source_left = left
    if left:
        result.message = "目标已就绪，来源未完全移除"
    return result


def remove_single_empty_dir(path: Path) -> bool:
    """仅在目录确认存在且为空（无文件、无子目录）时删除该目录本身。

    不递归、不上溯父级。返回是否实际删除。
    """
    try:
        if not path.is_dir():
            return False
        next(path.iterdir())
        return False  # 还有任何条目（文件或子目录）→ 保留
    except StopIteration:
        pass
    except OSError:
        return False
    try:
        os.rmdir(path)
        return True
    except OSError:
        return False


def count_dir_entries(path: Path) -> int:
    """目录内条目数（文件+子目录）；目录不存在返回 0。"""
    try:
        return sum(1 for _ in path.iterdir())
    except OSError:
        return 0
