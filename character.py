"""
character.py — DnD 骰子插件的角色卡模块。

本模块定义了 DnD 角色卡的数据结构与管理器接口
（属性值、技能熟练度、豁免等）。

当前实现：load/save/delete 在内存 LRU 缓存中运行，不跨进程持久化。
未来版本将通过 AstrBot KV 存储实现持久化，并支持以下功能：
  - 命名掷骰快捷方式（如 "/r str" → 1d20+<力量修正>）
  - 按会话绑定角色
  - 从 D&D Beyond / Roll20 JSON 格式导入

注意（开发规范）：未来涉及本地文件读写时，必须通过
    from astrbot.api.star import StarTools
并调用 StarTools.get_data_dir() 获取 Path 对象进行文件读写，
严禁使用硬编码路径。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 数据：属性值
# ---------------------------------------------------------------------------

ABILITY_NAMES = ("str", "dex", "con", "int", "wis", "cha")

SKILLS: dict[str, str] = {
    # 技能名: 关联属性
    "acrobatics": "dex",
    "animal_handling": "wis",
    "arcana": "int",
    "athletics": "str",
    "deception": "cha",
    "history": "int",
    "insight": "wis",
    "intimidation": "cha",
    "investigation": "int",
    "medicine": "wis",
    "nature": "int",
    "perception": "wis",
    "performance": "cha",
    "persuasion": "cha",
    "religion": "int",
    "sleight_of_hand": "dex",
    "stealth": "dex",
    "survival": "wis",
}


@dataclass
class AbilityScores:
    """DnD 六项核心属性值。"""

    strength: int = 10
    dexterity: int = 10
    constitution: int = 10
    intelligence: int = 10
    wisdom: int = 10
    charisma: int = 10

    def __post_init__(self) -> None:
        """Clamp ability scores to the legal DnD range [1, 30]."""

        def _clamp(v: object) -> int:
            try:
                return max(1, min(30, int(v)))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 10

        self.strength = _clamp(self.strength)
        self.dexterity = _clamp(self.dexterity)
        self.constitution = _clamp(self.constitution)
        self.intelligence = _clamp(self.intelligence)
        self.wisdom = _clamp(self.wisdom)
        self.charisma = _clamp(self.charisma)

    def get(self, ability: str) -> int:
        """根据属性缩写（str/dex/con/int/wis/cha）返回对应属性值。"""
        mapping = {
            "str": self.strength,
            "dex": self.dexterity,
            "con": self.constitution,
            "int": self.intelligence,
            "wis": self.wisdom,
            "cha": self.charisma,
        }
        key = ability.lower()
        if key not in mapping:
            raise ValueError(f"未知属性: {ability!r}")
        return mapping[key]

    @staticmethod
    def modifier(score: int) -> int:
        """DnD 标准属性修正值公式：floor((score - 10) / 2)。"""
        return (score - 10) // 2


@dataclass
class CharacterSheet:
    """
    DnD 5e 角色卡。

    当前存根版本字段较少，接口设计保证未来版本扩展字段时不破坏调用方。
    """

    name: str = "未知冒险者"
    level: int = 1
    ability_scores: AbilityScores = field(default_factory=AbilityScores)
    # 熟练技能：SKILLS 中的技能名集合
    skill_proficiencies: set[str] = field(default_factory=set)
    # 豁免熟练：属性缩写集合
    save_proficiencies: set[str] = field(default_factory=set)
    # 自定义命名掷骰快捷方式：{"攻击": "1d20+5", "偷袭": "2d6"}
    named_rolls: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 将等级限制在 5e 合法范围 [1, 20] 内
        try:
            self.level = max(1, min(20, int(self.level)))
        except (TypeError, ValueError):
            self.level = 1
        # 将熟练集合统一转为小写，保证查询时大小写一致。
        # 用 or () 兜底：外部传入 None（插件生态中常见）时不触发 TypeError。
        self.skill_proficiencies = {s.lower() for s in (self.skill_proficiencies or ())}
        self.save_proficiencies = {s.lower() for s in (self.save_proficiencies or ())}

    @property
    def proficiency_bonus(self) -> int:
        """按角色等级计算的 5e 标准熟练加值。"""
        return 2 + (self.level - 1) // 4

    def get_ability_modifier(self, ability: str) -> int:
        """返回给定属性缩写的修正值。"""
        score = self.ability_scores.get(ability)
        return AbilityScores.modifier(score)

    def get_skill_modifier(self, skill: str) -> int:
        """
        返回技能检定的总修正值。
        包含属性修正值，若熟练则额外加上熟练加值。
        """
        skill = skill.lower()
        if skill not in SKILLS:
            raise ValueError(f"未知技能: {skill!r}")
        ability = SKILLS[skill]
        mod = self.get_ability_modifier(ability)
        if skill in self.skill_proficiencies:
            mod += self.proficiency_bonus
        return mod


# ---------------------------------------------------------------------------
# LRU 缓存辅助类
# ---------------------------------------------------------------------------

_CACHE_MAX_SIZE: int = 1024


class _BoundedLRUCache:
    """
    简单的有界 LRU 缓存，基于 OrderedDict 实现。

    当条目数达到 maxsize 时，自动递出最久未使用（LRU）的条目，
    避免长期运行的机器上内存无限增长。
    """

    def __init__(self, maxsize: int = _CACHE_MAX_SIZE) -> None:
        self._store: OrderedDict[str, CharacterSheet] = OrderedDict()
        # 强制最小値为 1；若 maxsize=0， set() 会立即在空 OrderedDict 上
        # 调用 popitem()，导致 KeyError。
        self._maxsize = max(1, maxsize)

    def get(self, key: str) -> CharacterSheet | None:
        """返回 *key* 对应的角色卡，并将其标记为最近使用。"""
        if key not in self._store:
            return None
        self._store.move_to_end(key)  # 刷新最近使用顺序
        return self._store[key]

    def set(self, key: str, value: CharacterSheet) -> None:
        """插入或更新 *key*，若容量已满则驱逐 LRU 条目。"""
        if key in self._store:
            self._store.move_to_end(key)
        else:
            if len(self._store) >= self._maxsize:
                self._store.popitem(last=False)  # 驱逐最久未使用条目
        self._store[key] = value

    def pop(self, key: str) -> None:
        """移除 *key*；若不存在则无操作。"""
        self._store.pop(key, None)


# ---------------------------------------------------------------------------
# 管理器接口
# ---------------------------------------------------------------------------


class CharacterManager:
    """
    管理用户/会话的角色卡。

    扩展点：接入 AstrBot KV 存储以实现跨会话持久化。
    构造时传入 Star 实例，以便实现后调用 star.put_kv_data / star.get_kv_data。

    使用方式（未来版本）：
        manager = CharacterManager(star=self)
        await manager.save(user_id, sheet)
        sheet = await manager.load(user_id)
    """

    def __init__(self, star: object | None = None) -> None:
        # star: Star 插件实例（用于 KV 持久化，未来使用）
        self._star = star
        # LRU 内存缓存：user_id -> CharacterSheet，容量有界避免内存泄漏
        self._cache: _BoundedLRUCache = _BoundedLRUCache()
        # 异步并发锁：保护 save/load/delete 的非原子操作序列，
        # 防止未来引入 await（如 KV 存储写入）时出现竞态条件。
        self._lock: asyncio.Lock = asyncio.Lock()

    async def load(self, user_id: str) -> CharacterSheet | None:
        """
        加载 user_id 对应的角色卡。

        当前实现：仅内存缓存，不跨会话持久化。
        未来实现：从 KV 存储反序列化。
        """
        async with self._lock:
            return self._cache.get(user_id)

    async def save(self, user_id: str, sheet: CharacterSheet) -> None:
        """
        持久化 user_id 对应的角色卡。

        当前实现：仅内存缓存，不跨会话持久化。
        未来实现：序列化到 KV 存储。
        """
        async with self._lock:
            self._cache.set(user_id, sheet)

    async def delete(self, user_id: str) -> None:
        """
        删除 user_id 对应的角色卡。

        当前实现：仅从内存缓存移除。
        未来实现：同时从 KV 存储删除。
        """
        async with self._lock:
            self._cache.pop(user_id)

    def get_cached(self, user_id: str) -> CharacterSheet | None:
        """返回内存缓存中的角色卡（不访问 KV 存储）。"""
        return self._cache.get(user_id)
