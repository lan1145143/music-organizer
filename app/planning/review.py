"""统一整理状态：全自动零干预流水线的双态机。

所有正常扫描项一律 READY，直接进入执行队列——不再存在"待确认边界 /
待补全信息 / 待选择处理方式 / 待手动处理"等人工拦截状态。
唯一例外是 SKIPPED_ERROR：执行前探测到来源失联（文件被外部移动/重命名/
删除）或无读取权限时，跳过该项并写入结果报告，绝不阻断整体计划执行。

提醒（reminders）仅供详情与预览展示，不影响流转：
- 未识别专辑 → 未分类专辑兜底；缺整理艺术家 → 未知艺术家兜底；
- 疑似重复（旧状态遗留）→ 保留各份；
- 重复检测未运行 → 显示"未检测"（不声称无重复）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.models import Unit, UnitStatus
# 兼容别名：外部（e2e/冒烟/测试）可能引用分组常量
GROUP_READY = "ready"
GROUP_NEEDS = "needs"          # 仅 SKIPPED_ERROR（来源失联）落入此组
GROUP_SIDE = "side"            # 已完成 / 随整夹处理（附属展示）

READY = UnitStatus.READY.value
SKIPPED_ERROR = UnitStatus.MISSING.value   # 失联复用 missing 状态承载"跳过并报告"


@dataclass
class UnitReview:
    unit_id: str = ""
    group: str = GROUP_READY
    organizable: bool = True
    blockers: list[str] = field(default_factory=list)     # 仅来源失联/无权限
    reminders: list[str] = field(default_factory=list)
    side_kind: str = ""          # done | child | ""

    @property
    def reason_text(self) -> str:
        return "；".join(self.blockers) if self.blockers else ""

    @property
    def reminder_text(self) -> str:
        return "；".join(self.reminders) if self.reminders else ""


def evaluate_unit(unit: Unit, policy=None) -> UnitReview:
    """计算一个单元的整理状态（纯函数，不修改单元）。

    policy 参数保留以兼容调用方签名（策略开关已废除），内容不再生效。
    """
    review = UnitReview(unit_id=unit.id)
    if unit.status == UnitStatus.DONE.value:
        review.group = GROUP_SIDE
        review.side_kind = "done"
        return review
    if unit.status == UnitStatus.CHILD.value:
        review.group = GROUP_SIDE
        review.side_kind = "child"
        review.reminders.append("随父文件夹整体处理，不单独执行")
        return review
    if unit.status == UnitStatus.MISSING.value:
        # 唯一拦截：来源失联 → SKIPPED_ERROR（跳过并报告，不阻断整体）
        review.group = GROUP_NEEDS
        review.organizable = False
        review.blockers.append("来源已失联（文件被移动、重命名或删除），本次跳过")
        return review

    if unit.is_album:
        # 边界一律自动采用；缺整理艺术家/缺专辑名只提醒
        if not (unit.album_artist or "").strip():
            review.reminders.append("整理艺术家未指定，将放入“未知艺术家”")
        if not (unit.album or "").strip():
            review.reminders.append("未识别到专辑名，目标目录将使用“未分类专辑”")
        if unit.status == UnitStatus.INCOMPLETE.value:
            review.reminders.append("CUE 引用文件缺失（可能不完整），仍按整夹自动整理")
    elif unit.is_track:
        if not (unit.title or "").strip():
            review.reminders.append("缺少歌曲名，将使用“未命名歌曲”兜底")
        if not (unit.artist or "").strip():
            review.reminders.append("缺少歌曲艺术家，目录归属将退回专辑艺术家或“未知艺术家”")
        if not (unit.album or "").strip():
            review.reminders.append("未能识别专辑，将放入“未分类专辑”目录")
    elif unit.is_archive:
        mode = (unit.archive or {}).get("mode", "undecided")
        if mode == "archive_only":
            review.reminders.append("原包按规则归档到“原始压缩包”目录")
        else:
            review.reminders.append("压缩包内容不拆解：原包随规则自动归档")

    # 重复：确定/疑似一律保留各份并提醒（不再要求确认保留项）
    if unit.duplicate_group:
        if unit.dup_role == "duplicate":
            review.reminders.append("已标记为重复项：默认保留，与其它副本一同整理")
        elif unit.dup_role == "suspect":
            review.reminders.append("疑似重复：保留各份继续整理")
        elif unit.dup_role == "keep":
            review.reminders.append("已选为本组保留项")
    if unit.dup_role != "duplicate":
        pass  # 保留项选择接口已移除；重复项全部按普通单元流转

    review.organizable = not review.blockers
    review.group = GROUP_READY if review.organizable else GROUP_NEEDS
    return review


def group_of(unit: Unit, policy=None) -> str:
    return evaluate_unit(unit, policy).group


def summarize(units: list[Unit], policy=None) -> dict:
    """分组计数：主列表与整理中心共用。"""
    result = {
        "ready": 0, "ready_checked": 0,
        "needs": 0, "needs_checked": 0,
        "side": 0,
    }
    for unit in units:
        review = evaluate_unit(unit, policy)
        if review.group == GROUP_SIDE:
            result["side"] += 1
            continue
        result[review.group] += 1
        if unit.checked:
            result[f"{review.group}_checked"] += 1
    return result
