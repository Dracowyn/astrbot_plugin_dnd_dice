"""
dice_parser.py — DnD 骰池表达式解析器。

支持语法（大小写不敏感）：
  基础:               d20, 1d20, 2d6+5, d8-1
  FATE/Fudge 骰:      4dF
  保留最高/最低:      4d6kh3, 8d100k4, 2d20kl1
  丢弃最低/最高:      8d6d3 (dl3), 8d6dl3, 8d6dh3
  优势/劣势:          d20adv, d20dis  （2d20kh1 / 2d20kl1 的语法糖）
  标准爆炸骰:         d6!, 2d10!>4 (>=4 爆炸), d6!3 (=3 爆炸)
  复合爆炸骰:         5d6!! (Shadowrun 风格)
  穿透爆炸骰:         5d6!p (HackMaster 风格)
  目标数成功计数:     3d6>3, 10d6<4
  失败计数附加:       3d6>3f1, 10d6<4f>5
  重骰:               2d8r<2, 8d6r, 2d6ro<2 (只重骰一次)
  排序:               8d6s (升序), 8d6sd (降序)
  多骰组:             2d6+1d4+3
  标签:               1d20+5 攻击检定  或  1d20+5#攻击检定
  复合修正:           2d6+1d4+3-1d2
"""

from __future__ import annotations

import re
import unicodedata
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
class RerollCondition:
    """单条重骰条件，例如 r<2 或 ro=1。"""

    compare: str  # ">" / "<" / "="
    value: int
    once: bool = False  # True = ro（只重骰一次），False = r（循环）


@dataclass
class DiceGroup:
    """单组骰子，例如 4d6kh3 或 d20!。"""

    count: int  # 骰子数量
    sides: int  # 每个骰子的面数（0 代表 FATE 骰）
    fate: bool = False  # 是否为 FATE/Fudge 骰（dF）
    keep_mode: str | None = None  # "kh"（保留最高）或 "kl"（保留最低）
    keep_n: int | None = None  # 保留几个
    drop_mode: str | None = None  # "dl"（丢弃最低）或 "dh"（丢弃最高）
    drop_n: int | None = None  # 丢弃几个
    # --- 爆炸相关 ---
    exploding: bool = False  # 是否为爆炸骰
    explode_mode: str = "standard"  # "standard"(!), "compound"(!!), "penetrate"(!p)
    explode_compare: str | None = None  # ">" / "<" / "=" (None = 等于最大值)
    explode_value: int | None = None  # 自定义爆炸阈值
    # --- 成功/失败计数 ---
    success_compare: str | None = None  # ">" / "<" / "="
    success_value: int | None = None
    failure_compare: str | None = None  # ">" / "<" / "="
    failure_value: int | None = None
    # --- 重骰 ---
    reroll_conditions: list[RerollCondition] = field(default_factory=list)
    # --- 排序 ---
    sort_order: str | None = None  # "asc" / "desc"
    # --- 符号哨兵（内部使用）---
    modifier: int = 0  # -1 = 哨兵：该组取反


@dataclass
class ParsedExpression:
    """完整的骰池表达式解析结果。"""

    groups: list[DiceGroup] = field(default_factory=list)
    flat_modifier: int = 0  # 所有 token 累计的平坦整数修正值
    label: str = ""  # 可选标签/说明
    dc: int | None = None  # 难度等级（Difficulty Class），如 /r d20 感知 15 中的 15


# ---------------------------------------------------------------------------
# 词法分析助手
# ---------------------------------------------------------------------------

# 平坦整数 token（用于位置感知匹配）
_INT_TOKEN_RE = re.compile(r"\d+")

# 全角 → ASCII 转换表：防止全角符号（如 ＋）被误判为非 ASCII 标签分隔符
_FULLWIDTH_TABLE = str.maketrans(
    "＋－＊／（）０１２３４５６７８９",
    "+-*/()" + "0123456789",
)


def _normalize_fullwidth(s: str) -> str:
    """
    将常见全角算术字符规范化为 ASCII 等价形式。

    例如：用户输入 d20＋5，＋（U+FF0B）属非 ASCII，不进行此归一化则
    _strip_label 会在 ＋ 处截断，把 "+5" 误判成标签而改变掷骰逻辑。
    """
    return s.translate(_FULLWIDTH_TABLE)


def _read_int(s: str, pos: int) -> tuple[int | None, int]:
    """尝试在 pos 处读取非负整数，返回 (value, new_pos) 或 (None, pos)。"""
    m = _INT_TOKEN_RE.match(s, pos)
    if m:
        return int(m.group(0)), m.end()
    return None, pos


def _read_compare_point(s: str, pos: int) -> tuple[str | None, int | None, int]:
    """
    读取可选的比较点（ComparePoint）：[>|<|=]N 或 仅 N（默认 =）。
    返回 (compare_op, value, new_pos)；若无有效数字则返回 (None, None, pos)。
    """
    if pos >= len(s):
        return None, None, pos
    compare = "="
    if s[pos] in (">", "<", "="):
        compare = s[pos]
        pos += 1
    val, new_pos = _read_int(s, pos)
    if val is None:
        # 消耗了比较符但没有数字 → 回退
        if compare != "=":
            return None, None, pos - 1
        return None, None, pos
    return compare, val, new_pos


# ---------------------------------------------------------------------------
# 骰子 token 子解析器（各负责一类修饰，就地修改 DiceGroup）
# ---------------------------------------------------------------------------


def _parse_keep_drop(expr: str, pos: int, group: DiceGroup) -> int:
    """
    解析可选的 keep / drop / adv / dis 修饰符，就地更新 group。

    支持：kh / kl / k（保留）、dh / dl / d（丢弃）、adv / dis 语法糖。
    """
    n = len(expr)
    if pos >= n:
        return pos
    ch = expr[pos].lower()
    if ch == "k":
        pos += 1
        if pos < n and expr[pos].lower() == "h":
            pos += 1
            kn, pos = _read_int(expr, pos)  # type: ignore[assignment]
            group.keep_mode = "kh"
            group.keep_n = kn if kn is not None else 1
        elif pos < n and expr[pos].lower() == "l":
            pos += 1
            kn, pos = _read_int(expr, pos)  # type: ignore[assignment]
            group.keep_mode = "kl"
            group.keep_n = kn if kn is not None else 1
        else:
            kn, pos = _read_int(expr, pos)  # type: ignore[assignment]
            group.keep_mode = "kh"
            group.keep_n = kn if kn is not None else 1
    elif ch == "d":
        # 先检查 'dis' 语法糖，避免被 drop 分支误匹配
        if pos + 3 <= n and expr[pos : pos + 3].lower() == "dis":
            group.count = 2
            group.keep_mode = "kl"
            group.keep_n = 1
            pos += 3
        else:
            # 丢弃：需要 dl/dh/d(数字) 三种情况
            # 避免误把下一骰组的 'd' 消耗掉：必须有 h/l 或紧跟数字
            peek = pos + 1
            if peek < n and expr[peek].lower() == "h":
                pos += 2
                dn, pos = _read_int(expr, pos)  # type: ignore[assignment]
                group.drop_mode = "dh"
                group.drop_n = dn if dn is not None else 1
            elif peek < n and expr[peek].lower() == "l":
                pos += 2
                dn, pos = _read_int(expr, pos)  # type: ignore[assignment]
                group.drop_mode = "dl"
                group.drop_n = dn if dn is not None else 1
            elif peek < n and expr[peek].isdigit():
                pos += 1
                dn, pos = _read_int(expr, pos)  # type: ignore[assignment]
                group.drop_mode = "dl"  # 'd' 简写 = dl（丢弃最低，与 Roll20 一致）
                group.drop_n = dn if dn is not None else 1
            # else: 不是丢弃修饰 → 不消耗
    elif expr[pos : pos + 3].lower() == "adv":
        group.count = 2
        group.keep_mode = "kh"
        group.keep_n = 1
        pos += 3
    return pos


def _parse_exploding(expr: str, pos: int, group: DiceGroup) -> int:
    """解析可选的爆炸修饰（!、!!、!p）及自定义爆炸阈值，就地更新 group。"""
    n = len(expr)
    if pos >= n or expr[pos] != "!":
        return pos
    group.exploding = True
    pos += 1
    if pos < n and expr[pos] == "!":
        group.explode_mode = "compound"
        pos += 1
    elif pos < n and expr[pos].lower() == "p":
        group.explode_mode = "penetrate"
        pos += 1
    else:
        group.explode_mode = "standard"
    # 可选自定义爆炸 ComparePoint
    if pos < n and (expr[pos] in (">", "<", "=") or expr[pos].isdigit()):
        cmp, val, pos = _read_compare_point(expr, pos)
        group.explode_compare = cmp
        group.explode_value = val
    return pos


def _parse_success_failure(expr: str, pos: int, group: DiceGroup) -> int:
    """解析可选的成功计数（>N、<N）和失败计数（fN）修饰，就地更新 group。"""
    n = len(expr)
    if pos >= n or expr[pos] not in (">", "<"):
        return pos
    cmp, val, new_pos = _read_compare_point(expr, pos)
    if val is None:
        return pos
    group.success_compare = cmp
    group.success_value = val
    pos = new_pos
    # 可选失败计数（f[>|<|=]N）
    if pos < n and expr[pos].lower() == "f":
        pos += 1
        f_cmp, f_val, new_pos2 = _read_compare_point(expr, pos)
        if f_val is not None:
            group.failure_compare = f_cmp
            group.failure_value = f_val
            pos = new_pos2
        else:
            pos -= 1  # 'f' 后无数字 → 退回
    return pos


def _parse_reroll(expr: str, pos: int, group: DiceGroup) -> int:
    """解析可重复的重骰修饰（r 和 ro），就地向 group.reroll_conditions 追加条件。"""
    n = len(expr)
    while pos < n and expr[pos].lower() == "r":
        once = False
        pos += 1
        if pos < n and expr[pos].lower() == "o":
            once = True
            pos += 1
        cmp, val, new_pos = _read_compare_point(expr, pos)
        if val is None:
            cmp, val = "=", 1  # 默认：重骰 =1
        else:
            pos = new_pos
        group.reroll_conditions.append(
            RerollCondition(compare=cmp, value=val, once=once)
        )
    return pos


def _parse_sort(expr: str, pos: int, group: DiceGroup) -> int:
    """解析可选的排序修饰（s / sa / sd），就地更新 group。"""
    n = len(expr)
    if pos >= n or expr[pos].lower() != "s":
        return pos
    pos += 1
    if pos < n and expr[pos].lower() == "d":
        group.sort_order = "desc"
        pos += 1
    elif pos < n and expr[pos].lower() == "a":
        group.sort_order = "asc"
        pos += 1
    else:
        group.sort_order = "asc"  # 默认升序
    return pos


def _parse_dice_token(expr: str, pos: int) -> tuple[DiceGroup | None, int]:
    """
    从 expr[pos] 起尝试解析一个骰子 token。

    返回 (DiceGroup, new_pos) 或 (None, pos)（未能匹配时）。
    内部依次调用五个独立子解析器处理各类修饰符。

    子解析器扩展约定
    ----------------
    每个子解析器的函数签名如下::

        def _parse_XYZ(expr: str, pos: int, group: DiceGroup) -> int:

    须满足：
    * 只消耗其能识别的字符，相应推进 *pos*；
    * 修饰符不存在时返回原始 *pos* 不变；
    * 就地修改 *group* 以记录已解析的选项；
    * 不抛出异常——输入模糊时应回退（返回预读前的 pos）而非修改 *group*。

    新增修饰符应作为额外的子解析器实现，并追加到本函数末尾
    _parse_sort 调用链之后。
    """
    start = pos
    n = len(expr)

    # 1. 可选骰子数量
    count_val, pos = _read_int(expr, pos)

    # 2. 'D' 或 'd'
    if pos >= n or expr[pos].lower() != "d":
        return None, start

    pos += 1  # 消耗 'd'

    # 3. 骰面：'F'/'f' = FATE，否则整数
    fate = False
    sides = 0
    if pos < n and expr[pos].lower() == "f":
        fate = True
        sides = 0
        pos += 1
    else:
        sides_val, pos2 = _read_int(expr, pos)
        if sides_val is None:
            return None, start
        sides = sides_val
        pos = pos2

    count = count_val if count_val is not None else 1
    group = DiceGroup(count=count, sides=sides, fate=fate)

    # 4–8. 依次应用各类修饰（每个函数就地修改 group 并返回更新后的 pos）
    pos_after_base = pos  # 占位：消耗必选 NdM 后的位置
    pos = _parse_keep_drop(expr, pos, group)
    pos = _parse_exploding(expr, pos, group)
    pos = _parse_success_failure(expr, pos, group)
    pos = _parse_reroll(expr, pos, group)
    pos = _parse_sort(expr, pos, group)

    # 安全守卫：若所有子解析器均未前进，且剩余字符为字母，
    # 说明存在无法识别的修饰符，立即报错而非回退到主循环再失败。
    if pos == pos_after_base and pos < len(expr) and expr[pos].isalpha():
        raise DiceParseError(
            f"无法识别的骰子修饰符 {expr[pos:]!r}"
            f"（应为 kh/kl/k、dl/dh/d<N>、!、>/<、r/ro、s/s[ad] 之一）"
        )

    return group, pos


def _strip_label(raw: str) -> tuple[str, str]:
    """
    从原始输入中分离表达式部分与可选标签。

    分离策略（优先级从高到低）：
      1. '#' 强制分隔符，优先处理。
      2. 在首个非 ASCII *字母* 字符处截断（Unicode 类别 L*，如 CJK、假名；
         非 ASCII 标点/符号不触发）——兼容『d20 感知 15』、
         『2d6 + 1d4 伤害』（先找非 ASCII 再处理空格，避免算式空格误判为标签）。
      3. 以第一个空白字符切分：
         a. 若空白后紧跟 +/- 运算符，整体去空格视为纯算式，无标签。
         b. 否则按空白切分——兼容『d20 感知 15』（纯 ASCII 标签）。
      4. 纯 ASCII 且无分隔符：整体作为表达式，无标签。
         ASCII 标签必须通过 '#' 或空格显式分隔。

    返回 (expression_part, label_part)。
    """
    raw = raw.strip()
    raw = _normalize_fullwidth(raw)

    # 1. '#' 强制分隔符。
    if "#" in raw:
        parts = raw.split("#", 1)
        return parts[0].strip(), parts[1].strip()

    # 2. 在首个非 ASCII *字母* 字符处截断（此步须在空白检测之前，
    #    否则 '2d6 + 1d4 伤害' 会在第一个空格处就被截断）。
    #    仅对 Unicode 字母类别（unicodedata.category 以 'L' 开头，
    #    即 Lu/Ll/Lt/Lm/Lo，涵盖汉字、假名等自然语言文字）触发截断；
    #    非 ASCII 标点/符号（如 em-dash U+2013、NBSP U+00A0）不触发，
    #    避免对含有此类字符的算式表达式进行误分割。
    # 注意：此策略在首个非 ASCII 字母处截断。若未来表达式语法扩展至
    # 含非 ASCII 标识符，需在此处添加白名单豁免，避免误截断。
    for i, ch in enumerate(raw):
        if ord(ch) > 0x7F and unicodedata.category(ch).startswith("L"):
            return raw[:i].strip(), raw[i:].strip()

    # 3. 以第一个空白字符切分（纯 ASCII 输入）。
    ws_match = re.search(r"\s", raw)
    if ws_match:
        after = raw[ws_match.start() :].lstrip()
        # 若空白后紧跟运算符，说明这是算式内部空格（如 '2d6 + 1d4'），
        # 去除所有空格后整体作为表达式，不分离标签。
        if after and after[0] in ("+", "-"):
            # 移除全部空白字符（含 \n、\r 等），而非仅空格和制表符。
            return re.sub(r"\s+", "", raw), ""
        return raw[: ws_match.start()].strip(), raw[ws_match.start() :].strip()

    # 4. 纯 ASCII、无分隔符：整体作为表达式。
    return raw, ""


def parse(raw: str, default_sides: int = 20) -> ParsedExpression:
    """
    将原始骰池表达式字符串解析为 ParsedExpression。

    表达式无效或为空时抛出 DiceParseError。

    Args:
        raw: 原始骰池表达式字符串。
        default_sides: 无参数时使用的默认骰面数，默认为 20（d20）。
    """
    _MAX_INPUT_LEN = 200
    if raw and len(raw) > _MAX_INPUT_LEN:
        raise DiceParseError(
            f"表达式过长（输入 {len(raw)} 字符，最大 {_MAX_INPUT_LEN} 字符）"
        )

    if not raw or not raw.strip():
        # 默认：单个 dN（N 由调用方指定，通常为 20）
        return ParsedExpression(
            groups=[DiceGroup(count=1, sides=default_sides)], flat_modifier=0, label=""
        )

    expr_str, label = _strip_label(raw.strip())

    # 检测标签末尾是否为整数，若是则提取为难度等级（DC）。
    # 兼容有无空格两种写法：
    #   '技能名 15' → label='技能名', dc=15
    #   '技能名15'  → label='技能名', dc=15
    dc: int | None = None
    if label:
        # 仅在「空格 + 纯整数尾段」时提取 DC，避免 '房间2' 被误判为 DC=2。
        # 兼容写法：
        #   '感知 15'  → label='感知', dc=15（空格分隔）
        #   '15'       → label='', dc=15（纯数字标签）
        dc_match = re.match(r"^(.+\S)\s+(\d+)\s*$", label)
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
        # 处理可选 +/- 符号
        sign = 1
        if expr_str[pos] in ("+", "-"):
            sign = 1 if expr_str[pos] == "+" else -1
            pos += 1
            if pos >= len(expr_str):
                raise DiceParseError(f"表达式末尾不能是运算符: '{raw}'")

        group, new_pos = _parse_dice_token(expr_str, pos)
        if group is not None:
            if new_pos == pos:
                # 安全守卫：_parse_dice_token 返回了骰子组但位置未前进，
                # 这将导致死循环。正常情况下不应出现，属于防御性布局。
                raise DiceParseError(f"无法继续解析骰池表达式: '{raw}'")
            found_any = True
            if sign == -1:
                group.modifier = -1  # 哨兵值：执行器将对小计取反
            groups.append(group)
            pos = new_pos
        else:
            # 尝试匹配平坦整数修正值。
            m2 = _INT_TOKEN_RE.match(expr_str, pos)
            if m2:
                found_any = True
                flat_modifier += sign * int(m2.group(0))
                pos = m2.end()
            else:
                raise DiceParseError(
                    f"无法解析骰池表达式中的 '{expr_str[pos:]}' (完整输入: '{raw}')\n"
                    "示例语法: d20, 1d20+5, 4d6kh3, d20adv, d6!, 3d6>3, 2d8r<2, 4dF"
                )

    if not found_any:
        raise DiceParseError(
            f"输入中未找到有效的骰池表达式: '{raw}'\n"
            "示例语法: d20, 1d20+5, 4d6kh3, d20adv, d6!, 3d6>3, 2d8r<2, 4dF"
        )

    return ParsedExpression(
        groups=groups, flat_modifier=flat_modifier, label=label, dc=dc
    )
