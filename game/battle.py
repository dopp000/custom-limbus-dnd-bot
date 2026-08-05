from dataclasses import dataclass, field
from game.skills import Skill


@dataclass
class Fighter:
    """One combatant in a Battle. Pure data plus small helpers, no Discord code here."""

    name: str
    side: str
    hp: int = 100
    max_hp: int = 100
    sp: int = 15
    speed: int = 10
    skill_slots: int = 3

    skills: dict[str, Skill] = field(default_factory=dict)
    declared_skill: Skill | None = None
    declared_target: "Fighter | None" = None

    # Temporary: tracks a single "primary" status name, only for picking the
    # embed border color right now. The real Count/Potency status engine
    # replaces this later, this is just a stand-in so we have something
    # real to test the visual layer against.
    active_status: str | None = None

    def set_status(self, status_name: str | None):
        self.active_status = status_name

    def is_alive(self) -> bool:
        return self.hp > 0

    def take_damage(self, amount: int):
        self.hp = max(0, self.hp - amount)

    def gain_sp(self, amount: int):
        self.sp += amount

    def spend_sp(self, amount: int) -> bool:
        if self.sp < amount:
            return False
        self.sp -= amount
        return True

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
        return f"{self.name} (HP {self.hp}/{self.max_hp}, SP {self.sp}, Speed {self.speed})"


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

    def summary(self) -> str:
        lines = [f"Round {self.round_number}"]
        for side_name in ("A", "B"):
            lines.append(f"\nSide {side_name}")
            for f in self.side(side_name):
                status = "Down" if not f.is_alive() else str(f)
                lines.append(f"- {status}")
        return "\n".join(lines)