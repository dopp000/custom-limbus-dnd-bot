from dataclasses import dataclass, field


@dataclass
class Fighter:
    """test."""

    name: str
    side: str          # "A" or "B" which team they're on
    hp: int = 100
    max_hp: int = 100
    sp: int = 15
    speed: int = 10
    skill_slots: int = 3

    # Filled in during the Declare Phase, cleared at the start of each round.
    # We'll build the actual skill-declaring command in the next lesson —
    # for now this just holds a plain string as a placeholder.
    declared_action: str | None = None
    declared_target: "Fighter | None" = None

    def is_alive(self) -> bool:
        return self.hp > 0

    def take_damage(self, amount: int):
        self.hp = max(0, self.hp - amount)

    def gain_sp(self, amount: int):
        self.sp += amount

    def spend_sp(self, amount: int) -> bool:
        """Returns False (and spends nothing) if the fighter can't afford it."""
        if self.sp < amount:
            return False
        self.sp -= amount
        return True

    def clear_declaration(self):
        self.declared_action = None
        self.declared_target = None

    def __str__(self):
        return f"{self.name} (HP {self.hp}/{self.max_hp}, SP {self.sp}, Speed {self.speed})"


@dataclass
class Battle:
    """One active fight scoped to a single channel."""

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
        """True once every living fighter has locked in an action for this round."""
        return all(
            f.declared_action is not None
            for f in self.fighters
            if f.is_alive()
        )

    def start_new_round(self):
        self.round_number += 1
        for f in self.fighters:
            f.clear_declaration()

    def summary(self) -> str:
        lines = [f"**Round {self.round_number}**"]
        for side_name in ("A", "B"):
            lines.append(f"\n__Side {side_name}__")
            for f in self.side(side_name):
                status = "Dead" if not f.is_alive() else str(f)
                lines.append(f"- {status}")
        return "\n".join(lines)