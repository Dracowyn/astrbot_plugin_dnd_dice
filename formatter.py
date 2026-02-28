"""
formatter.py — DnD 骰点结果的纯文本格式化器。

输出规则：
  - 不使用任何 Markdown 语法（无 **、~~、>、-、# 等）
  - 不使用 emoji
  - 被丢弃的骰子用括号标注，如 (1)
  - 被重骰替换的原始值附加波浪线，如 ~2~
  - 爆炸追加骰附加 "!" 后缀，如 6!
  - FATE 骰面映射：-1 → "-", 0 → "0", 1 → "+"
  - 成功计数模式下，计入成功的骰值标注 *，计入失败的标注 x
  - 天然 20 / 天然 1 在结果行末注释
  - show_detail=False 时仅显示：<表达式> = <总计>
"""

from __future__ import annotations

from .dice_roller import DiceGroupResult, DieRoll, RollResult

# ---------------------------------------------------------------------------
# FATE 骰值映射
# ---------------------------------------------------------------------------

_FATE_DISPLAY = {-1: "-", 0: "0", 1: "+"}


def _fate_str(v: int) -> str:
    return _FATE_DISPLAY.get(v, str(v))


def _cmp(value: int, op: str, threshold: int) -> bool:
    """计算 value <op> threshold，其中 '>' / '<' 分别表示 >= / <=。

    与 dice_roller._compare 逻辑一致，但避免导入私有名称。
    """
    if op == ">":
        return value >= threshold
    if op == "<":
        return value <= threshold
    return value == threshold


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _group_label(gr: DiceGroupResult) -> str:
    """为单组骰子构建可读标签，如 '4d6kh3'、'd20adv'、'5d6!!'、'3d6>3'。"""
    g = gr.group
    count_str = str(g.count) if g.count != 1 else ""

    if g.fate:
        base = f"{count_str}dF"
    else:
        base = f"{count_str}d{g.sides}"

    # keep / drop 后缀
    if g.keep_mode == "kh" and g.keep_n == 1 and g.count == 2:
        kd_suffix = "adv"
    elif g.keep_mode == "kl" and g.keep_n == 1 and g.count == 2:
        kd_suffix = "dis"
    elif g.keep_mode == "kh" and g.keep_n is not None:
        kd_suffix = f"kh{g.keep_n}"
    elif g.keep_mode == "kl" and g.keep_n is not None:
        kd_suffix = f"kl{g.keep_n}"
    elif g.drop_mode == "dl" and g.drop_n is not None:
        kd_suffix = f"dl{g.drop_n}"
    elif g.drop_mode == "dh" and g.drop_n is not None:
        kd_suffix = f"dh{g.drop_n}"
    else:
        kd_suffix = ""

    # 爆炸后缀
    if g.exploding:
        if g.explode_mode == "compound":
            explode_mark = "!!"
        elif g.explode_mode == "penetrate":
            explode_mark = "!p"
        else:
            explode_mark = "!"
        # 自定义爆炸阈值
        if g.explode_compare is not None and g.explode_value is not None:
            cmp = "" if g.explode_compare == "=" else g.explode_compare
            explode_mark += f"{cmp}{g.explode_value}"
    else:
        explode_mark = ""

    # 成功/失败计数
    success_str = ""
    if g.success_compare is not None and g.success_value is not None:
        cmp = "" if g.success_compare == "=" else g.success_compare
        success_str = f"{cmp}{g.success_value}"
    failure_str = ""
    if g.failure_compare is not None and g.failure_value is not None:
        cmp = "" if g.failure_compare == "=" else g.failure_compare
        failure_str = f"f{cmp}{g.failure_value}"

    # 重骰后缀
    reroll_str_parts = []
    for cond in g.reroll_conditions:
        prefix = "ro" if cond.once else "r"
        cmp = "" if cond.compare == "=" else cond.compare
        reroll_str_parts.append(f"{prefix}{cmp}{cond.value}")
    reroll_str = "".join(reroll_str_parts)

    # 排序后缀
    if g.sort_order == "desc":
        sort_str = "sd"
    elif g.sort_order == "asc":
        sort_str = "s"
    else:
        sort_str = ""

    sign = "-" if gr.negated else ""
    return f"{sign}{base}{kd_suffix}{explode_mark}{success_str}{failure_str}{reroll_str}{sort_str}"


def _annotate_die(die: DieRoll, gr: DiceGroupResult) -> str:
    """Format a single DieRoll into its display string."""
    g = gr.group
    raw = _fate_str(die.value) if g.fate else str(die.value)
    if die.state == "rerolled":
        return f"~{raw}~"
    if die.state == "exploded":
        # 爆炸追加骰在成功计数模式下同样参与计数，展示时也应标注成功/失败标记。
        if gr.is_success_mode and g.success_compare and g.success_value is not None:
            if _cmp(die.value, g.success_compare, g.success_value):
                raw = f"{raw}*"
            elif (
                g.failure_compare
                and g.failure_value is not None
                and _cmp(die.value, g.failure_compare, g.failure_value)
            ):
                raw = f"{raw}x"
        return f"{raw}!"
    # state 为 "kept"（保留）或 "dropped"（已丢弃）时到达此处
    if gr.is_success_mode and die.state == "kept":
        if g.success_compare and g.success_value is not None:
            if _cmp(die.value, g.success_compare, g.success_value):
                raw = f"{raw}*"
            elif (
                g.failure_compare
                and g.failure_value is not None
                and _cmp(die.value, g.failure_compare, g.failure_value)
            ):
                raw = f"{raw}x"
    if die.state == "dropped":
        return f"({raw})"
    return raw


def _format_dice_list(gr: DiceGroupResult) -> str:
    """
    将单组骰子的各个骰值格式化为括号列表。

    展示规则：
      - 被丢弃骰子用圆括号包裹：(1)
      - 被重骰替换的原始值加波浪线：~2~
      - 爆炸追加骰加 '!' 后缀（复合爆炸无追加骰概念，跳过）
      - FATE 骰显示 -/0/+
      - 成功计数模式：计入成功的骰值加 *，计入失败的加 x

    优先使用引擎填充的 die_rolls（按位置精确标注，杜绝重复值引发的顺序错乱）。
    若 die_rolls 为空（如测试直接构造 DiceGroupResult），则回退到基于频率计数的
    旧路径（原有行为，向后兼容）。
    """
    # --- 新路径：引擎已提供带状态的 DieRoll 列表 ---
    if gr.die_rolls:
        return "[" + ", ".join(_annotate_die(d, gr) for d in gr.die_rolls) + "]"

    # --- 旧路径（向后兼容，用于未填充 die_rolls 的直接构造场景）---
    # WARNING: 此路径通过频率计数推断每驔骰子的状态。该未必准确
    # 当多驔骰子面値相同时：不同位置的相同值可能被误属为保留或丢弃。
    # 请始终在原始投骰路径中填充 die_rolls，尽量不依赖此回退分支。
    g = gr.group
    fate = g.fate

    base_count = g.count
    base_rolls = gr.all_rolls[:base_count]
    extra_rolls = gr.all_rolls[base_count:]  # 爆炸追加

    rerolled_remaining: dict[int, int] = {}
    for v in gr.rerolled_originals:
        rerolled_remaining[v] = rerolled_remaining.get(v, 0) + 1

    dropped_remaining: dict[int, int] = {}
    for v in gr.dropped_rolls:
        dropped_remaining[v] = dropped_remaining.get(v, 0) + 1

    display_parts: list[str] = []

    for val in base_rolls:
        if rerolled_remaining.get(val, 0) > 0:
            rerolled_remaining[val] -= 1
            raw = _fate_str(val) if fate else str(val)
            display_parts.append(f"~{raw}~")
            continue
        raw = _fate_str(val) if fate else str(val)
        if gr.is_success_mode:
            if g.success_compare and g.success_value is not None:
                if _cmp(val, g.success_compare, g.success_value):
                    raw = f"{raw}*"
                elif (
                    g.failure_compare
                    and g.failure_value is not None
                    and _cmp(val, g.failure_compare, g.failure_value)
                ):
                    raw = f"{raw}x"
        if dropped_remaining.get(val, 0) > 0:
            dropped_remaining[val] -= 1
            display_parts.append(f"({raw})")
        else:
            display_parts.append(raw)

    for val in extra_rolls:
        raw = _fate_str(val) if fate else str(val)
        if gr.is_success_mode and g.success_compare and g.success_value is not None:
            if _cmp(val, g.success_compare, g.success_value):
                raw = f"{raw}*"
            elif (
                g.failure_compare
                and g.failure_value is not None
                and _cmp(val, g.failure_compare, g.failure_value)
            ):
                raw = f"{raw}x"
        display_parts.append(f"{raw}!")

    return "[" + ", ".join(display_parts) + "]"


def _rebuild_expr(result: RollResult) -> str:
    """重建用于显示的紧凑表达式字符串。"""
    parts: list[str] = []
    for i, gr in enumerate(result.group_results):
        lbl = _group_label(gr)
        if i == 0 and not gr.negated:
            parts.append(lbl.lstrip("+"))
        elif gr.negated:
            parts.append(lbl)  # _group_label 已带 '-' 前缀
        else:
            parts.append("+" + lbl)

    mod = result.expression.flat_modifier
    if mod > 0:
        parts.append(f"+{mod}")
    elif mod < 0:
        parts.append(str(mod))

    return "".join(parts)


# ---------------------------------------------------------------------------
# 公开格式化器
# ---------------------------------------------------------------------------


def _net_success_str(total_successes: int, total_failures: int) -> str:
    """
    将成功数/失败数格式化为可读字符串。

    当负号组导致计数器出现负値时，改为展示净值，避免 '−N成功' 等语义难懂的输出。
    """
    if total_successes < 0 or total_failures < 0:
        net = total_successes - total_failures
        return f"{net}净成功" if net >= 0 else f"{abs(net)}净失败"
    if total_failures:
        return f"{total_successes}成功 {total_failures}失败"
    return f"{total_successes}成功"


def format_result(result: RollResult, show_detail: bool = True) -> str:
    """
    将 RollResult 格式化为单行纯文本字符串。

    show_detail=True  → {标签} {表达式}: {骰值列表} = {总计}
    show_detail=False → {标签} {表达式} = {总计}

    成功计数模式示例：
      3d6>3: [1, 4*, 5*, 2, 6*] = 3成功
      3d6>3f1: [1x, 4*, 5*] = 2成功 1失败
    """
    expr_str = _rebuild_expr(result)
    prefix = f"{result.label} {expr_str}" if result.label else expr_str

    # --- 成功计数模式 ---
    if result.is_success_mode:
        total_successes = 0
        total_failures = 0
        for gr in result.group_results:
            s = gr.successes or 0
            f = gr.failures or 0
            if gr.negated:
                # 负号组：将两个计数器各自减去本组值，保持净值与 subtotal 一致
                total_successes -= s
                total_failures -= f
            else:
                total_successes += s
                total_failures += f

        if not show_detail:
            return f"{prefix} = {_net_success_str(total_successes, total_failures)}"

        dice_parts = [_format_dice_list(gr) for gr in result.group_results]
        dice_str = " ".join(dice_parts)

        dc = result.expression.dc
        if dc is not None:
            net = total_successes - total_failures
            judge = "成功" if net >= dc else "失败"
            count_str = _net_success_str(total_successes, total_failures)
            return f"{prefix}: {dice_str} = {count_str} / {dc} {judge}"

        count_str = _net_success_str(total_successes, total_failures)
        return f"{prefix}: {dice_str} = {count_str}"

    # --- 普通求和模式 ---
    if not show_detail:
        return f"{prefix} = {result.total}"

    dice_parts = [_format_dice_list(gr) for gr in result.group_results]
    dice_str = " ".join(dice_parts)

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
