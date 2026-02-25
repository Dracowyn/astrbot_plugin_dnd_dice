"""
main.py — AstrBot DnD D20 骰子插件入口。

指令：
  /r [表达式]    使用 DnD 骰池语法掷骰。
  /roll [表达式] /r 的别名。

支持的骰池语法（Roll20 规范）：
  d20                    单个 d20。
  1d20+5                 1 枚 d20 加 +5 修正。
  4dF                    4 枚 FATE/Fudge 骰（-/0/+）。
  4d6kh3                 掷 4d6，保留最高 3 个。
  8d100k4                掷 8d100，保留最高 4（k = kh 简写）。
  2d20kl1                掷 2d20，保留最低 1 个（劣势）。
  d20adv                 优势骰（2d20kh1 的简写）。
  d20dis                 劣势骰（2d20kl1 的简写）。
  8d6d3 / 8d6dl3         掷 8d6，丢弃最低 3 个。
  8d6dh3                 掷 8d6，丢弃最高 3 个。
  d6!                    标准爆炸骰（掷出最大值追加一骰）。
  d6!>4                  掷出 >=4 即爆炸。
  5d6!!                  复合爆炸（Shadowrun 风格，追加值合并）。
  5d6!p                  穿透爆炸（HackMaster 风格，追加骰 -1）。
  3d6>3                  目标数成功计数（>=3 算成功）。
  10d6<4                 目标数成功计数（<=4 算成功）。
  3d6>3f1                成功计数 + 失败计数（1 算失败）。
  2d8r<2                 重骰：<=2 的骰值循环重掷。
  2d6ro<2                重骰：<=2 只重掷一次。
  8d6s / 8d6sd           掷 8d6，结果升序/降序显示。
  2d6+1d4+3              多骰组合加修正值。
  1d20+5#攻击检定        用 '#' 分隔附加标签。
  1d20+5 攻击检定        用空格分隔附加标签。
  d20 感知 15            技能检定：标签 + DC，输出"成功/失败"。
  d20adv 察觉 13         优势技能检定。

LLM 函数工具：
  插件注册了 `roll_dice` 工具，LLM 可在 TRPG 叙事中自动调用该工具掷骰。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .character import CharacterManager
from .dice_parser import DiceParseError, parse
from .dice_roller import DiceRollError, roll
from .formatter import format_result

# ---------------------------------------------------------------------------
# 配置读取辅助函数
# ---------------------------------------------------------------------------


def _safe_int(value: object, default: int, min_val: int | None = None) -> int:
    """将任意配置值安全转换为 int，转换失败或低于下限时返回默认值。"""
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if min_val is not None and result < min_val:
        return default
    return result


def _safe_bool(value: object, default: bool) -> bool:
    """将任意配置值安全转换为 bool，转换失败时返回默认值。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() not in ("false", "0", "no", "off", "")
    try:
        return bool(value)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 解析失败或请求帮助时显示的语法提示
# ---------------------------------------------------------------------------

_SYNTAX_HELP = (
    "用法：/r [骰池表达式] [标签] [DC]\n"
    "示例：\n"
    "  /r d20\n"
    "  /r 1d20+5\n"
    "  /r 4dF                  FATE骰\n"
    "  /r 4d6kh3\n"
    "  /r 8d100k4              k = kh 简写\n"
    "  /r 8d6d3                丢弃最低3\n"
    "  /r 8d6dh3               丢弃最高3\n"
    "  /r d20adv\n"
    "  /r d20dis\n"
    "  /r d6!                  标准爆炸\n"
    "  /r d6!>4                自定义爆炸点\n"
    "  /r 5d6!!                复合爆炸\n"
    "  /r 5d6!p                穿透爆炸\n"
    "  /r 3d6>3                目标数成功计数\n"
    "  /r 3d6>3f1              成功+失败计数\n"
    "  /r 2d8r<2               重骰\n"
    "  /r 2d6ro<2              只重骰一次\n"
    "  /r 8d6s                 排序(升序)\n"
    "  /r 8d6sd                排序(降序)\n"
    "  /r 2d6+1d4+3 伤害\n"
    "  /r 1d20+5#攻击检定\n"
    "  /r d20 感知 15\n"
    "  /r d20adv 察觉 13"
)


# ---------------------------------------------------------------------------
# 插件主类
# ---------------------------------------------------------------------------


@register(
    "astrbot_plugin_dnd_dice",
    "Dracowyn",
    "支持完整 Roll20 骰池规范的 DnD 掷骰插件，含 FATE 骰、丢弃骰、爆炸变体、成功计数、重骰、排序及 LLM 工具调用。",
    "0.2.0",
)
class DnDDicePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        cfg = config or {}
        # _safe_int / _safe_bool 防止非数字字符串或越界值导致插件加载失败
        self.max_dice_count: int = _safe_int(cfg.get("max_dice_count"), 100, min_val=1)
        self.max_dice_sides: int = _safe_int(cfg.get("max_dice_sides"), 1000, min_val=1)
        self.exploding_max_depth: int = _safe_int(
            cfg.get("exploding_max_depth"), 20, min_val=1
        )
        self.show_detail: bool = _safe_bool(cfg.get("show_detail"), True)

        # 骰面大小配置
        self.default_dice_sides: int = _safe_int(
            cfg.get("default_dice_sides"), 20, min_val=2
        )
        self.allow_session_dice_sides: bool = _safe_bool(
            cfg.get("allow_session_dice_sides"), True
        )
        self.enable_whitelist: bool = _safe_bool(cfg.get("enable_whitelist"), False)
        raw_whitelist = cfg.get("whitelist_users") or []
        self.whitelist_users: list[str] = (
            [str(u) for u in raw_whitelist] if isinstance(raw_whitelist, list) else []
        )
        self.allow_private_bypass_whitelist: bool = _safe_bool(
            cfg.get("allow_private_bypass_whitelist"), True
        )

        # 角色卡管理器延迟初始化，避免核心接口尚未实现时被意外调用
        self._character_manager: CharacterManager | None = None

    @property
    def character_manager(self) -> CharacterManager:
        """懒加载角色卡管理器（核心持久化接口在后续版本中实现）。"""
        if self._character_manager is None:
            self._character_manager = CharacterManager(star=self)
        return self._character_manager

    async def initialize(self) -> None:
        logger.info(
            "[dnd_dice] DnD D20 骰子插件已加载。"
            f"限制: 最多骰子数={self.max_dice_count}, 最大面数={self.max_dice_sides}, "
            f"爆炸深度={self.exploding_max_depth}, 显示明细={self.show_detail}, "
            f"默认骰面={self.default_dice_sides}, 允许会话设置={self.allow_session_dice_sides}"
        )

    # ------------------------------------------------------------------
    # 骰面大小辅助方法
    # ------------------------------------------------------------------

    async def _get_effective_sides(self, event: AstrMessageEvent) -> int:
        """
        获取当前会话的有效默认骰面数。

        优先使用会话级设置（通过 /dset 命令设置），不存在则回退到全局默认值。
        """
        key = f"session_sides:{event.unified_msg_origin}"
        sides = await self.get_kv_data(key, self.default_dice_sides)
        return _safe_int(sides, self.default_dice_sides, min_val=2)

    async def _check_permission(self, event: AstrMessageEvent) -> bool:
        """
        检查当前用户是否有权使用 /dset 命令。

        判断顺序：
        1. allow_session_dice_sides 为 False → 始终拒绝
        2. enable_whitelist 为 False → 始终允许
        3. 私聊且 allow_private_bypass_whitelist → 允许
        4. whitelist_users 非空 → 检查 sender_id 是否在列表中
        5. whitelist_users 为空 → 使用 AstrBot 管理员判断
        """
        if not self.allow_session_dice_sides:
            return False
        if not self.enable_whitelist:
            return True
        if self.allow_private_bypass_whitelist and event.is_private_chat():
            return True
        sender_id = str(event.get_sender_id())
        if self.whitelist_users:
            return sender_id in self.whitelist_users
        # 白名单为空，回退到 AstrBot 全局管理员
        return event.is_admin()

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _do_roll(self, expression_str: str, default_sides: int = 20) -> str:
        """
        解析、执行并格式化一条骰池表达式。

        返回纯文本结果字符串，所有异常均被捕获并转换为可读错误信息。

        Args:
            expression_str: 骰池表达式字符串。
            default_sides: 空表达式时使用的默认骰面数。
        """
        try:
            expr = parse(expression_str, default_sides=default_sides)
        except DiceParseError as e:
            return f"解析错误: {e}\n{_SYNTAX_HELP}"

        try:
            result = roll(
                expr,
                max_dice=self.max_dice_count,
                max_sides=self.max_dice_sides,
                exploding_depth=self.exploding_max_depth,
            )
        except DiceRollError as e:
            return f"掷骰错误: {e}"

        try:
            return format_result(result, show_detail=self.show_detail)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[dnd_dice] 格式化结果时发生意外错误: {e}")
            return "掷骰完成，但系统内部错误"

    # ------------------------------------------------------------------
    # /r 指令处理器
    # ------------------------------------------------------------------

    @filter.command("r", alias={"roll"})
    async def roll_cmd(self, event: AstrMessageEvent) -> AsyncGenerator:
        """
        使用 DnD 骰池语法掷骰。

        用法: /r [骰池表达式] [标签] [DC]
        示例: /r 1d20+5, /r 4d6kh3, /r d20adv, /r d6!, /r 2d6+1d4+3 伤害
              /r d20 感知 15, /r d20感知15, /r d20+3 奥秘 12
        """
        raw_msg: str = event.message_str.strip()

        # 去掉开头的指令名（/r 或 /roll），提取骰池表达式部分。
        parts = raw_msg.split(None, 1)  # 按第一个空白字符分割
        expression_str = parts[1].strip() if len(parts) > 1 else ""

        effective_sides = await self._get_effective_sides(event)

        # 无参数时默认掷一个 dN（N 为会话/全局默认骰面数）
        if not expression_str:
            expression_str = f"d{effective_sides}"

        output = self._do_roll(expression_str, default_sides=effective_sides)
        yield event.plain_result(output)

    # ------------------------------------------------------------------
    # LLM 函数工具
    # ------------------------------------------------------------------

    @filter.command("dset", alias={"dice_set"})
    async def dset_cmd(self, event: AstrMessageEvent) -> AsyncGenerator:
        """
        设置当前会话的默认骰面数。

        用法:
          /dset <面数>     将当前会话默认骰面数设为指定值
          /dset reset     清除会话设置，恢复为全局默认
          /dset           查看当前会话的默认骰面数
        """
        raw_msg: str = event.message_str.strip()
        parts = raw_msg.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        # 查询当前设置
        if not arg:
            current = await self._get_effective_sides(event)
            key = f"session_sides:{event.unified_msg_origin}"
            is_session_set = await self.get_kv_data(key, None) is not None
            source = "会话设置" if is_session_set else "默认"
            yield event.plain_result(
                f"当前默认骰面数: d{current}（{source}）\n"
                # f"全局默认: d{self.default_dice_sides}\n"
                f"用法: /dset <面数>\n"
                f"示例: /dset 6\n"
                f"重置: /dset reset\n"
            )
            return

        # 权限检查
        if not await self._check_permission(event):
            if not self.allow_session_dice_sides:
                yield event.plain_result("管理员已禁用会话骰面设置功能。")
            else:
                yield event.plain_result(
                    "你没有权限使用此命令。"
                    + (
                        "（白名单模式已启用，请联系管理员）"
                        if self.enable_whitelist
                        else ""
                    )
                )
            return

        key = f"session_sides:{event.unified_msg_origin}"

        # 重置会话设置
        if arg.lower() in ("reset", "重置", "0"):
            await self.delete_kv_data(key)
            yield event.plain_result(
                f"已清除骰面设置，恢复为默认 d{self.default_dice_sides}。"
            )
            return

        # 解析并验证面数
        try:
            new_sides = int(arg)
        except ValueError:
            yield event.plain_result(
                f"无效的面数: '{arg}'，请输入 2~{self.max_dice_sides} 之间的整数，或 reset 重置。"
            )
            return

        if new_sides < 2:
            yield event.plain_result("骰面数不能小于 2。")
            return
        if new_sides > self.max_dice_sides:
            yield event.plain_result(f"骰面数不能超过限制 {self.max_dice_sides}。")
            return

        await self.put_kv_data(key, new_sides)
        yield event.plain_result(
            f"已将当前默认骰面数设为 d{new_sides}。"
            f"后续 /r 将默认投掷 d{new_sides}。"
        )

    @filter.llm_tool(name="roll_dice")
    async def roll_dice_tool(
        self,
        event: AstrMessageEvent,
        expression: str,
        label: str = "",
    ) -> str:
        """
        在 TRPG/DnD 游戏中掷骰子。当需要进行攻击骰、伤害骰、属性检定、豁免
        或任何需要随机结果的场合时调用此工具。返回值为掷骰结果，你需要将结果融入叙事中。

        Args:
            expression(string): DnD/Roll20 标准骰池表达式，不含标签和 DC。
                - 基础骰: "d20", "1d20", "2d6", "d8"
                - FATE 骰: "4dF"（-1/0/+1 三面骰）
                - 带修正: "1d20+5", "2d6+3", "d20-1"
                - 保留最高/最低: "4d6kh3"（4d6取高3）, "2d20kl1"（2d20取低1）
                - k = kh 简写: "8d100k4"
                - 丢弃最低/最高: "8d6d3"(dl3), "8d6dh3"
                - 优势/劣势: "d20adv"（优势）, "d20dis"（劣势）
                - 标准爆炸骰: "d6!", "d6!>4"（>=4 即爆）
                - 复合爆炸: "5d6!!"（Shadowrun 风格）
                - 穿透爆炸: "5d6!p"（HackMaster 风格）
                - 目标数成功计数: "3d6>3", "10d6<4"
                - 成功+失败计数: "3d6>3f1"
                - 重骰: "2d8r<2"（循环）, "2d6ro<2"（只重骰一次）
                - 排序: "8d6s"（升序）, "8d6sd"（降序）
                - 多骰组合: "2d6+1d4", "1d8+1d6+5"
            label(string): 本次投掷的说明，不需要标签时传空字符串。
                - 仅说明: "攻击检定", "力量豁免", "火球伤害"
                - 含 DC 判定（有空格）: "感知 15", "奥秘检定 12"
                  → 掷骰总计 >= DC 时输出"成功"，否则"失败"
                  → 天然 20 强制为"大成功"，天然 1 强制为"大失败"
                - 含 DC 判定（无空格）: "感知15", "奥秘检定12"（与上等价）
        """
        # 将标签拼入表达式，交给解析器处理。
        if label:
            full_expr = f"{expression}#{label}"
        else:
            full_expr = expression

        effective_sides = await self._get_effective_sides(event)
        output = self._do_roll(full_expr, default_sides=effective_sides)

        # 将结果返回给 LLM，由 LLM 将骰点结果融入叙事后回复用户。
        return output

    # ------------------------------------------------------------------
    # 插件卸载
    # ------------------------------------------------------------------

    async def terminate(self) -> None:
        logger.info("[dnd_dice] DnD D20 骰子插件已卸载。")
