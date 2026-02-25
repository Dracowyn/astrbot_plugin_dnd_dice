"""
dice_parser.py — DnD 骰池表达式解析器。

支持语法（大小写不敏感）：
  基础:           d20, 1d20, 2d6+5, d8-1
  保留最高/最低:  4d6kh3, 2d20kl1
  优势/劣势:      d20adv, d20dis  （2d20kh1 / 2d20kl1 的语法糖）
  爆炸骰:         d6!, 2d10!
  多骰组:         2d6+1d4+3
  标签:           1d20+5 攻击检定  或  1d20+5#攻击检定
  复合修正:       2d6+1d4+3-1d2
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class DiceParseError(ValueError):
    """骰池表达式无法解析时抛出。"""


# ---------------------------------------------------------------------------
# 解析器产出的数据结构
# ---------------------------------------------------------------------------


@dataclass
class DiceGroup:
    """单组骰子，例如 4d6kh3 或 d20!。"""

    count: int  # 骰子数量
    sides: int  # 每个骰子的面数
    keep_mode: str | None = None  # "kh"（保留最高）或 "kl"（保留最低）
    keep_n: int | None = None  # 保留几个
    exploding: bool = False  # 是否为爆炸骰
    modifier: int = 0  # 每组的附加平坦修正，通常为 0


@dataclass
class ParsedExpression:
    """完整的骰池表达式解析结果。"""

    groups: list[DiceGroup] = field(default_factory=list)
    flat_modifier: int = 0  # 所有 token 累计的平坦整数修正值
    label: str = ""  # 可选标签/说明
    dc: int | None = None  # 难度等级（Difficulty Class），如 /r d20 感知 15 中的 15


# ---------------------------------------------------------------------------
# 词法分析器
# ---------------------------------------------------------------------------

# 单个骰子 token：[count]d<sides>[adv|dis|kh<n>|kl<n>][!]
_DICE_TOKEN_RE = re.compile(
    r"(?P<count>\d+)?d(?P<sides>\d+)"
    r"(?P<special>adv|dis|kh\d+|kl\d+)?"
    r"(?P<exploding>!)?",
    re.IGNORECASE,
)

# 平坦整数 token（用于位置感知匹配）
_INT_TOKEN_RE = re.compile(r"\d+")


def _strip_label(raw: str) -> tuple[str, str]:
    """
    从原始输入中分离表达式部分与可选标签。

    策略：从字符串起始处贪婪匹配骰子表达式字符集，
    遇到第一个非表达式字符（中文、字母标签等）即截断。
    支持有空格和无空格两种写法，例如：
      'd20 技能名 10'  →  ('d20', '技能名 10')
      'd20技能名10'   →  ('d20', '技能名10')
    '#' 为强制分隔符，优先处理。

    返回 (expression_part, label_part)。
    """
    raw = raw.strip()

    # 优先处理 '#' 强制分隔符。
    if "#" in raw:
        parts = raw.split("#", 1)
        return parts[0].strip(), parts[1].strip()

    # 从起始处匹配骰子表达式字符集（不含空格，空格视为分隔符）。
    # 字符集：数字、d/D、k/K、h/H、l/L、+、-、!、a/A、v/V、i/I、s/S（adv/dis）。
    m = re.match(r"^([\ddDkKhHlL+\-!aAvViIsS]+)(.*)", raw, re.DOTALL)
    if m and m.group(1):
        return m.group(1).strip(), m.group(2).strip()

    return raw, ""


def parse(raw: str) -> ParsedExpression:
    """
    将原始骰池表达式字符串解析为 ParsedExpression。

    表达式无效或为空时抛出 DiceParseError。
    """
    if not raw or not raw.strip():
        # 默认：单个 d20
        return ParsedExpression(
            groups=[DiceGroup(count=1, sides=20)], flat_modifier=0, label=""
        )

    expr_str, label = _strip_label(raw.strip())

    # 检测标签末尾是否为整数，若是则提取为难度等级（DC）。
    # 兼容有无空格两种写法：
    #   '技能名 15' → label='技能名', dc=15
    #   '技能名15'  → label='技能名', dc=15
    dc: int | None = None
    if label:
        # .*\D 确保数字前至少有一个非数字字符，避免把纯标签误判。
        dc_match = re.match(r"^(.*\D)(\d+)\s*$", label)
        if dc_match:
            label = dc_match.group(1).strip()
            dc = int(dc_match.group(2))
        elif re.match(r"^\d+$", label.strip()):
            # 标签本身就是纯数字（无文字说明时直接写 DC）
            dc = int(label.strip())
            label = ""

    # 去掉表达式内部的空格，便于 token 解析。
    expr_str = expr_str.replace(" ", "")
    if not expr_str:
        raise DiceParseError(f"无法解析骰池表达式: '{raw}'")

    # 逐 token 遍历表达式字符串。
    groups: list[DiceGroup] = []
    flat_modifier = 0
    pos = 0
    found_any = False

    while pos < len(expr_str):
        # 尝试在当前位置匹配一组骰子。
        # 多骰组时，骰子组前可有可选的 +/- 符号。
        sign = 1
        if expr_str[pos] in ("+", "-"):
            sign = 1 if expr_str[pos] == "+" else -1
            pos += 1
            if pos >= len(expr_str):
                raise DiceParseError(f"表达式末尾不能是运算符: '{raw}'")

        m = _DICE_TOKEN_RE.match(expr_str, pos)
        if m:
            found_any = True
            count_s = m.group("count")
            count = int(count_s) if count_s else 1
            sides = int(m.group("sides"))

            special = (m.group("special") or "").lower()
            exploding = bool(m.group("exploding"))
            keep_mode: str | None = None
            keep_n: int | None = None

            if special == "adv":
                count = 2
                keep_mode = "kh"
                keep_n = 1
            elif special == "dis":
                count = 2
                keep_mode = "kl"
                keep_n = 1
            elif special.startswith("kh"):
                keep_mode = "kh"
                keep_n = int(special[2:])
            elif special.startswith("kl"):
                keep_mode = "kl"
                keep_n = int(special[2:])

            group = DiceGroup(
                count=count,
                sides=sides,
                keep_mode=keep_mode,
                keep_n=keep_n,
                exploding=exploding,
            )
            # 负号骰子组：通过 modifier 哨兵值记录符号，执行器中取反小计。
            if sign == -1:
                group.modifier = -1  # 哨兵值：执行器将对小计取反
            groups.append(group)
            pos = m.end()
        else:
            # 尝试匹配平坦整数修正值。
            # +/- 符号已消耗并存储在 `sign` 中。
            m2 = _INT_TOKEN_RE.match(expr_str, pos)
            if m2:
                found_any = True
                flat_modifier += sign * int(m2.group(0))
                pos = m2.end()
            else:
                raise DiceParseError(
                    f"无法解析骰池表达式中的 '{expr_str[pos:]}' (完整输入: '{raw}')\n"
                    "示例语法: d20, 1d20+5, 4d6kh3, d20adv, d6!, 2d6+1d4+3"
                )

    if not found_any:
        raise DiceParseError(
            f"输入中未找到有效的骰池表达式: '{raw}'\n"
            "示例语法: d20, 1d20+5, 4d6kh3, d20adv, d6!, 2d6+1d4+3"
        )

    if not groups:
        # 仅有平坦修正值，无骰子组——仍为合法输入。
        pass

    return ParsedExpression(
        groups=groups, flat_modifier=flat_modifier, label=label, dc=dc
    )
