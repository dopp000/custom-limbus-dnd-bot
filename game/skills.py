import random
from dataclasses import dataclass, field, replace

from game.conditions import Trigger, TriggerContext, evaluate_condition


@dataclass
class Skill:
    """A single skill definition: just the numbers, no Discord code."""

    name: str
    base_power: int
    coin_power: int
    coins: int
    damage_type: str = "blunt"  # "slash", "blunt", or "pierce"

    # Per-coin status effects, one entry per coin (aligned by index).
    # coin_statuses[i] is None if that coin inflicts nothing. Potency/
    # count are the RAW values before resistance, resistance gets applied
    # by the caller (apply_incoming_hit in cogs/battle.py) at hit time.
    # Status names are not restricted to a fixed list, this same shape
    # covers keyword statuses (Rupture, Bleed, ...) and non-keyword ones
    # (Fragile, Bind, Power Down, ...) equally.
    coin_statuses: list[str | None] = field(default_factory=list)
    coin_status_potencies: list[int] = field(default_factory=list)
    coin_status_counts: list[int] = field(default_factory=list)

    # Skill-level Conditional Triggers, independent of the per-coin
    # status system above. Evaluated once per resolution via
    # resolve_triggers below, not per coin, since a trigger's condition
    # (target's HP, caster's Sanity, etc.) doesn't change coin to coin
    # within a single resolution.
    triggers: list[Trigger] = field(default_factory=list)

    def __post_init__(self):
        # Pads status lists up to `coins` entries if a Skill gets built
        # without explicitly passing them (quick construction, tests),
        # so nothing that indexes into these lists breaks.
        while len(self.coin_statuses) < self.coins:
            self.coin_statuses.append(None)
        while len(self.coin_status_potencies) < self.coins:
            self.coin_status_potencies.append(0)
        while len(self.coin_status_counts) < self.coins:
            self.coin_status_counts.append(0)


@dataclass
class CoinResult:
    """The outcome of a single coin toss within a skill's resolution."""

    heads: bool
    power_after: int
    damage_dealt: int


@dataclass
class SkillResult:
    """The full outcome of resolving one skill: every coin, plus the total."""

    skill: Skill
    coin_results: list[CoinResult]

    @property
    def total_damage(self) -> int:
        return sum(c.damage_dealt for c in self.coin_results)

    @property
    def final_power(self) -> int:
        """The Power reached after the last coin. This is what Clashes compare."""
        return self.coin_results[-1].power_after

    def log(self) -> str:
        """A human-readable breakdown, useful for both testing and Discord output."""
        lines = [f"**{self.skill.name}** (Base {self.skill.base_power}, +{self.skill.coin_power} Coin Power, {self.skill.coins} coins)"]
        for i, c in enumerate(self.coin_results, start=1):
            face = "Heads" if c.heads else "Tails"
            status_note = ""
            if i - 1 < len(self.skill.coin_statuses) and self.skill.coin_statuses[i - 1]:
                name = self.skill.coin_statuses[i - 1]
                potency = self.skill.coin_status_potencies[i - 1]
                count = self.skill.coin_status_counts[i - 1]
                status_note = f", inflicts {name} {potency}/{count}"
            lines.append(f"  Coin {i}: {face}, Power {c.power_after}, {c.damage_dealt} damage{status_note}")
        lines.append(f"  **Total: {self.total_damage} damage**")
        return "\n".join(lines)


def flip_coin(heads_chance: int = 50) -> bool:
    """Rolls a percentage-based coin flip. heads_chance is 0-100, the
    percent chance of landing Heads. Defaults to a fair 50/50 so every
    existing caller that doesn't pass this stays behaves exactly as
    before.
    """
    roll = random.randint(1, 100)
    return roll <= heads_chance


def resolve_skill(skill: Skill, heads_chance: int = 50) -> SkillResult:
    """Resolves a skill's coins one at a time, in sequence.

    Every coin lands a hit at whatever Power has been built up so far.
    A Heads permanently raises Power (by coin_power) for every hit after it,
    including its own. A Tails deals a hit too, just without raising Power.

    heads_chance is this skill's OWN caster's Sanity-driven odds (see
    Fighter.heads_chance in game/battle.py), defaulting to a fair 50/50.
    """
    power = skill.base_power
    results: list[CoinResult] = []

    for _ in range(skill.coins):
        heads = flip_coin(heads_chance)
        if heads:
            power += skill.coin_power
        results.append(CoinResult(heads=heads, power_after=power, damage_dealt=power))

    return SkillResult(skill=skill, coin_results=results)


def resolve_triggers(skill: Skill, context: TriggerContext) -> tuple[Skill, list[Trigger]]:
    """Evaluates every trigger on a skill against the current context.

    Pre-roll effects (bonus_power, bonus_coin_power) are baked into a
    modified copy of the skill immediately, since they have to apply
    before coins are ever tossed -- this is why it returns a (possibly
    new) Skill rather than mutating in place. Post-hit effects
    (inflict_status, sanity_gain) are NOT applied here, they're only
    evaluated and returned as "fired", so the caller can apply them
    alongside normal hit resolution (see apply_trigger_effects in
    cogs/battle.py) only if the skill actually lands -- a losing side of
    a clash never hits, so its post-hit triggers should never fire even
    though its pre-roll bonuses still legitimately affected the clash
    math.
    """
    fired = [t for t in skill.triggers if evaluate_condition(t.condition, context)]

    bonus_power = sum(t.effect_value for t in fired if t.effect_type == "bonus_power")
    bonus_coin_power = sum(t.effect_value for t in fired if t.effect_type == "bonus_coin_power")

    if bonus_power or bonus_coin_power:
        skill = replace(
            skill,
            base_power=skill.base_power + bonus_power,
            coin_power=skill.coin_power + bonus_coin_power,
        )

    post_hit = [t for t in fired if t.effect_type in ("inflict_status", "sanity_gain")]
    return skill, post_hit


def resolve_clash(result_a: SkillResult, result_b: SkillResult) -> SkillResult | None:
    """Compares two SkillResults by final_power.

    Returns the winning SkillResult (whose total_damage should be applied to
    the loser), or None on a tie. You decide how ties get handled at the
    call site, since that is a rules decision, not a math one.

    Superseded by resolve_round_clash below for actual gameplay, kept
    here since it's simple and still useful for quick comparisons.
    """
    if result_a.final_power > result_b.final_power:
        return result_a
    elif result_b.final_power > result_a.final_power:
        return result_b
    else:
        return None


@dataclass
class PairwiseClashOutcome:
    """The result of the earlier per-coin-index clash model. Superseded by
    the round-based attrition model below, kept for reference.
    """
    winner: str  # "a" or "b"
    result_a: SkillResult
    result_b: SkillResult
    a_survived: list[bool]
    b_survived: list[bool]
    rerolls: int

    def winner_result(self) -> SkillResult:
        return self.result_a if self.winner == "a" else self.result_b

    def loser_result(self) -> SkillResult:
        return self.result_b if self.winner == "a" else self.result_a

    def winner_survived(self) -> list[bool]:
        return self.a_survived if self.winner == "a" else self.b_survived

    def total_damage(self) -> int:
        result = self.winner_result()
        survived = self.winner_survived()
        return sum(c.damage_dealt for c, s in zip(result.coin_results, survived) if s)


def resolve_pairwise_clash(skill_a: Skill, skill_b: Skill, max_rerolls: int = 50) -> PairwiseClashOutcome:
    """The earlier per-coin-index clash model. Superseded by
    resolve_round_clash below, kept here for reference.
    """
    rerolls = 0
    while True:
        result_a = resolve_skill(skill_a)
        result_b = resolve_skill(skill_b)

        len_a = len(result_a.coin_results)
        len_b = len(result_b.coin_results)
        max_len = max(len_a, len_b)

        a_survived = [True] * len_a
        b_survived = [True] * len_b

        for i in range(max_len):
            if i >= len_a or i >= len_b:
                continue
            power_a = result_a.coin_results[i].power_after
            power_b = result_b.coin_results[i].power_after
            if power_a > power_b:
                b_survived[i] = False
            elif power_b > power_a:
                a_survived[i] = False

        a_total = sum(c.power_after for c, s in zip(result_a.coin_results, a_survived) if s)
        b_total = sum(c.power_after for c, s in zip(result_b.coin_results, b_survived) if s)

        if a_total == b_total:
            rerolls += 1
            if rerolls >= max_rerolls:
                raise RuntimeError(
                    f"Clash failed to resolve after {max_rerolls} rerolls, "
                    "something is almost certainly wrong with the input skills."
                )
            continue

        winner = "a" if a_total > b_total else "b"
        return PairwiseClashOutcome(
            winner=winner,
            result_a=result_a,
            result_b=result_b,
            a_survived=a_survived,
            b_survived=b_survived,
            rerolls=rerolls,
        )


@dataclass
class ClashRound:
    """One round of clash attrition: both sides tossed all their currently
    remaining coins fresh, and whichever side's final Power was lower
    loses exactly one coin (permanently) heading into the next round.
    """
    result_a: SkillResult
    result_b: SkillResult
    coins_a_before: int
    coins_b_before: int
    loser: str | None  # "a", "b", or None on a tie (nobody loses a coin this round)


@dataclass
class ClashOutcome:
    """The full outcome of a round-based attrition clash: every attrition
    round that happened, plus the winner's final one-sided damage toss
    (a completely fresh roll using whatever coins they had left).
    """
    winner: str  # "a" or "b"
    rounds: list[ClashRound]
    winner_final_result: SkillResult

    def total_damage(self) -> int:
        return self.winner_final_result.total_damage


def resolve_round_clash(
    skill_a: Skill,
    skill_b: Skill,
    heads_chance_a: int = 50,
    heads_chance_b: int = 50,
    max_rounds: int = 100,
) -> ClashOutcome:
    """Resolves a clash via round-by-round coin attrition.

    Each round, both sides toss ALL of their currently-remaining coins
    fresh (not carrying over previous rolls), building Power the normal
    sequential way. Whichever side's final Power is lower this round
    permanently loses one coin. A tie means neither side loses a coin,
    but the round still counts, both sides simply re-toss again next
    round with unchanged coin counts.

    This repeats until one side's coin count hits zero, at which point
    the OTHER side wins the clash outright. The winner then makes one
    final, completely fresh toss using whatever coins they have left,
    exactly like an unopposed attack, and that toss is what actually
    deals damage. The attrition rounds themselves never deal damage,
    they only decide who wins and how many coins the winner has left.

    The winner's per-coin status lists are sliced down to match however
    many coins survived, keeping the FIRST N entries (front-loading
    status onto early coin slots is a real strategic choice, since those
    are the ones most likely to make it through attrition intact).

    heads_chance_a/heads_chance_b are each side's OWN Sanity-driven odds
    (Fighter.heads_chance()), applied to every toss on that side, both
    during attrition and on the final damage toss.

    max_rounds is a safety valve against a true infinite loop (a tie
    every single round forever); hitting it is astronomically unlikely
    with real coin randomness.
    """
    coins_a = skill_a.coins
    coins_b = skill_b.coins
    rounds: list[ClashRound] = []
    round_count = 0

    while coins_a > 0 and coins_b > 0:
        round_count += 1
        if round_count > max_rounds:
            raise RuntimeError(
                f"Clash failed to resolve after {max_rounds} rounds, "
                "something is almost certainly wrong with the input skills."
            )

        before_a, before_b = coins_a, coins_b
        temp_a = replace(skill_a, coins=coins_a)
        temp_b = replace(skill_b, coins=coins_b)
        result_a = resolve_skill(temp_a, heads_chance_a)
        result_b = resolve_skill(temp_b, heads_chance_b)

        if result_a.final_power > result_b.final_power:
            loser = "b"
            coins_b -= 1
        elif result_b.final_power > result_a.final_power:
            loser = "a"
            coins_a -= 1
        else:
            loser = None

        rounds.append(ClashRound(
            result_a=result_a, result_b=result_b,
            coins_a_before=before_a, coins_b_before=before_b,
            loser=loser,
        ))

    winner = "a" if coins_a > 0 else "b"
    winner_skill = skill_a if winner == "a" else skill_b
    winner_remaining = coins_a if winner == "a" else coins_b
    winner_heads_chance = heads_chance_a if winner == "a" else heads_chance_b

    final_skill = replace(
        winner_skill,
        coins=winner_remaining,
        coin_statuses=winner_skill.coin_statuses[:winner_remaining],
        coin_status_potencies=winner_skill.coin_status_potencies[:winner_remaining],
        coin_status_counts=winner_skill.coin_status_counts[:winner_remaining],
    )
    final_result = resolve_skill(final_skill, winner_heads_chance)

    return ClashOutcome(winner=winner, rounds=rounds, winner_final_result=final_result)