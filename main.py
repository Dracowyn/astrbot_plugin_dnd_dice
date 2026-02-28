"""
main.py — AstrBot DnD D20 骰子插件入口。

指令：
  /r [表达式]    使用 DnD 骰池语法掷骰。
  /roll [表达式] /r 的别名。
  /rh [me|all|clear]  查看或清空当前会话的投掷历史记录。
    无参数 / all：群聊显示全员历史，私聊显示本人历史。
    me：仅显示自己的投掷记录（群聊内使用）。
    clear：清空当前会话历史（群聊需白名单权限）。

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

import time
from collections import OrderedDict
from collections.abc import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .character import CharacterManager
from .dice_parser import DiceParseError, parse
from .dice_roller import DiceRollError, roll
from .formatter import format_result
from .history import ROLL_ERROR_PREFIXES, RollHistoryManager

# 内存前缀缓存所允许的最大会话来源数量。
_PREFIX_CACHE_MAX: int = 512
# 前缀缓存条目 TTL（秒）。超时后下次访问触发 KV 重新验证，
# 降低外部直接修改 KV 后本实例长期持有陈旧前缀的风险。
_PREFIX_CACHE_TTL: float = 300.0

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
        return value.strip().lower() not in ("false", "0", "no", "off", "")
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
        self.max_input_len: int = _safe_int(cfg.get("max_input_len"), 200, min_val=10)
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

        # 自定义触发前缀配置
        self.default_cmd_prefix: str = str(cfg.get("default_cmd_prefix") or "")
        self.allow_custom_prefix: bool = _safe_bool(
            cfg.get("allow_custom_prefix"), True
        )

        # 投掷历史配置
        self.enable_history: bool = _safe_bool(cfg.get("enable_history"), True)
        self.allow_view_history: bool = _safe_bool(cfg.get("allow_view_history"), True)
        self.max_history_count: int = _safe_int(
            cfg.get("max_history_count"), 50, min_val=1
        )

        # 投掷历史管理器
        self._history = RollHistoryManager(
            star=self,
            max_count=self.max_history_count,
            enabled=self.enable_history,
        )

        # 角色卡管理器延迟初始化，避免核心接口尚未实现时被意外调用
        self._character_manager: CharacterManager | None = None

        # 内存写透式 LRU 缓存，按会话来源存储自定义前缀。
        # 避免在 custom_prefix_route 中对每条消息都进行 KV 存储查询。
        # 键为 unified_msg_origin 字符串；None 表示“未设置自定义前缀”。
        # OrderedDict 保留插入顺序，命中时调用 move_to_end() 可实现
        # O(1) 的 LRU 驱逐，容量溢出时调用 popitem(last=False)。
        self._prefix_cache: OrderedDict[str, str | None] = (
            OrderedDict()
        )  # 每个缓存条目的写入时间戳（monotonic），用于 TTL 过期检测。
        self._prefix_cache_ts: dict[str, float] = {}

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
            f"默认骰面={self.default_dice_sides}, 允许会话设置={self.allow_session_dice_sides}, "
            f"启用历史={self.enable_history}, 允许查看历史={self.allow_view_history}, "
            f"最大历史记录数={self.max_history_count}"
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

    async def _get_effective_prefix(self, event: AstrMessageEvent) -> str:
        """
        获取当前会话的有效自定义触发前缀。

        优先使用会话级设置（通过 /rprefix 命令设置），不存在则回退到全局配置值。
        结果缓存在 _prefix_cache 中，避免对每条消息都访问 KV 存储。
        缓存条目在 _PREFIX_CACHE_TTL 秒后过期失效，届时重新读取 KV，
        以便感知外部直接修改（其他实例、后台脚本等）带来的前缀变更。
        """
        origin = event.unified_msg_origin
        now = time.monotonic()
        ttl_expired = now - self._prefix_cache_ts.get(origin, 0.0) > _PREFIX_CACHE_TTL
        if origin not in self._prefix_cache or ttl_expired:
            kv_key = f"custom_prefix:{origin}"
            raw = await self.get_kv_data(kv_key, None)
            self._set_prefix_cache(origin, str(raw) if raw is not None else None)
        else:
            # 缓存命中且未过期：将条目提升至最近使用位置。
            self._prefix_cache.move_to_end(origin)
        cached = self._prefix_cache[origin]
        return cached if cached is not None else self.default_cmd_prefix

    def _set_prefix_cache(self, origin: str, value: str | None) -> None:
        """写入前缀缓存，内部自动执行容量检查与 LRU 驱逐，所有写入路径均通过此方法。"""
        if (
            origin not in self._prefix_cache
            and len(self._prefix_cache) >= _PREFIX_CACHE_MAX
        ):
            # 缓存已满 且 key 不在缓存中：驱逐最久未使用的条目及其时间戳。
            evicted, _ = self._prefix_cache.popitem(last=False)
            self._prefix_cache_ts.pop(evicted, None)
        self._prefix_cache[origin] = value
        self._prefix_cache.move_to_end(origin)  # 提升至 MRU 位置
        self._prefix_cache_ts[origin] = time.monotonic()  # 刷新写入时间戳

    async def _whitelist_check(self, event: AstrMessageEvent) -> bool:
        """
        管理命令白名单验证（不含功能开关检查）。

        注意：此白名单仅限制 /dset 和 /rprefix 等管理命令的使用权限，
        不影响 /r 掷骰指令（掷骰对所有用户始终开放）。

        判断顺序：
        1. enable_whitelist 为 False → 始终允许
        2. 私聊且 allow_private_bypass_whitelist → 允许
        3. whitelist_users 非空 → 检查 sender_id 是否在列表中
        4. whitelist_users 为空 → 使用 AstrBot 管理员判断
        """
        if not self.enable_whitelist:
            return True
        if self.allow_private_bypass_whitelist and event.is_private_chat():
            return True
        sender_id = str(event.get_sender_id())
        if self.whitelist_users:
            return sender_id in self.whitelist_users
        # 白名单为空，回退到 AstrBot 全局管理员
        return event.is_admin()

    async def _check_permission(self, event: AstrMessageEvent) -> bool:
        """
        检查当前用户是否有权使用 /dset 命令。

        判断顺序：
        1. allow_session_dice_sides 为 False → 始终拒绝
        2. 通用白名单验证
        """
        if not self.allow_session_dice_sides:
            return False
        return await self._whitelist_check(event)

    async def _check_prefix_permission(self, event: AstrMessageEvent) -> bool:
        """
        检查当前用户是否有权使用 /rprefix 命令。

        判断顺序：
        1. allow_custom_prefix 为 False → 始终拒绝
        2. 通用白名单验证
        """
        if not self.allow_custom_prefix:
            return False
        return await self._whitelist_check(event)

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
            expr = parse(
                expression_str,
                default_sides=default_sides,
                max_input_len=self.max_input_len,
            )
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
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            logger.exception(f"[dnd_dice] 格式化结果时发生意外错误: {e}")
            return "掷骰完成，但格式化时发生内部错误"

    # ------------------------------------------------------------------
    # /dset 命令核心逻辑（供标准命令和自定义前缀路由共用）
    # ------------------------------------------------------------------

    async def _handle_dset(
        self, event: AstrMessageEvent, arg: str, display_prefix: str = "/"
    ) -> AsyncGenerator:
        """
        /dset 命令核心逻辑，由 dset_cmd 和 custom_prefix_route 统一调用。

        Args:
            event: 消息事件。
            arg: 去除命令名后的参数字符串（空字符串表示仅查询）。
            display_prefix: 回复提示中显示的前缀符号（如 '/' 或自定义符号）。
        """
        key = f"session_sides:{event.unified_msg_origin}"

        # 权限检查（查询与写入均需授权，防止未授权用户探测会话配置）
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

        # 查询当前设置
        if not arg:
            current = await self._get_effective_sides(event)
            is_session_set = await self.get_kv_data(key, None) is not None
            source = "会话设置" if is_session_set else "默认"
            yield event.plain_result(
                f"当前默认骰面数: d{current}（{source}）\n"
                f"用法: {display_prefix}dset <面数>\n"
                f"示例: {display_prefix}dset 6\n"
                f"重置: {display_prefix}dset reset\n"
            )
            return

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
            f"已将当前默认骰面数设为 d{new_sides}。后续 {display_prefix}r 将默认投掷 d{new_sides}。"
        )

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
        if not output.startswith(ROLL_ERROR_PREFIXES):
            await self._history.add(event, expression_str, output)
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
          /dset reset     清除默认骰子面数设置，恢复为全局默认
          /dset           查看当前会话的默认骰面数
        """
        raw_msg: str = event.message_str.strip()
        parts = raw_msg.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        async for msg in self._handle_dset(event, arg, display_prefix="/"):
            yield msg

    @filter.command("rprefix")
    async def rprefix_cmd(self, event: AstrMessageEvent) -> AsyncGenerator:
        """
        设置或查询当前会话的自定义骰子指令触发前缀。

        用法:
          /rprefix              查看当前会话的有效前缀
          /rprefix <前缀>       将当前会话触发前缀设为指定符号（如 . 或 !）
          /rprefix reset        清除会话前缀，恢复全局默认
        """
        raw_msg: str = event.message_str.strip()
        parts = raw_msg.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        key = f"custom_prefix:{event.unified_msg_origin}"

        # 查询当前前缀
        if not arg:
            current = await self._get_effective_prefix(event)
            if current:
                yield event.plain_result(
                    f"当前触发前缀：{current!r}\n"
                    f"用法：/rprefix <前缀> 设置前缀（如 /rprefix . 或 /rprefix !）\n"
                    f"重置：/rprefix reset"
                )
            else:
                yield event.plain_result(
                    "当前未设置自定义前缀，使用系统默认前缀\n"
                    "用法：/rprefix <前缀> 设置前缀（如 /rprefix . 或 /rprefix !）"
                )
            return

        # 权限检查
        if not await self._check_prefix_permission(event):
            if not self.allow_custom_prefix:
                yield event.plain_result("管理员已禁用自定义触发前缀功能。")
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

        # 重置会话前缀
        if arg.lower() in ("reset", "重置", "清除"):
            await self.delete_kv_data(key)
            self._set_prefix_cache(event.unified_msg_origin, None)
            if self.default_cmd_prefix:
                yield event.plain_result(
                    f"已清除自定义骰子前缀设置，恢复为默认前缀 {self.default_cmd_prefix!r}。"
                )
            else:
                yield event.plain_result("已清除自定义骰子前缀设置，使用默认前缀。")
            return

        # 前缀长度校验
        if len(arg) > 5:
            yield event.plain_result("前缀过长，建议使用 1~2 个字符（如 . 或 !!）。")
            return

        # 拒绝与系统命令前缀 '/' 相同的前缀——路由层明确忽略它，
        # 设置后自定义路由不会生效，形成"可设置但不可用"的逻辑陷阱。
        if arg == "/":
            yield event.plain_result(
                "前缀 '/' 与系统命令前缀冲突，设置后自定义路由不会生效，"
                "请选择其他符号（如 . ! ~ !!）。"
            )
            return

        # 前缀字符集校验：不允许空白字符或字母，避免路由歧义和误触发。
        if any(c.isspace() or c.isalpha() for c in arg):
            yield event.plain_result(
                "前缀不能包含空白字符或字母，请使用标点/符号（如 . ! ~ !! 等）。"
            )
            return

        await self.put_kv_data(key, arg)
        self._set_prefix_cache(event.unified_msg_origin, arg)
        yield event.plain_result(
            f"已将自定义骰子前缀设为 {arg!r}。\n"
            f"现在可用 {arg}r、{arg}roll、{arg}dset 等触发骰子功能，\n"
            f"也可继续使用 /r 等前缀。"
        )

    # ------------------------------------------------------------------
    # /rh 指令：投掷历史记录
    # ------------------------------------------------------------------

    async def _handle_rhistory(
        self, event: AstrMessageEvent, arg: str
    ) -> AsyncGenerator:
        """
        /rh 命令核心逻辑，由 rhistory_cmd 和 custom_prefix_route 统一调用。

        Args:
            event: 消息事件。
            arg: 去除命令名后的参数字符串（空字符串表示查询全部/默认模式）。
        """
        if not self.enable_history:
            yield event.plain_result("投掷历史记录功能未启用。")
            return

        arg_lower = arg.lower()

        # --- 清除历史 ---
        if arg_lower in ("clear", "清除", "清空"):
            # 群聊需白名单权限；私聊任何人均可清除自己的历史
            if not event.is_private_chat() and not await self._whitelist_check(event):
                yield event.plain_result("你没有权限清除群聊中的投掷历史记录。")
                return
            count = await self._history.clear(event)
            yield event.plain_result(
                f"已清空投掷历史，共删除 {count} 条记录。"
                if count
                else "当前暂无投掷历史记录。"
            )
            return

        # --- 查看历史（需检查查看权限）---
        if not self.allow_view_history:
            yield event.plain_result("管理员已禁用投掷历史查看功能。")
            return

        is_group = not event.is_private_chat()

        if arg_lower in ("me",):
            # 仅显示自己的记录（群聊过滤，私聊无差别）
            sender_id = str(event.get_sender_id())
            sender_name = str(event.get_sender_name())
            entries = await self._history.get_by_sender(event, sender_id)
            # 群聊中明确显示是谁的记录，避免歧义。
            title = f"{sender_name} 的投掷记录" if is_group else "我的投掷记录"
            text = RollHistoryManager.format_entries(
                entries, show_sender=False, title=title
            )
            yield event.plain_result(text)
            return

        # 默认：all 或空白
        entries = await self._history.get_all(event)
        # 群聊显示发送者昵称(ID)，便于区分同名用户；私聊不显示
        text = RollHistoryManager.format_entries(entries, show_sender=is_group)
        yield event.plain_result(text)

    @filter.command("rh", alias={"rhistory"})
    async def rhistory_cmd(self, event: AstrMessageEvent) -> AsyncGenerator:
        """
        查看或清除当前会话的投掷历史记录。

        用法:
          /rh           查看会话全部历史（群聊含发送者，私聊不显示）
          /rh all       同无参数
          /rh me        仅显示自己的投掷记录（群聊内使用）
          /rh clear     清空当前会话历史（群聊需白名单权限）
        """
        raw_msg: str = event.message_str.strip()
        parts = raw_msg.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        async for msg in self._handle_rhistory(event, arg):
            yield msg

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def custom_prefix_route(self, event: AstrMessageEvent) -> AsyncGenerator:
        """
        自定义触发前缀消息路由。

        读取会话或全局配置的自定义前缀，将 {prefix}r / {prefix}roll / {prefix}dset 等
        消息路由到对应的掷骰或骰面设置逻辑。
        """
        # 非文本或空消息无触发前缀可言，先过滤，避免对每条消息都查询缓存/KV。
        text = event.message_str.strip()
        if not text:
            return

        prefix = await self._get_effective_prefix(event)
        if not prefix:
            return
        # 当有效前缀等于 AstrBot 系统命令前缀（"/"）时，
        # @filter.command 装饰器已处理这些消息，此处不再路由，
        # 否则会触发重复响应。
        if prefix == "/":
            return

        text_lower = text.lower()
        p = prefix.lower()

        # --- 骰池指令匹配 ---
        for cmd_key in (f"{p}roll", f"{p}r"):
            if (
                text_lower == cmd_key
                or text_lower.startswith(cmd_key + " ")
                or text_lower.startswith(cmd_key + "\n")
            ):
                arg_part = text[len(cmd_key) :].strip()
                effective_sides = await self._get_effective_sides(event)
                expression_str = arg_part if arg_part else f"d{effective_sides}"
                output = self._do_roll(expression_str, default_sides=effective_sides)
                if not output.startswith(ROLL_ERROR_PREFIXES):
                    await self._history.add(event, expression_str, output)
                yield event.plain_result(output)
                event.stop_event()
                return

        # --- 骰面设置指令匹配 ---
        for cmd_key in (f"{p}dice_set", f"{p}dset"):
            if (
                text_lower == cmd_key
                or text_lower.startswith(cmd_key + " ")
                or text_lower.startswith(cmd_key + "\n")
            ):
                arg = text[len(cmd_key) :].strip()
                async for msg in self._handle_dset(event, arg, display_prefix=prefix):
                    yield msg
                event.stop_event()
                return
        # --- 历史记录指令匹配 ---
        for cmd_key in (f"{p}rhistory", f"{p}rh"):
            if (
                text_lower == cmd_key
                or text_lower.startswith(cmd_key + " ")
                or text_lower.startswith(cmd_key + "\n")
            ):
                arg = text[len(cmd_key) :].strip()
                async for msg in self._handle_rhistory(event, arg):
                    yield msg
                event.stop_event()
                return

    @filter.llm_tool(name="roll_dice")
    async def roll_dice_tool(
        self,
        event: AstrMessageEvent,
        expression: str,
        label: str = "",
    ) -> str:
        """
        在 TRPG/DnD 游戏中掷骰子。当需要进行攻击骰、伤害骰、属性检定、豁免或任何
        需要随机结果的场合时调用此工具。返回掷骰结果，由你将结果融入叙事后回复给用户。

        Args:
            expression(string): DnD/Roll20 标准骰池表达式，不含标签和 DC。
                常用格式：d20、1d20+5、4d6kh3、2d20kl1（劣势）、4dF（FATE 骰）、
                d6!（爆炸骰）、3d6>3（目标数成功计数）。
            label(string): 本次投掷的说明，不需要标签时传空字符串。
                含 DC 判定时格式为"说明 数字"（如 "感知 15"），
                掷骰总计 >= DC 时输出"成功"，否则"失败"。
        """
        # 将标签拼入表达式，交给解析器处理。
        if label:
            full_expr = f"{expression}#{label}"
        else:
            full_expr = expression

        effective_sides = await self._get_effective_sides(event)
        output = self._do_roll(full_expr, default_sides=effective_sides)

        # 将结果返回给 LLM，由 LLM 将骰点结果融入叙事后回复用户。
        if not output.startswith(ROLL_ERROR_PREFIXES):
            await self._history.add(event, full_expr, output)
        return output

    # ------------------------------------------------------------------
    # 插件卸载
    # ------------------------------------------------------------------

    async def terminate(self) -> None:
        logger.info("[dnd_dice] DnD D20 骰子插件已卸载。")
