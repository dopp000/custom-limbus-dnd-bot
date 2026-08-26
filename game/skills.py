import random
from dataclasses import dataclass, replace


@dataclass
class Skill:
    """A single skill definition: just the numbers, no Discord code."""

    name: str
    base_power: int
    coin_power: int
    coins: int
    damage_type: str = "blunt"  # "slash", "blunt", or "pierce"
    # If set, this skill inflicts the named status on its target when it hits.
    # status_potency/status_count are the RAW values before resistance is
    # applied, resistance gets applied by the caller at hit-resolution time.
    status_name: str | None = None
    status_potency: int = 0
    status_count: int = 0


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
            lines.append(f"  Coin {i}: {face}, Power {c.power_after}, {c.damage_dealt} damage")
        lines.append(f"  **Total: {self.total_damage} damage**")
        return "\n".join(lines)


def flip_coin() -> bool:
    """Rolls a d20. 11-20 is Heads, 1-10 is Tails. Returns True for Heads."""
    roll = random.randint(1, 20)
    return roll >= 11


def resolve_skill(skill: Skill) -> SkillResult:
    """Resolves a skill's coins one at a time, in sequence.

    Every coin lands a hit at whatever Power has been built up so far.
    A Heads permanently raises Power (by coin_power) for every hit after it,
    including its own. A Tails deals a hit too, just without raising Power.
    """
    power = skill.base_power
    results: list[CoinResult] = []

    for _ in range(skill.coins):
        heads = flip_coin()
        if heads:
            power += skill.coin_power
        results.append(CoinResult(heads=heads, power_after=power, damage_dealt=power))

    return SkillResult(skill=skill, coin_results=results)


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


def resolve_round_clash(skill_a: Skill, skill_b: Skill, max_rounds: int = 100) -> ClashOutcome:
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
        result_a = resolve_skill(temp_a)
        result_b = resolve_skill(temp_b)

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

    final_skill = replace(winner_skill, coins=winner_remaining)
    final_result = resolve_skill(final_skill)

    return ClashOutcome(winner=winner, rounds=rounds, winner_final_result=final_result)