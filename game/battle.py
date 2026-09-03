import random
from dataclasses import dataclass, field
from game.skills import Skill
from game.statuses import StatusInstance
from game.resistances import DEFAULT_RESISTANCES

SANITY_MIN = -45
SANITY_MAX = 45

# Clash win is +2 SP PER COIN in the winner's winning skill (variable,
# not flat -- see SANITY_PER_COIN_CLASH_WIN below, applied against
# outcome.winner_final_result.skill.coins by the caller in
# cogs/battle.py). Clash loss and the unopposed-Heads bonus stay flat.
SANITY_PER_COIN_CLASH_WIN = 2
SANITY_CLASH_LOSS = 3
SANITY_PER_HEADS_UNOPPOSED = 2
SANITY_DRIFT_POSITIVE = 4
SANITY_DRIFT_NEGATIVE = 2

BATTLE_TYPES = {
    "spar": {"title": "Spar", "color": 0x57F287},
    "standard": {"title": "Proelium Commune", "color": 0xFEE75C},
    "fatal": {
        "title": "Proelium Fatale",
        "color": 0xED4245,
        "image": "https://cdn.discordapp.com/attachments/670849939294912545/1542197225252462702/ProeliumFatale.gif",
    },
}


@dataclass
class DeclaredAction:
    """One skill+target pairing occupying one of a fighter's skill slots,
    explicitly aimed at one specific slot on the target.

    slot is the CASTER's own slot number (1-based) this action lives in.
    target_slot is which of the TARGET's slots this action is aimed at.
    A real Clash only happens if the target's own action in target_slot
    points back at (this caster, slot) -- see combat() in cogs/battle.py.

    target and target_slot are deliberately mutable (not frozen), since a
    speed-priority clash steal can silently redirect an already-declared
    action's target onto a different enemy after the fact, without the
    original caster knowing beforehand. See Battle.find_mutual_clash_partner
    and StealApprovalView in cogs/battle.py.
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

    # Discord user ID of whoever controls this fighter. Set from
    # Character.owner_id for saved characters, or from whoever ran
    # /battle addfighter for a one-off NPC. Used to DM this fighter's
    # controller for things they need to privately approve, like a
    # clash-steal request, since ephemeral replies only reach whoever
    # is actually running the current slash command.
    owner_id: int | None = None

    # The range each of this fighter's skill slots rolls its own Speed
    # from, once per round (e.g. 4-7). Defaults to a constant range equal
    # to `speed`, so a fighter with no explicit range set just behaves
    # like every slot has the same fixed speed, matching the old
    # single-speed behavior. Real per-character ranges (set via
    # /character edit) are pulled in by from_character below.
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

    # Round-scoped single-use flags for the two Counter-family Skill
    # flags ([Counter] and [Clashable Counter], see SKILL_FLAG_TAGS in
    # game/conditions.py). Both reset every round in clear_declaration
    # below. See find_eligible_counter / find_eligible_clashable_counter
    # / apply_counter_redirects in cogs/battle.py for how these get set.
    counter_used_this_round: bool = False
    clashable_counter_used_this_round: bool = False

    def __post_init__(self):
        if self.speed_min is None:
            self.speed_min = self.speed
        if self.speed_max is None:
            self.speed_max = self.speed
        if not self.slot_speeds:
            self.roll_slot_speeds()

    @classmethod
    def from_character(cls, character, side: str) -> "Fighter":
        fighter = cls(
            name=character.name,
            side=side,
            hp=character.hp,
            max_hp=character.max_hp,
            speed=character.speed,
            power=character.power,
            avatar_url=character.avatar_url,
            resistances=dict(character.resistances),
            owner_id=character.owner_id,
        )
        # If the saved character has its own speed range (set via
        # /character edit), it takes over from the flat speed default
        # that __post_init__ already applied above, and slots are
        # rerolled against the real range. /battle addfighter's own
        # speed_min/speed_max params still override this afterward if
        # the host passes them, since that runs after this returns.
        if character.speed_min is not None:
            fighter.speed_min = character.speed_min
            fighter.speed_max = (
                character.speed_max if character.speed_max is not None else character.speed_min
            )
            fighter.roll_slot_speeds()
        return fighter

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
        self.counter_used_this_round = False
        self.clashable_counter_used_this_round = False

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

    def find_mutual_clash_partner(self, target: Fighter, target_slot: int) -> tuple[Fighter, int] | None:
        """Checks whether target's target_slot is currently locked in a
        real mutual clash with some other fighter: that fighter's own
        declared action targets exactly target's target_slot, AND
        target's action in target_slot targets exactly that fighter's
        slot back.

        Returns (fighter, slot) of that clash partner, or None if
        target_slot isn't currently in a genuine mutual clash (nothing
        declared there yet, or only a one-sided declare so far).

        Used to detect a clash-steal situation: if a third fighter also
        wants target's target_slot, and this returns a partner on that
        third fighter's own side, the partner is the ally who'd have to
        approve giving up the clash. See StealApprovalView in
        cogs/battle.py.
        """
        target_action = target.declared_actions.get(target_slot)
        if target_action is None:
            return None
        partner = target_action.target
        partner_slot = target_action.target_slot
        partner_action = partner.declared_actions.get(partner_slot)
        if partner_action is None:
            return None
        if partner_action.target is target and partner_action.target_slot == target_slot:
            return partner, partner_slot
        return None

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