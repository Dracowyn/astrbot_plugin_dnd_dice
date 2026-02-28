"""
history.py — DnD 骰子插件的投掷历史管理模块。

提供会话级投掷历史，通过 AstrBot 的 KV 存储持久化。
记录按 unified_msg_origin（群聊或私聊会话）分别存储。
群聊场景支持查询全员记录或按发送者过滤。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.star import Star

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 投掷输出以这些前缀开头时视为失败投掷，不写入历史记录。
ROLL_ERROR_PREFIXES: tuple[str, ...] = (
    "解析错误:",
    "掷骰错误:",
    "掷骰完成，但格式化时发生内部错误",
)

# KV 存储已按 plugin_id 做插件级命名空间隔离（见 PluginKVStoreMixin），
# 此前缀仅用于在本插件内部区分不同功能的 key（与 session_sides:、custom_prefix: 等区分），
# 不存在与其他插件产生 key 冲突的风险。
_KV_PREFIX = "history:"

# 单次查询最多显示的条数。
_DISPLAY_LIMIT = 20

# 存储的结果摘要最大字符数。
_RESULT_SUMMARY_MAX = 100


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class HistoryEntry:
    """单条投掷历史记录。"""

    expr: str  # 投掷表达式
    result: str  # 结果首行，截断至 _RESULT_SUMMARY_MAX 字符
    sender_id: str  # 发送者 ID
    sender_name: str  # 发送者昵称
    ts: str  # 时间戳，格式：MM-DD HH:MM:SS

    # ------------------------------------------------------------------
    # 序列化辅助方法
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """将记录转换为可写入 KV 存储的字典。"""
        return {
            "expr": self.expr,
            "result": self.result,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> HistoryEntry:
        """从 KV 存储读取的字典中还原记录。"""
        return cls(
            expr=str(data.get("expr", "")),
            result=str(data.get("result", "")),
            sender_id=str(data.get("sender_id", "")),
            sender_name=str(data.get("sender_name", "")),
            ts=str(data.get("ts", "")),
        )

    @classmethod
    def build(
        cls,
        event: AstrMessageEvent,
        expr: str,
        result: str,
    ) -> HistoryEntry:
        """从当前消息事件和投掷结果构造一条新记录。"""
        # 取结果的第一个非空行，超长时截断。
        first_line = next(
            (line for line in result.splitlines() if line.strip()),
            result,
        ).strip()
        if len(first_line) > _RESULT_SUMMARY_MAX:
            first_line = first_line[:_RESULT_SUMMARY_MAX] + "…"

        ts = datetime.now().strftime("%m-%d %H:%M:%S")  # noqa: DTZ005

        return cls(
            expr=expr,
            result=first_line,
            sender_id=str(event.get_sender_id()),
            sender_name=str(event.get_sender_name()),
            ts=ts,
        )


# ---------------------------------------------------------------------------
# 历史管理器
# ---------------------------------------------------------------------------


class RollHistoryManager:
    """基于 AstrBot KV 存储的会话级投掷历史管理器。"""

    def __init__(self, star: Star, max_count: int, enabled: bool = True) -> None:
        self._star = star
        self._max_count: int = max(1, max_count)
        self._enabled: bool = enabled

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def add(
        self,
        event: AstrMessageEvent,
        expr: str,
        result: str,
    ) -> None:
        """将一次成功的投掷追加到会话历史。

        若历史功能已禁用，或 result 表示投掷失败，则静默忽略。
        所有异常均被捕获，确保历史写入失败不影响投掷结果的正常响应。
        """
        if not self._enabled:
            return
        # 失败投掷不写入历史。
        if result.startswith(ROLL_ERROR_PREFIXES):
            return

        try:
            key = _KV_PREFIX + event.unified_msg_origin
            raw = await self._star.get_kv_data(key, [])
            if not isinstance(raw, list):
                raw = []

            entry = HistoryEntry.build(event, expr, result)
            raw.append(entry.to_dict())

            # 超出上限时，保留最新的 _max_count 条，丢弃最旧的记录。
            if len(raw) > self._max_count:
                raw = raw[-self._max_count :]

            await self._star.put_kv_data(key, raw)
        except (OSError, ValueError, TypeError, RuntimeError) as e:
            # 历史记录属于非核心功能，写入失败只记录警告，不中断正常投掷响应。
            logger.warning(f"[dnd_dice] 写入投掷历史失败: {e}")
        except Exception as e:
            # 兜底捕获 KV 存储可能抛出的未知异常，同样只记录警告。
            logger.warning(f"[dnd_dice] 写入投掷历史时发生未预期异常: {e}")

    async def get_all(self, event: AstrMessageEvent) -> list[HistoryEntry]:
        """返回当前会话的全部历史记录。"""
        try:
            key = _KV_PREFIX + event.unified_msg_origin
            raw = await self._star.get_kv_data(key, [])
            if not isinstance(raw, list):
                return []
            return [HistoryEntry.from_dict(d) for d in raw if isinstance(d, dict)]
        except (OSError, ValueError, TypeError, RuntimeError) as e:
            logger.warning(f"[dnd_dice] 读取投掷历史失败: {e}")
            return []
        except Exception as e:
            logger.warning(f"[dnd_dice] 读取投掷历史时发生未预期异常: {e}")
            return []

    async def get_by_sender(
        self,
        event: AstrMessageEvent,
        sender_id: str,
    ) -> list[HistoryEntry]:
        """返回当前会话中指定发送者的历史记录。"""
        entries = await self.get_all(event)
        return [e for e in entries if e.sender_id == sender_id]

    async def clear(self, event: AstrMessageEvent) -> int:
        """清空当前会话的全部历史记录，返回被删除的条数。"""
        try:
            key = _KV_PREFIX + event.unified_msg_origin
            raw = await self._star.get_kv_data(key, [])
            count = len(raw) if isinstance(raw, list) else 0
            await self._star.delete_kv_data(key)
            return count
        except (OSError, ValueError, TypeError, RuntimeError) as e:
            logger.warning(f"[dnd_dice] 清空投掷历史失败: {e}")
            return 0
        except Exception as e:
            logger.warning(f"[dnd_dice] 清空投掷历史时发生未预期异常: {e}")
            return 0

    # ------------------------------------------------------------------
    # 格式化辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def format_entries(
        entries: list[HistoryEntry],
        show_sender: bool,
        title: str | None = None,
    ) -> str:
        """将记录列表渲染为人类可读的纯文本字符串。

        最多显示 _DISPLAY_LIMIT 条最新记录。
        show_sender 为 True 时（群聊模式）每行包含发送者昵称。
        title 非空时替代默认的「投掷历史」作为标题前缀。
        """
        if not entries:
            return "暂无投掷历史记录。"

        # 取最新的若干条。
        visible = entries[-_DISPLAY_LIMIT:]
        lines: list[str] = []
        for i, entry in enumerate(visible, start=1):
            if show_sender:
                # 群聊全员模式：仅显示昵称。
                lines.append(
                    f"{i}. [{entry.ts}] {entry.sender_name}: "
                    f"{entry.expr} → {entry.result}"
                )
            else:
                lines.append(f"{i}. [{entry.ts}] {entry.expr} → {entry.result}")

        total = len(entries)
        # 优先使用调用方传入的自定义标题，否则使用通用标题。
        label = title if title else "投掷历史"
        header = (
            f"{label}（共 {total} 条，显示最新 {len(visible)} 条）："
            if total > len(visible)
            else f"{label}（共 {total} 条）："
        )
        return header + "\n" + "\n".join(lines)
