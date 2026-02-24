"""
dice_roller.py — DnD 骰子执行引擎。

接受 ParsedExpression，返回包含每个骰子完整明细的 RollResult。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .dice_parser import DiceGroup, ParsedExpression

# ---------------------------------------------------------------------------
# 结果数据类
# ---------------------------------------------------------------------------


@dataclass
class DiceGroupResult:
    """单组骰子的掷骰结果。"""

    group: DiceGroup  # 原始骰池规格
    all_rolls: list[int] = field(default_factory=list)  # 所有骰出的值（含爆炸追加）
    kept_rolls: list[int] = field(default_factory=list)  # 计入小计的骰子
    dropped_rolls: list[int] = field(default_factory=list)  # 被丢弃的骰子（kh/kl）
    exploded_extra: list[int] = field(default_factory=list)  # 爆炸触发的额外骰子
    negated: bool = False  # 该组前缀为 '-' 时为 True

    @property
    def subtotal(self) -> int:
        """保留骰之和，若为负号组则取反。"""
        total = sum(self.kept_rolls)
        return -total if self.negated else total


@dataclass
class RollResult:
    """整条 ParsedExpression 的完整掷骰结果。"""

    expression: ParsedExpression  # 原始解析表达式
    group_results: list[DiceGroupResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            sum(r.subtotal for r in self.group_results) + self.expression.flat_modifier
        )

    @property
    def label(self) -> str:
        return self.expression.label

    @property
    def is_natural_20(self) -> bool:
        """单枚 d20（非优势/劣势）掷出 20 时为 True。"""
        for r in self.group_results:
            g = r.group
            if g.sides == 20 and g.keep_mode is None and g.count == 1:
                if r.kept_rolls and r.kept_rolls[0] == 20:
                    return True
            # 优势/劣势时，保留值为 20 也算天然 20
            if (
                g.sides == 20
                and g.keep_mode in ("kh", "kl")
                and g.count == 2
                and g.keep_n == 1
            ):
                if r.kept_rolls and r.kept_rolls[0] == 20:
                    return True
        return False

    @property
    def is_natural_1(self) -> bool:
        """单枚 d20（非劣势）掷出 1 时为 True。"""
        for r in self.group_results:
            g = r.group
            if g.sides == 20 and g.keep_mode is None and g.count == 1:
                if r.kept_rolls and r.kept_rolls[0] == 1:
                    return True
            if g.sides == 20 and g.keep_mode == "kh" and g.count == 2 and g.keep_n == 1:
                if r.kept_rolls and r.kept_rolls[0] == 1:
                    return True
        return False


# ---------------------------------------------------------------------------
# 配置感知验证器
# ---------------------------------------------------------------------------


class DiceRollError(ValueError):
    """因超出配置限制而拒绝掷骰时抛出。"""


# ---------------------------------------------------------------------------
# 核心掷骰逻辑
# ---------------------------------------------------------------------------


def _roll_single(sides: int) -> int:
    return random.randint(1, sides)


def _roll_exploding(sides: int, max_depth: int) -> list[int]:
    """
    掷一枚爆炸骰。返回所有骰出的值（含触发链）。
    爆炸次数超过 max_depth 后停止。
    """
    results: list[int] = []
    depth = 0
    while True:
        val = _roll_single(sides)
        results.append(val)
        if val == sides and depth < max_depth:
            depth += 1
        else:
            break
    return results


def _roll_group(
    group: DiceGroup, max_dice: int, max_sides: int, exploding_depth: int
) -> DiceGroupResult:
    """掷一组骰子并返回结果。"""
    if group.count > max_dice:
        raise DiceRollError(f"骰子数量 {group.count} 超过最大限制 {max_dice}")
    if group.sides > max_sides:
        raise DiceRollError(f"骰子面数 {group.sides} 超过最大限制 {max_sides}")
    if group.sides < 1:
        raise DiceRollError(f"骰子面数必须至少为 1，得到 {group.sides}")
    if group.count < 1:
        raise DiceRollError(f"骰子数量必须至少为 1，得到 {group.count}")

    negated = group.modifier == -1
    exploded_extra: list[int] = []
    raw_rolls: list[int] = []

    if group.exploding:
        for _ in range(group.count):
            chain = _roll_exploding(group.sides, exploding_depth)
            raw_rolls.append(chain[0])
            exploded_extra.extend(chain[1:])
        all_rolls = raw_rolls + exploded_extra
    else:
        raw_rolls = [_roll_single(group.sides) for _ in range(group.count)]
        all_rolls = list(raw_rolls)

    # Apply keep-highest / keep-lowest.
    if group.keep_mode and group.keep_n is not None:
        kn = min(group.keep_n, len(raw_rolls))
        sorted_desc = sorted(raw_rolls, reverse=True)
        if group.keep_mode == "kh":
            kept = sorted(sorted_desc[:kn])
            dropped = sorted(sorted_desc[kn:])
        else:  # kl
            kept = sorted(sorted_desc[len(sorted_desc) - kn :])
            dropped = sorted(sorted_desc[: len(sorted_desc) - kn])

        # 爆炸骰 + kh/kl 时：将爆炸追加骰纳入保留池重新排序。
        if group.exploding and exploded_extra:
            combined = sorted(raw_rolls + exploded_extra, reverse=True)
            if group.keep_mode == "kh":
                kept = combined[:kn]
                dropped = combined[kn:]
            else:
                kept = combined[len(combined) - kn :]
                dropped = combined[: len(combined) - kn]
    else:
        # 无保留过滤器；所有骰子（含爆炸追加）全部加总。
        kept = list(all_rolls)
        dropped = []

    return DiceGroupResult(
        group=group,
        all_rolls=all_rolls,
        kept_rolls=kept,
        dropped_rolls=dropped,
        exploded_extra=exploded_extra,
        negated=negated,
    )


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def roll(
    expr: ParsedExpression,
    max_dice: int = 100,
    max_sides: int = 1000,
    exploding_depth: int = 20,
) -> RollResult:
    """
    执行 ParsedExpression 并返回 RollResult。

    任何骰子组违反配置限制时抛出 DiceRollError。
    """
    result = RollResult(expression=expr)
    for group in expr.groups:
        gr = _roll_group(group, max_dice, max_sides, exploding_depth)
        result.group_results.append(gr)
    return result
