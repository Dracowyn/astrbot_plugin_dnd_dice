"""
character.py — DnD 骰子插件的角色卡模块（当前版本为存根）。

本模块定义了 DnD 角色卡的数据结构与管理器接口
（属性值、技能熟练度、豁免等）。

当前版本：仅存根，所有数据操作均抛出 NotImplementedError。
未来版本将通过 AstrBot KV 存储实现持久化，并支持以下功能：
  - 自动计算属性修正值
  - 熟练加值应用
  - 命名掷骰快捷方式（如 "/r str" → 1d20+<力量修正>）
  - 按会话绑定角色
  - 从 D&D Beyond / Roll20 JSON 格式导入
"""

from __future__ import annotations

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
        for attr in (
            "strength",
            "dexterity",
            "constitution",
            "intelligence",
            "wisdom",
            "charisma",
        ):
            raw = getattr(self, attr)
            try:
                clamped = max(1, min(30, int(raw)))
            except (TypeError, ValueError):
                clamped = 10
            object.__setattr__(self, attr, clamped)

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
        # Clamp level to the legal 5e range [1, 20]
        try:
            self.level = max(1, min(20, int(self.level)))
        except (TypeError, ValueError):
            self.level = 1
        # Normalize proficiency sets to lowercase for consistent lookup
        self.skill_proficiencies = {s.lower() for s in self.skill_proficiencies}
        self.save_proficiencies = {s.lower() for s in self.save_proficiencies}

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
        # 内存缓存：user_id -> CharacterSheet
        self._cache: dict[str, CharacterSheet] = {}

    async def load(self, user_id: str) -> CharacterSheet | None:
        """
        加载 user_id 对应的角色卡。

        当前实现：仅内存缓存，不跨会话持久化。
        未来实现：从 KV 存储反序列化。
        """
        return self._cache.get(user_id)

    async def save(self, user_id: str, sheet: CharacterSheet) -> None:
        """
        持久化 user_id 对应的角色卡。

        当前实现：仅内存缓存，不跨会话持久化。
        未来实现：序列化到 KV 存储。
        """
        self._cache[user_id] = sheet

    async def delete(self, user_id: str) -> None:
        """
        删除 user_id 对应的角色卡。

        当前实现：仅从内存缓存移除。
        未来实现：同时从 KV 存储删除。
        """
        self._cache.pop(user_id, None)

    def get_cached(self, user_id: str) -> CharacterSheet | None:
        """返回内存缓存中的角色卡（不访问 KV 存储）。"""
        return self._cache.get(user_id)
