from dataclasses import dataclass, field
from game.skills import Skill
from game.statuses import StatusInstance
from game.resistances import DEFAULT_RESISTANCES

SANITY_MIN = -45
SANITY_MAX = 45

SANITY_CLASH_WIN = 5
SANITY_CLASH_LOSS = 5
SANITY_PER_HEADS_UNOPPOSED = 1
SANITY_DRIFT_POSITIVE = 4
SANITY_DRIFT_NEGATIVE = 2

# Battle type -> display title + embed color. Spar is casual, Standard is
# a normal encounter, Fatal is a real stakes fight (Limbus naming).
BATTLE_TYPES = {
    "spar": {"title": "Spar", "color": 0x57F287},               # green
    "standard": {"title": "Proelium Commune", "color": 0xFEE75C},  # yellow
    "fatal": {"title": "Proelium Fatale", "color": 0xED4245},      # red
}


@dataclass
class Fighter:
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

    statuses: dict[str, StatusInstance] = field(default_factory=dict)

    @classmethod
    def from_character(cls, character, side: str) -> "Fighter":
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
        return 50 + self.sanity

    def apply_sanity_drift(self):
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
    battle_type: str = "spar"  # "spar", "standard", or "fatal"
    message_id: int | None = None  # the persistent battle-view post, edited in place

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