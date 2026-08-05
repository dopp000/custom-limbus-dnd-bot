import random
from dataclasses import dataclass


@dataclass
class Skill:
    """A single skill definition — just the numbers, no Discord code."""

    name: str
    base_power: int
    coin_power: int
    coins: int


@dataclass
class CoinResult:
    """The outcome of a single coin toss within a skill's resolution."""

    heads: bool
    power_after: int
    damage_dealt: int


@dataclass
class SkillResult:
    """The full outcome of resolving one skill — every coin, plus the total."""

    skill: Skill
    coin_results: list[CoinResult]

    @property
    def total_damage(self) -> int:
        return sum(c.damage_dealt for c in self.coin_results)

    @property
    def final_power(self) -> int:
        """The Power reached after the last coin — this is what Clashes compare."""
        return self.coin_results[-1].power_after

    def log(self) -> str:
        lines = [f"**{self.skill.name}** (Base {self.skill.base_power}, +{self.skill.coin_power} Coin Power, {self.skill.coins} coins)"]
        for i, c in enumerate(self.coin_results, start=1):
            face = "Heads" if c.heads else "Tails"
            lines.append(f"  Coin {i}: {face} → Power {c.power_after} → {c.damage_dealt} damage")
        lines.append(f"  **Total: {self.total_damage} damage**")
        return "\n".join(lines)


def flip_coin() -> bool:
    """Rolls a d20. 11-20 is Heads, 1-10 is Tails. Returns True for Heads."""
    roll = random.randint(1, 20)
    return roll >= 11


def resolve_skill(skill: Skill) -> SkillResult:
    power = skill.base_power
    results: list[CoinResult] = []

    for _ in range(skill.coins):
        heads = flip_coin()
        if heads:
            power += skill.coin_power
        results.append(CoinResult(heads=heads, power_after=power, damage_dealt=power))

    return SkillResult(skill=skill, coin_results=results)


def resolve_clash(result_a: SkillResult, result_b: SkillResult) -> SkillResult | None:
    """Compares two SkillResults by final_power. Returns the winner, or None on a tie."""
    if result_a.final_power > result_b.final_power:
        return result_a
    elif result_b.final_power > result_a.final_power:
        return result_b
    else:
        return None