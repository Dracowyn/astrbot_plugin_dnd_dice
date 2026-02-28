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

# 模块级 SystemRandom 实例，提供更高质量的随机数。
# SystemRandom 使用操作系统 CSPRNG（如 Linux 的 /dev/urandom），
# 而非确定性的梅森旋转算法，确保掷骰结果在实际使用中不可预测。
_rng = random.SystemRandom()


@dataclass
class DieRoll:
    """单枚骰子的带状态注解结果。"""

    value: int
    state: str  # 骰子状态："kept"（保留）| "dropped"（丢弃）| "rerolled"（重骰）| "exploded"（爆炸追加）


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
    # 每颗骰子的带状态注解结果（由执行引擎填充，供格式化器精确展示）。
    # 列表按投掷时间顺序排列，包含被重骰替换的原始值、最终保留/丢弃值
    # 以及爆炸追加骰。格式化器优先使用此列表，而非基于频率计数的旧路径。
    die_rolls: list[DieRoll] = field(default_factory=list)

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
        """
        单枚 d20 掷出 20 时为 True。

        仅在表达式符合单次 d20 检定形态时生效：整个表达式有且仅有一组骰子、
        该组为 d20（含优势/劣势）且不处于成功计数模式。复合表达式（如 2d6+1d20）不触发。
        """
        if len(self.group_results) != 1:
            return False
        r = self.group_results[0]
        if r.is_success_mode:
            return False
        g = r.group
        if g.sides == 20 and g.keep_mode is None and g.count == 1:
            return bool(r.kept_rolls and r.kept_rolls[0] == 20)
        # 优势/劣势：2d20kh1 / 2d20kl1
        if (
            g.sides == 20
            and g.keep_mode in ("kh", "kl")
            and g.count == 2
            and g.keep_n == 1
        ):
            return bool(r.kept_rolls and r.kept_rolls[0] == 20)
        return False

    @property
    def is_natural_1(self) -> bool:
        """
        单枚 d20 掷出 1 时为 True，含优势（kh）和劣势（kl）场景。

        仅在表达式符合单次 d20 检定形态时生效（同 is_natural_20）。
        """
        if len(self.group_results) != 1:
            return False
        r = self.group_results[0]
        if r.is_success_mode:
            return False
        g = r.group
        if g.sides == 20 and g.keep_mode is None and g.count == 1:
            return bool(r.kept_rolls and r.kept_rolls[0] == 1)
        # 优势（kh）和劣势（kl）——保留骰为 1 均触发"大失败"
        if (
            g.sides == 20
            and g.keep_mode in ("kh", "kl")
            and g.count == 2
            and g.keep_n == 1
        ):
            return bool(r.kept_rolls and r.kept_rolls[0] == 1)
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
    return _rng.randint(1, sides)


def _roll_fate() -> int:
    return _rng.choice(_FATE_VALUES)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 爆炸骰三种模式
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 重骰辅助
# ---------------------------------------------------------------------------


def _apply_rerolls(
    raw_rolls: list[int],
    sides: int,
    conditions: list[RerollCondition],
    max_depth: int,
    fate: bool,
) -> tuple[list[int], list[int], list[list[tuple[int, str]]]]:
    """
    对 raw_rolls 中满足条件的骰子执行重骰。

    返回 (final_rolls, rerolled_originals, per_slot_histories)。
      - final_rolls: 重骰后的最终值列表
      - rerolled_originals: 被替换掉的原始值（向后兼容用）
      - per_slot_histories: 每个位置的骰骰历史记录。
        历史记录是 (value, state) 元组的列表，其中 state 为
        "rerolled"（被替换的原始值）或 "kept"（最终保留值，
        keep/drop 阶段可能进一步改为 "dropped"）。
    """
    final: list[int] = []
    rerolled: list[int] = []
    histories: list[list[tuple[int, str]]] = []

    for val in raw_rolls:
        original = val
        history: list[tuple[int, str]] = []

        for cond in conditions:
            if _compare(val, cond.compare, cond.value):
                rerolled.append(original)
                history.append((original, "rerolled"))
                if cond.once:
                    val = _roll_fate() if fate else _roll_single(sides)
                else:
                    depth = 0
                    while _compare(val, cond.compare, cond.value) and depth < max_depth:
                        val = _roll_fate() if fate else _roll_single(sides)
                        depth += 1
                break  # 一个骰值只触发第一个匹配的条件

        history.append(
            (val, "kept")
        )  # 状态在后续 keep/drop 阶段可能进一步改为 "dropped"
        final.append(val)
        histories.append(history)

    return final, rerolled, histories


# ---------------------------------------------------------------------------
# 核心掷骰逻辑（拆分为若干独立辅助函数，_roll_group 负责编排）
# ---------------------------------------------------------------------------


def _roll_base_dice(
    group: DiceGroup,
    sides: int,
    exploding_depth: int,
) -> list[int]:
    """掷基础骰（不含重骰/keep/drop 等后处理）。"""
    if group.fate:
        return [_roll_fate() for _ in range(group.count)]
    if group.exploding and group.explode_mode == "compound":
        return [
            _roll_compound_exploding(sides, group, exploding_depth)
            for _ in range(group.count)
        ]
    return [_roll_single(sides) for _ in range(group.count)]


def _explode_after_reroll(
    group: DiceGroup,
    sides: int,
    raw_rolls: list[int],
    exploding_depth: int,
) -> list[int]:
    """在重骰之后应用标准/穿透爆炸，返回追加骰列表（复合爆炸和 FATE 骰跳过）。"""
    if not group.exploding or group.fate or group.explode_mode == "compound":
        return []
    exploded_extra: list[int] = []
    if group.explode_mode == "penetrate":
        for v in raw_rolls:
            depth = 0
            curr = v
            while _should_explode(curr, sides, group) and depth < exploding_depth:
                depth += 1
                curr = max(1, _roll_single(sides) - 1)
                exploded_extra.append(curr)
    else:  # 标准爆炸模式
        for v in raw_rolls:
            depth = 0
            curr = v
            while _should_explode(curr, sides, group) and depth < exploding_depth:
                depth += 1
                curr = _roll_single(sides)
                exploded_extra.append(curr)
    return exploded_extra


def _apply_keep_drop_indexed(
    raw_rolls: list[int],
    exploded_extra: list[int],
    group: DiceGroup,
    effective_keep_mode: str | None,
    effective_keep_n: int | None,
) -> tuple[list[int], list[int], set[int]]:
    """
    基于位置索引的 keep / drop 过滤。

    返回 (kept_vals, dropped_vals, dropped_positions)。
    dropped_positions 是合并池（raw_rolls + exploded_extra）中被丢弃的位置集合，
    使 _build_die_rolls 能精确标注每颢骰子的状态，包括被丢弃的爆炸追加骰
    （这些应标注 state="dropped"，而非 state="exploded"）。
    """
    if not effective_keep_mode or effective_keep_n is None:
        return list(raw_rolls) + list(exploded_extra), [], set()

    # 爆炸骰 + keep：将追加骰纳入竞争池
    pool = (
        raw_rolls + exploded_extra
        if group.exploding and exploded_extra and group.explode_mode != "compound"
        else raw_rolls
    )

    indexed = list(enumerate(pool))
    sorted_desc = sorted(indexed, key=lambda x: x[1], reverse=True)
    kn = max(0, min(effective_keep_n, len(pool)))

    if effective_keep_mode == "kh":
        kept_indices = {idx for idx, _ in sorted_desc[:kn]}
    else:  # kl（保留最低）
        kept_indices = {idx for idx, _ in sorted_desc[len(pool) - kn :]}

    # 跟踪完整池（基础骰 + 爆炸追加骰）中所有被丢弃的位置索引，
    # 以便 _build_die_rolls 将被丢弃的爆炸追加骰正确标注为 "dropped"
    # 而非 "exploded"。
    dropped_positions = {i for i in range(len(pool)) if i not in kept_indices}

    kept_vals = [pool[i] for i in range(len(pool)) if i in kept_indices]
    dropped_vals = [pool[i] for i in range(len(pool)) if i not in kept_indices]
    return kept_vals, dropped_vals, dropped_positions


def _count_successes(
    kept_vals: list[int],
    group: DiceGroup,
) -> tuple[int | None, int | None]:
    """计算 kept_vals 中的成功数和失败数（均为 None 表示非成功计数模式）。"""
    if group.success_compare is None or group.success_value is None:
        return None, None
    successes = sum(
        1 for v in kept_vals if _compare(v, group.success_compare, group.success_value)
    )
    failures: int | None = None
    if group.failure_compare is not None and group.failure_value is not None:
        failures = sum(
            1
            for v in kept_vals
            if _compare(v, group.failure_compare, group.failure_value)
        )
    return successes, failures


def _build_die_rolls(
    raw_rolls: list[int],
    exploded_extra: list[int],
    per_slot_histories: list[list[tuple[int, str]]],
    dropped_positions: set[int],
) -> list[DieRoll]:
    """
    将每颗骰子的历史记录和最终状态合并为有序 DieRoll 列表。

    列表顺序与投掷顺序一致，被重骰的原始值以 state="rerolled" 出现在
    其替代值之前，被丢弃的最终值以 state="dropped" 标注，爆炸追加骰
    以 state="exploded" 追加在末尾。

    dropped_positions 包含合并池（raw_rolls + exploded_extra）中所有被丢弃位置的索引。
    爆炸追加骰在池中的索引为 len(raw_rolls) + j。
    """
    die_rolls: list[DieRoll] = []
    for i, history in enumerate(per_slot_histories):
        # 历史中除最后一项外均为被替换的原始值（state="rerolled"）
        for val, state in history[:-1]:
            die_rolls.append(DieRoll(value=val, state=state))
        # 最后一项是最终值；keep/drop 阶段决定其最终状态
        final_val, _ = history[-1]
        final_state = "dropped" if i in dropped_positions else "kept"
        die_rolls.append(DieRoll(value=final_val, state=final_state))
    for j, val in enumerate(exploded_extra):
        # 爆炸追加骰也可能被 keep/drop 规则淘汰；
        # 其在合并池中的索引为 len(raw_rolls) + j。
        pool_idx = len(raw_rolls) + j
        state = "dropped" if pool_idx in dropped_positions else "exploded"
        die_rolls.append(DieRoll(value=val, state=state))
    return die_rolls


def _roll_group(
    group: DiceGroup,
    max_dice: int,
    max_sides: int,
    exploding_depth: int,
    reroll_max_depth: int = 20,
) -> DiceGroupResult:
    """掷一组骰子并返回结果（编排各辅助函数）。"""
    if group.count > max_dice:
        raise DiceRollError(f"骰子数量 {group.count} 超过最大限制 {max_dice}")
    if not group.fate and group.sides > max_sides:
        raise DiceRollError(f"骰子面数 {group.sides} 超过最大限制 {max_sides}")
    if not group.fate and group.sides < 1:
        raise DiceRollError(f"骰子面数必须至少为 1，得到 {group.sides}")
    if group.count < 1:
        raise DiceRollError(f"骰子数量必须至少为 1，得到 {group.count}")

    negated = group.modifier == -1
    sides = group.sides

    # --- 1. 基础掷骰 ---
    raw_rolls = _roll_base_dice(group, sides, exploding_depth)

    # --- 2. 重骰（在爆炸之前应用）---
    rerolled_originals: list[int] = []
    per_slot_histories: list[list[tuple[int, str]]] = [[(v, "kept")] for v in raw_rolls]
    if group.reroll_conditions:
        effective_sides = sides if not group.fate else 0
        raw_rolls, rerolled_originals, per_slot_histories = _apply_rerolls(
            raw_rolls,
            effective_sides,
            group.reroll_conditions,
            reroll_max_depth,
            group.fate,
        )

    # --- 3. 爆炸（重骰完成后应用；复合爆炸和 FATE 骰跳过）---
    exploded_extra = _explode_after_reroll(group, sides, raw_rolls, exploding_depth)

    # 对爆炸追加骰也应用重骰规则（Roll20 规范），追加骰历史不纳入主体
    if group.reroll_conditions and exploded_extra:
        extra_effective_sides = sides if not group.fate else 0
        exploded_extra, extra_rerolled, _ = _apply_rerolls(
            exploded_extra,
            extra_effective_sides,
            group.reroll_conditions,
            reroll_max_depth,
            group.fate,
        )
        rerolled_originals.extend(extra_rerolled)

    all_rolls = raw_rolls + exploded_extra

    # --- 4. Keep / Drop（转换 drop → keep 后统一走索引路径）---
    effective_keep_mode = group.keep_mode
    effective_keep_n = group.keep_n
    if group.drop_mode is not None and group.drop_n is not None:
        # 计算实际竞争池大小：当爆炸追加骰被纳入 keep/drop 池时
        # （与 _apply_keep_drop_indexed 中的条件相同），drop_n 须相对于
        # 完整池进行截断和减法，而非仅基于 len(raw_rolls)。
        pool_size = len(raw_rolls)
        if group.exploding and exploded_extra and group.explode_mode != "compound":
            pool_size += len(exploded_extra)
        dn = min(group.drop_n, pool_size)
        if group.drop_mode == "dl":
            effective_keep_mode = "kh"
            effective_keep_n = pool_size - dn
        else:  # dh（丢弃最高）
            effective_keep_mode = "kl"
            effective_keep_n = pool_size - dn

    kept_vals, dropped_vals, dropped_positions = _apply_keep_drop_indexed(
        raw_rolls, exploded_extra, group, effective_keep_mode, effective_keep_n
    )

    # --- 5. 排序（仅影响展示顺序）---
    if group.sort_order == "asc":
        kept_vals = sorted(kept_vals)
    elif group.sort_order == "desc":
        kept_vals = sorted(kept_vals, reverse=True)

    if group.sort_order is not None and not exploded_extra:
        dropped_sorted = sorted(dropped_vals, reverse=(group.sort_order == "desc"))
        all_rolls = sorted(
            kept_vals + dropped_sorted, reverse=(group.sort_order == "desc")
        )

    # --- 6. 成功/失败计数 ---
    successes, failures = _count_successes(kept_vals, group)

    # --- 7. 构建带状态的 DieRoll 列表（供 formatter 精确标注每颗骰子）---
    die_rolls = _build_die_rolls(
        raw_rolls, exploded_extra, per_slot_histories, dropped_positions
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
        die_rolls=die_rolls,
    )


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def roll(
    expr: ParsedExpression,
    max_dice: int = 100,
    max_sides: int = 1000,
    exploding_depth: int = 20,
    reroll_max_depth: int = 20,
) -> RollResult:
    """
    执行 ParsedExpression 并返回 RollResult。

    任何骰子组违反配置限制时抛出 DiceRollError。
    """
    # 全局预算检查：单组限制无法防止多组合计超出 max_dice。
    total_base_dice = sum(g.count for g in expr.groups)
    if total_base_dice > max_dice:
        raise DiceRollError(
            f"骰子数量 {total_base_dice} 超过单次掷骰限制 {max_dice}（跨所有骰子组合计）"
        )
    result = RollResult(expression=expr)
    for group in expr.groups:
        gr = _roll_group(group, max_dice, max_sides, exploding_depth, reroll_max_depth)
        result.group_results.append(gr)
    return result
