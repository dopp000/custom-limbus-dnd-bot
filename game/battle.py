from dataclasses import dataclass, field
from game.skills import Skill
from game.statuses import StatusInstance
from game.resistances import DEFAULT_RESISTANCES

# Sanity is the per-battle resource that biases a fighter's own coin
# flips. Always starts at 0 each battle (never carried over from a
# Character), clamped to this range.
SANITY_MIN = -45
SANITY_MAX = 45

# Sanity economy constants, all tunable.
SANITY_CLASH_WIN = 5
SANITY_CLASH_LOSS = 5
SANITY_PER_HEADS_UNOPPOSED = 1
SANITY_DRIFT_POSITIVE = 4   # positive Sanity drains toward 0 by up to this much each turn
SANITY_DRIFT_NEGATIVE = 2   # negative Sanity recovers toward 0 by up to this much each turn (slower)


@dataclass
class Fighter:
    """One combatant in a Battle. Pure data plus small helpers, no Discord code here."""

    name: str
    side: str
    hp: int = 100
    max_hp: int = 100
    sanity: int = 0
    speed: int = 10
    power: int = 6
    skill_slots: int = 3
    avatar_url: str | None = None
    resistances: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_RESISTANCES))

    skills: dict[str, Skill] = field(default_factory=dict)
    declared_skill: Skill | None = None
    declared_target: "Fighter | None" = None

    # Real status engine, keyed by status name.
    statuses: dict[str, StatusInstance] = field(default_factory=dict)

    @classmethod
    def from_character(cls, character, side: str) -> "Fighter":
        """Builds a Fighter pre-filled with a saved Character's stats,
        avatar, and resistances. Sanity is NOT pulled from the character,
        it always starts at 0 (the dataclass default) at the start of
        every battle regardless of past battles.

        Accepts anything with the right attributes (a game.character.Character,
        in practice). Deliberately not importing Character here, keeps
        game/battle.py from needing to know game/character.py exists.
        """
        return cls(
            name=character.name,
            side=side,
            hp=character.hp,
            max_hp=character.max_hp,
            speed=character.speed,
            power=character.power,
            avatar_url=character.avatar_url,
            resistances=dict(character.resistances),
        )

    def get_status(self, name: str) -> StatusInstance | None:
        return self.statuses.get(name)

    def set_status_instance(self, instance: StatusInstance):
        """Stores a status instance, or removes it entirely if its Count
        has reached 0 (no point keeping an empty, expired entry around).
        """
        if instance.count <= 0:
            self.statuses.pop(instance.name, None)
        else:
            self.statuses[instance.name] = instance

    def is_alive(self) -> bool:
        return self.hp > 0

    def take_damage(self, amount: int):
        self.hp = max(0, self.hp - amount)

    def gain_sanity(self, amount: int):
        self.sanity = min(SANITY_MAX, self.sanity + amount)

    def lose_sanity(self, amount: int):
        self.sanity = max(SANITY_MIN, self.sanity - amount)

    def heads_chance(self) -> int:
        """Percent chance this fighter's own coins land Heads, driven by
        Sanity. 50 baseline, shifted by Sanity. Since Sanity is always
        clamped to [-45, 45], this naturally stays within [5, 95].
        """
        return 50 + self.sanity

    def apply_sanity_drift(self):
        """Called at the end of a round (start_new_round). Positive
        Sanity drains toward 0, negative Sanity recovers toward 0, at
        different rates, and neither ever crosses past 0.
        """
        if self.sanity > 0:
            self.sanity = max(0, self.sanity - SANITY_DRIFT_POSITIVE)
        elif self.sanity < 0:
            self.sanity = min(0, self.sanity + SANITY_DRIFT_NEGATIVE)

    def add_skill(self, skill: Skill):
        self.skills[skill.name.lower()] = skill

    def get_skill(self, name: str) -> Skill | None:
        return self.skills.get(name.lower())

    def declare(self, skill: Skill, target: "Fighter") -> None:
        self.declared_skill = skill
        self.declared_target = target

    def clear_declaration(self):
        self.declared_skill = None
        self.declared_target = None

    def __str__(self):
        return f"{self.name} (HP {self.hp}/{self.max_hp}, Sanity {self.sanity}, Speed {self.speed})"


@dataclass
class Battle:
    """One active fight, scoped to a single Discord channel."""

    channel_id: int
    fighters: list[Fighter] = field(default_factory=list)
    round_number: int = 1
    started: bool = False

    def add_fighter(self, fighter: Fighter):
        self.fighters.append(fighter)

    def get_fighter(self, name: str) -> Fighter | None:
        for f in self.fighters:
            if f.name.lower() == name.lower():
                return f
        return None

    def side(self, side: str) -> list[Fighter]:
        return [f for f in self.fighters if f.side == side]

    def all_declared(self) -> bool:
        """True once every living fighter has locked in a skill for this round."""
        return all(
            f.declared_skill is not None
            for f in self.fighters
            if f.is_alive()
        )

    def start_new_round(self):
        self.round_number += 1
        for f in self.fighters:
            f.clear_declaration()
            f.apply_sanity_drift()

    def summary(self) -> str:
        lines = [f"**Round {self.round_number}**"]
        for side_name in ("A", "B"):
            lines.append(f"\n__Side {side_name}__")
            for f in self.side(side_name):
                status = "Down" if not f.is_alive() else str(f)
                lines.append(f"- {status}")
        return "\n".join(lines)