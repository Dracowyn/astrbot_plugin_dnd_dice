"""
dice_roller.py — DnD 骰子执行引擎。

接受 ParsedExpression，返回包含每个骰子完整明细的 RollResult。
支持：基础骰、FATE 骰、keep/drop、爆炸（standard/compound/penetrate/自定义阈值）、
      目标数成功/失败计数、重骰（r/ro）、排序。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .dice_parser import DiceGroup, ParsedExpression, RerollCondition

# ---------------------------------------------------------------------------
# 结果数据类
# ---------------------------------------------------------------------------

_FATE_VALUES = (-1, 0, 1)  # FATE 骰面


@dataclass
class DiceGroupResult:
    """单组骰子的掷骰结果。"""

    group: DiceGroup  # 原始骰池规格
    all_rolls: list[int] = field(default_factory=list)  # 所有骰出的值（含爆炸追加）
    kept_rolls: list[int] = field(default_factory=list)  # 计入小计的骰子
    dropped_rolls: list[int] = field(default_factory=list)  # 被丢弃的骰子
    exploded_extra: list[int] = field(default_factory=list)  # 爆炸触发的额外骰子
    rerolled_originals: list[int] = field(default_factory=list)  # 被重骰替换的原始值
    negated: bool = False  # 该组前缀为 '-' 时为 True
    successes: int | None = None  # 目标数成功计数（None = 非计数模式）
    failures: int | None = None  # 失败计数（None = 未启用）

    @property
    def is_success_mode(self) -> bool:
        return self.successes is not None

    @property
    def subtotal(self) -> int:
        """
        保留骰之和（或成功数），若为负号组则取反。
        成功计数模式下返回 successes - failures。
        """
        if self.is_success_mode:
            s = self.successes or 0
            f = self.failures or 0
            result = s - f
        else:
            result = sum(self.kept_rolls)
        return -result if self.negated else result


@dataclass
class RollResult:
    """整条 ParsedExpression 的完整掷骰结果。"""

    expression: ParsedExpression  # 原始解析表达式
    group_results: list[DiceGroupResult] = field(default_factory=list)

    @property
    def is_success_mode(self) -> bool:
        """任意组处于成功计数模式即视为整体成功计数模式。"""
        return any(r.is_success_mode for r in self.group_results)

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
# 异常
# ---------------------------------------------------------------------------


class DiceRollError(ValueError):
    """因超出配置限制而拒绝掷骰时抛出。"""


# ---------------------------------------------------------------------------
# 辅助：比较点判断
# ---------------------------------------------------------------------------


def _compare(value: int, op: str, threshold: int) -> bool:
    """计算 value <op> threshold，其中 op 为 '>' / '<' / '='，'>='/'<=' 用 '>'/'<' 表示。"""
    if op == ">":
        return value >= threshold
    if op == "<":
        return value <= threshold
    return value == threshold


def _should_explode(value: int, sides: int, group: DiceGroup) -> bool:
    """判断 value 是否触发爆炸。"""
    if group.explode_compare is not None and group.explode_value is not None:
        return _compare(value, group.explode_compare, group.explode_value)
    # 默认：等于最大值才爆炸
    return value == sides


# ---------------------------------------------------------------------------
# 辅助：单骰掷出
# ---------------------------------------------------------------------------


def _roll_single(sides: int) -> int:
    return random.randint(1, sides)


def _roll_fate() -> int:
    return random.choice(_FATE_VALUES)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 爆炸骰三种模式
# ---------------------------------------------------------------------------


def _roll_standard_exploding(sides: int, group: DiceGroup, max_depth: int) -> list[int]:
    """标准爆炸：每个骰子独立爆炸，返回该骰子的所有值（首值 + 爆炸追加值）。"""
    results: list[int] = []
    depth = 0
    val = _roll_single(sides)
    results.append(val)
    while _should_explode(val, sides, group) and depth < max_depth:
        depth += 1
        val = _roll_single(sides)
        results.append(val)
    return results


def _roll_compound_exploding(sides: int, group: DiceGroup, max_depth: int) -> int:
    """复合爆炸（Shadowrun 风格）：所有爆炸值叠加为单个结果，返回合并总值。"""
    total = 0
    depth = 0
    val = _roll_single(sides)
    total += val
    while _should_explode(val, sides, group) and depth < max_depth:
        depth += 1
        val = _roll_single(sides)
        total += val
    return total


def _roll_penetrating_exploding(
    sides: int, group: DiceGroup, max_depth: int
) -> list[int]:
    """穿透爆炸（HackMaster 风格）：每次追加骰减 1，返回所有骰值（含追加）。"""
    results: list[int] = []
    depth = 0
    val = _roll_single(sides)
    results.append(val)
    while _should_explode(val, sides, group) and depth < max_depth:
        depth += 1
        val = max(1, _roll_single(sides) - 1)
        results.append(val)
    return results


# ---------------------------------------------------------------------------
# 重骰辅助
# ---------------------------------------------------------------------------


def _apply_rerolls(
    raw_rolls: list[int],
    sides: int,
    conditions: list[RerollCondition],
    max_depth: int,
    fate: bool,
) -> tuple[list[int], list[int]]:
    """
    对 raw_rolls 中满足条件的骰子执行重骰。

    返回 (final_rolls, rerolled_originals)。
    rerolled_originals 记录被替换掉的原始值（展示用）。
    """
    final: list[int] = []
    rerolled: list[int] = []

    for val in raw_rolls:
        original = val
        replaced = False

        for cond in conditions:
            if _compare(val, cond.compare, cond.value):
                replaced = True
                rerolled.append(original)
                if cond.once:
                    # ro：只重骰一次
                    val = _roll_fate() if fate else _roll_single(sides)
                else:
                    # r：循环重骰直至不满足条件（或超深度）
                    depth = 0
                    while _compare(val, cond.compare, cond.value) and depth < max_depth:
                        val = _roll_fate() if fate else _roll_single(sides)
                        depth += 1
                break  # 一个骰值只触发第一个匹配的条件

        if replaced and original not in rerolled:
            rerolled.append(original)
        final.append(val)

    return final, rerolled


# ---------------------------------------------------------------------------
# 核心掷骰逻辑
# ---------------------------------------------------------------------------


def _roll_group(
    group: DiceGroup, max_dice: int, max_sides: int, exploding_depth: int
) -> DiceGroupResult:
    """掷一组骰子并返回结果。"""
    if group.count > max_dice:
        raise DiceRollError(f"骰子数量 {group.count} 超过最大限制 {max_dice}")
    if not group.fate and group.sides > max_sides:
        raise DiceRollError(f"骰子面数 {group.sides} 超过最大限制 {max_sides}")
    if not group.fate and group.sides < 1:
        raise DiceRollError(f"骰子面数必须至少为 1，得到 {group.sides}")
    if group.count < 1:
        raise DiceRollError(f"骰子数量必须至少为 1，得到 {group.count}")

    negated = group.modifier == -1
    exploded_extra: list[int] = []
    raw_rolls: list[int] = []
    sides = group.sides

    # --- 1. 基础掷骰 ---
    if group.fate:
        # FATE/Fudge 骰：三面 -1/0/1
        raw_rolls = [_roll_fate() for _ in range(group.count)]
        all_rolls = list(raw_rolls)
    elif group.exploding:
        if group.explode_mode == "compound":
            # 复合爆炸：每骰返回合并值（单个整数）
            raw_rolls = [
                _roll_compound_exploding(sides, group, exploding_depth)
                for _ in range(group.count)
            ]
            all_rolls = list(raw_rolls)
        elif group.explode_mode == "penetrate":
            # 穿透爆炸：返回骰子链
            for _ in range(group.count):
                chain = _roll_penetrating_exploding(sides, group, exploding_depth)
                raw_rolls.append(chain[0])
                exploded_extra.extend(chain[1:])
            all_rolls = raw_rolls + exploded_extra
        else:
            # 标准爆炸
            for _ in range(group.count):
                chain = _roll_standard_exploding(sides, group, exploding_depth)
                raw_rolls.append(chain[0])
                exploded_extra.extend(chain[1:])
            all_rolls = raw_rolls + exploded_extra
    else:
        raw_rolls = [_roll_single(sides) for _ in range(group.count)]
        all_rolls = list(raw_rolls)

    # --- 2. 重骰 ---
    rerolled_originals: list[int] = []
    if group.reroll_conditions:
        # 重骰只作用于初始骰（raw_rolls），不含爆炸追加骰
        effective_sides = sides if not group.fate else 0
        raw_rolls, rerolled_originals = _apply_rerolls(
            raw_rolls,
            effective_sides,
            group.reroll_conditions,
            exploding_depth,
            group.fate,
        )
        all_rolls = raw_rolls + exploded_extra

    # --- 3. Keep / Drop 过滤 ---
    # 先把 drop 路径转换为等价的 keep 路径再统一处理
    effective_keep_mode = group.keep_mode
    effective_keep_n = group.keep_n

    if group.drop_mode is not None and group.drop_n is not None:
        # dl（丢弃最低）→ 等价于 kh(count - drop_n)
        # dh（丢弃最高）→ 等价于 kl(count - drop_n)
        dn = min(group.drop_n, len(raw_rolls))
        if group.drop_mode == "dl":
            effective_keep_mode = "kh"
            effective_keep_n = len(raw_rolls) - dn
        else:  # dh
            effective_keep_mode = "kl"
            effective_keep_n = len(raw_rolls) - dn

    if effective_keep_mode and effective_keep_n is not None:
        kn = max(0, min(effective_keep_n, len(raw_rolls)))
        sorted_desc = sorted(raw_rolls, reverse=True)
        if effective_keep_mode == "kh":
            kept_vals = sorted_desc[:kn]
            dropped_vals = sorted_desc[kn:]
        else:  # kl
            kept_vals = sorted_desc[len(sorted_desc) - kn :]
            dropped_vals = sorted_desc[: len(sorted_desc) - kn]

        # 爆炸骰 + keep 时：追加骰也纳入排序池
        if group.exploding and exploded_extra and group.explode_mode != "compound":
            combined = sorted(raw_rolls + exploded_extra, reverse=True)
            if effective_keep_mode == "kh":
                kept_vals = combined[:kn]
                dropped_vals = combined[kn:]
            else:
                kept_vals = combined[len(combined) - kn :]
                dropped_vals = combined[: len(combined) - kn]
    else:
        # 无 keep/drop 过滤：全部骰子计入
        kept_vals = list(all_rolls)
        dropped_vals = []

    # --- 4. 排序（仅影响展示顺序）---
    if group.sort_order == "asc":
        kept_vals = sorted(kept_vals)
    elif group.sort_order == "desc":
        kept_vals = sorted(kept_vals, reverse=True)

    # Rebuild all_rolls so the formatter displays dice in the correct sorted order.
    # Only applies when there are no exploded extras (to avoid double-counting).
    if group.sort_order is not None and not exploded_extra:
        dropped_sorted = sorted(dropped_vals, reverse=(group.sort_order == "desc"))
        all_rolls = sorted(
            kept_vals + dropped_sorted, reverse=(group.sort_order == "desc")
        )

    # --- 5. 成功/失败计数 ---
    successes: int | None = None
    failures: int | None = None
    if group.success_compare is not None and group.success_value is not None:
        successes = sum(
            1
            for v in kept_vals
            if _compare(v, group.success_compare, group.success_value)
        )
        if group.failure_compare is not None and group.failure_value is not None:
            failures = sum(
                1
                for v in kept_vals
                if _compare(v, group.failure_compare, group.failure_value)
            )

    return DiceGroupResult(
        group=group,
        all_rolls=all_rolls,
        kept_rolls=kept_vals,
        dropped_rolls=dropped_vals,
        exploded_extra=exploded_extra,
        rerolled_originals=rerolled_originals,
        negated=negated,
        successes=successes,
        failures=failures,
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
