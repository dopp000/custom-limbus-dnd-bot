import random
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

BATTLE_TYPES = {
    "spar": {"title": "Spar", "color": 0x57F287},
    "standard": {"title": "Proelium Commune", "color": 0xFEE75C},
    "fatal": {"title": "Proelium Fatale", "color": 0xED4245},
}


@dataclass
class DeclaredAction:
    """One skill+target pairing occupying one of a fighter's skill slots,
    explicitly aimed at one specific slot on the target.

    slot is the CASTER's own slot number (1-based) this action lives in.
    target_slot is which of the TARGET's slots this action is aimed at.
    A real Clash only happens if the target's own action in target_slot
    points back at (this caster, slot) -- see combat() in cogs/battle.py.
    """
    skill: Skill
    target: "Fighter"
    slot: int
    target_slot: int


@dataclass
class Fighter:
    name: str
    side: str
    hp: int = 100
    max_hp: int = 100
    sanity: int = 0
    speed: int = 10  # legacy flat speed, kept as the default for speed_min/max below
    power: int = 6
    skill_slots: int = 3
    avatar_url: str | None = None
    resistances: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_RESISTANCES))

    # The range each of this fighter's skill slots rolls its own Speed
    # from, once per round (e.g. 4-7). Defaults to a constant range equal
    # to `speed`, so a fighter with no explicit range set just behaves
    # like every slot has the same fixed speed, matching the old
    # single-speed behavior. A real per-character range (set via
    # /character edit) is a follow-up, not wired in yet.
    speed_min: int | None = None
    speed_max: int | None = None

    # One rolled speed per skill slot, independent of whatever skill (if
    # any) ends up assigned to that slot. Rolled fresh at Fighter creation
    # and at the start of every round. This is what determines Clash/
    # unopposed resolution order now, not the flat `speed` stat above.
    slot_speeds: list[int] = field(default_factory=list)

    skills: dict[str, Skill] = field(default_factory=dict)

    # Keyed by the CASTER's own slot number (1-based), not a plain list,
    # so a specific slot can be individually re-declared (moved) or
    # cancelled (undeclare) without disturbing the others.
    declared_actions: dict[int, DeclaredAction] = field(default_factory=dict)

    statuses: dict[str, StatusInstance] = field(default_factory=dict)

    def __post_init__(self):
        if self.speed_min is None:
            self.speed_min = self.speed
        if self.speed_max is None:
            self.speed_max = self.speed
        if not self.slot_speeds:
            self.roll_slot_speeds()

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

    def roll_slot_speeds(self):
        """Rerolls every skill slot's own Speed within [speed_min, speed_max].
        Called on creation and again at the start of every round -- slot
        speed is independent of whatever skill later gets assigned to it.
        """
        low, high = self.speed_min, self.speed_max
        if high < low:
            low, high = high, low
        self.slot_speeds = [
            random.randint(low, high) if high > low else low
            for _ in range(self.skill_slots)
        ]

    def slot_speed(self, slot: int) -> int:
        """1-based slot lookup. Returns 0 for an out-of-range slot rather
        than raising, since this gets called from places that only
        loosely validate slot numbers first.
        """
        if 1 <= slot <= len(self.slot_speeds):
            return self.slot_speeds[slot - 1]
        return 0

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

    def declare_in_slot(self, slot: int, skill: Skill, target: "Fighter", target_slot: int) -> bool:
        """Fills (or overwrites/moves) one specific skill slot.

        Returns False only if the slot number itself is out of range.
        Unlike the old declare(), this deliberately allows re-declaring
        an already-filled slot -- that's how "move a skill to a
        different slot" and "swap which skill is in this slot" both
        work: undeclare the old one (or just overwrite it here) and
        declare_in_slot the new one.
        """
        if not (1 <= slot <= self.skill_slots):
            return False
        self.declared_actions[slot] = DeclaredAction(
            skill=skill, target=target, slot=slot, target_slot=target_slot
        )
        return True

    def undeclare(self, slot: int) -> bool:
        """Clears one slot's declaration. Returns False if that slot
        wasn't declared in the first place.
        """
        return self.declared_actions.pop(slot, None) is not None

    def get_declared_skill_in_slot(self, slot: int) -> Skill | None:
        action = self.declared_actions.get(slot)
        return action.skill if action else None

    def has_declared(self) -> bool:
        return len(self.declared_actions) > 0

    def slots_filled(self) -> int:
        return len(self.declared_actions)

    def clear_declaration(self):
        self.declared_actions = {}

    def __str__(self):
        return f"{self.name} (HP {self.hp}/{self.max_hp}, Sanity {self.sanity}, Speed {self.speed})"


@dataclass
class Battle:
    """One active fight, scoped to a single Discord channel."""

    channel_id: int
    fighters: list[Fighter] = field(default_factory=list)
    round_number: int = 1
    started: bool = False
    battle_type: str = "spar"
    message_id: int | None = None

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
        """True once every living fighter has used at least one skill slot."""
        return all(
            f.has_declared()
            for f in self.fighters
            if f.is_alive()
        )

    def start_new_round(self):
        self.round_number += 1
        for f in self.fighters:
            f.clear_declaration()
            f.apply_sanity_drift()
            f.roll_slot_speeds()

    def summary(self) -> str:
        lines = [f"**Round {self.round_number}**"]
        for side_name in ("A", "B"):
            lines.append(f"\n__Side {side_name}__")
            for f in self.side(side_name):
                status = "Down" if not f.is_alive() else str(f)
                lines.append(f"- {status}")
        return "\n".join(lines)