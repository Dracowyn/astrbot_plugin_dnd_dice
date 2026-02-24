"""
formatter.py — DnD 骰点结果的纯文本格式化器。

输出规则：
  - 不使用任何 Markdown 语法（无 **、~~、>、-、# 等）
  - 不使用 emoji
  - 被丢弃的骰子用括号标注，如 (1)
  - 爆炸追加骰附加 "!" 后缀，如 6!
  - 天然 20 / 天然 1 在结果行末注释
  - show_detail=False 时仅显示：<表达式> = <总计>
"""

from __future__ import annotations

from .dice_roller import DiceGroupResult, RollResult

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _group_label(gr: DiceGroupResult) -> str:
    """为单组骰子构建可读标签，如 '4d6kh3' 或 'd20adv'。"""
    g = gr.group
    # 重建近似原始表达式记号
    count_str = str(g.count) if g.count != 1 else ""
    base = f"{count_str}d{g.sides}"

    if g.keep_mode == "kh" and g.keep_n == 1 and g.count == 2:
        suffix = "adv"
    elif g.keep_mode == "kl" and g.keep_n == 1 and g.count == 2:
        suffix = "dis"
    elif g.keep_mode == "kh" and g.keep_n is not None:
        suffix = f"kh{g.keep_n}"
    elif g.keep_mode == "kl" and g.keep_n is not None:
        suffix = f"kl{g.keep_n}"
    else:
        suffix = ""

    explode_mark = "!" if g.exploding else ""
    sign = "-" if gr.negated else ""
    return f"{sign}{base}{suffix}{explode_mark}"


def _format_dice_list(gr: DiceGroupResult) -> str:
    """
    将单组骰子的各个骰值格式化为括号列表。

    被丢弃的骰子用圆括号包裹。
    爆炸追加骰（超出初始数量的部分）附加 '!' 后缀。
    """
    g = gr.group

    # 将每个原始骰值归因为"丢弃"或"保留"，尽量保持原始顺序并正确处理重复值。

    display_parts: list[str] = []

    # 爆炸骰组的 all_rolls 列表：基础骰在前，爆炸追加骰在后。
    # 对爆炸追加骰加 '!' 注释。
    base_count = g.count
    base_rolls = gr.all_rolls[:base_count]
    extra_rolls = gr.all_rolls[base_count:]  # 爆炸追加骰

    # 用可变计数映射追踪丢弃值的归因。
    dropped_remaining: dict[int, int] = {}
    for v in gr.dropped_rolls:
        dropped_remaining[v] = dropped_remaining.get(v, 0) + 1

    for val in base_rolls:
        if dropped_remaining.get(val, 0) > 0:
            dropped_remaining[val] -= 1
            display_parts.append(f"({val})")
        else:
            display_parts.append(str(val))

    for val in extra_rolls:
        display_parts.append(f"{val}!")

    return "[" + ", ".join(display_parts) + "]"


def _rebuild_expr(result: RollResult) -> str:
    """重建用于显示的紧凑表达式字符串。"""
    parts: list[str] = []
    for i, gr in enumerate(result.group_results):
        label = _group_label(gr)
        if i == 0 and not gr.negated:
            parts.append(label.lstrip("+"))
        elif gr.negated:
            parts.append(label)  # _group_label 已带 '-' 前缀
        else:
            parts.append("+" + label)

    mod = result.expression.flat_modifier
    if mod > 0:
        parts.append(f"+{mod}")
    elif mod < 0:
        parts.append(str(mod))

    return "".join(parts)


# ---------------------------------------------------------------------------
# 公开格式化器
# ---------------------------------------------------------------------------


def format_result(result: RollResult, show_detail: bool = True) -> str:
    """
    将 RollResult 格式化为单行纯文本字符串。

    show_detail=True  → {标签} {表达式}: {骰值列表} = {总计}
      示例：技能名 2d20: [18, 13] = 31
            攻击检定 1d20+5: [17] +5 = 22
            力量 4d6kh3+2: [(1), 3, 5, 6] +2 = 16
            伤害 2d6+1d4+3: [4, 2] [3] +3 = 12

    show_detail=False → {标签} {表达式} = {总计}
    """
    expr_str = _rebuild_expr(result)
    prefix = f"{result.label} {expr_str}" if result.label else expr_str

    if not show_detail:
        return f"{prefix} = {result.total}"

    # 骰值段：每组骰子一个括号列表，中间用空格隔开
    dice_parts: list[str] = []
    for gr in result.group_results:
        dice_parts.append(_format_dice_list(gr))
    dice_str = " ".join(dice_parts)

    # 平坦修正段（仅在有修正时显示）
    mod = result.expression.flat_modifier
    mod_str = f" +{mod}" if mod > 0 else (f" {mod}" if mod < 0 else "")

    dc = result.expression.dc
    if dc is not None:
        if result.is_natural_20:
            judge = "大成功"
        elif result.is_natural_1:
            judge = "大失败"
        else:
            judge = "成功" if result.total >= dc else "失败"
        total_str = f"{result.total} / {dc} {judge}"
        return f"{prefix}: {dice_str}{mod_str} = {total_str}"

    result_annotation = ""
    if result.is_natural_20:
        result_annotation = " 大成功"
    elif result.is_natural_1:
        result_annotation = " 大失败"

    return f"{prefix}: {dice_str}{mod_str} = {result.total}{result_annotation}"
