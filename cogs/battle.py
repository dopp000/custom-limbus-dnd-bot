import discord
from discord.ext import commands
from discord import app_commands

from game.battle import (
    Battle, Fighter,
    SANITY_CLASH_WIN, SANITY_CLASH_LOSS, SANITY_PER_HEADS_UNOPPOSED,
)
from game.skills import Skill, SkillResult, ClashOutcome, resolve_skill, resolve_round_clash
from game.colors import get_status_color
from game.character import load_character
from game.statuses import StatusInstance, decay_after_trigger, apply_status, INFLICTABLE_STATUSES
from game.resistances import DAMAGE_TYPES, apply_resistance
from game.emojis import status_emoji, coin_emoji, damage_type_emoji

LOG_CHANNEL_ID = 1538071557560213595  # #bot-combat-logs


class SkillConfirmView(discord.ui.View):
    """Confirm/Cancel buttons shown after /battle addskill, so a skill
    only actually gets saved once the player explicitly confirms it (and
    a typo or change of mind doesn't need an admin to undo).
    """

    def __init__(self, fighter: Fighter, skill: Skill, preview_text: str, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.fighter = fighter
        self.skill = skill
        self.preview_text = preview_text

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.fighter.add_skill(self.skill)

        log_channel = interaction.client.get_channel(LOG_CHANNEL_ID)
        if log_channel is not None:
            await log_channel.send(
                f"{interaction.user.mention} confirmed a new skill for **{self.fighter.name}**:\n"
                f"{self.preview_text}"
            )

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"**Confirmed.** {self.fighter.name} learned {self.skill.name}.",
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Cancelled. No skill was added.",
            view=self,
        )
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


def format_skill_result(result: SkillResult, survived: list[bool] | None = None) -> str:
    """Discord-facing version of SkillResult.log(): same coin-by-coin
    breakdown, real Heads/Tails emoji, and each coin's own status shown
    as PotencyStatusCount (e.g. 3:Rupture:2) right next to that coin.

    If `survived` is given (one bool per coin, from a pairwise clash),
    destroyed coins are struck through and excluded from the total.
    """
    lines = [
        f"**{result.skill.name}** (Base {result.skill.base_power}, "
        f"+{result.skill.coin_power} Coin Power, {result.skill.coins} coins)"
    ]
    total = 0
    for i, c in enumerate(result.coin_results, start=1):
        face = coin_emoji("heads") if c.heads else coin_emoji("tails")
        alive = survived[i - 1] if survived is not None else True

        status_tag = ""
        idx = i - 1
        if idx < len(result.skill.coin_statuses) and result.skill.coin_statuses[idx]:
            name = result.skill.coin_statuses[idx]
            potency = result.skill.coin_status_potencies[idx]
            count = result.skill.coin_status_counts[idx]
            status_tag = f" {potency}{status_emoji(name)}{count}"

        line = f"  Coin {i}: {face} Power {c.power_after}, {c.damage_dealt} damage{status_tag}"
        if not alive:
            line = f"~~{line.strip()}~~ (destroyed)"
            lines.append(f"  {line}")
        else:
            lines.append(line)
            total += c.damage_dealt
    lines.append(f"  **Total: {total} damage**")
    return "\n".join(lines)


def format_clash_rounds(outcome: ClashOutcome, name_a: str, name_b: str) -> str:
    """Compact one-line-per-round summary of the attrition phase: each
    round's Power comparison and who lost a coin (or if it tied).
    """
    lines = []
    for i, r in enumerate(outcome.rounds, start=1):
        line = (
            f"Round {i}: {name_a} ({r.coins_a_before} coins) Power {r.result_a.final_power} "
            f"vs {name_b} ({r.coins_b_before} coins) Power {r.result_b.final_power}"
        )
        if r.loser == "a":
            line += f" -> {name_a} loses a coin"
        elif r.loser == "b":
            line += f" -> {name_b} loses a coin"
        else:
            line += " -> tie, nobody loses a coin"
        lines.append(line)
    return "\n".join(lines)


def build_fighter_embed(fighter: Fighter) -> discord.Embed:
    """One embed per fighter. Border color keys off whichever status is
    first in the dict (arbitrary if multiple are active, Discord only
    allows one color per embed so there's no perfect answer here).
    """
    primary_status = next(iter(fighter.statuses), None)
    embed = discord.Embed(
        title=fighter.name,
        color=get_status_color(primary_status),
    )
    if fighter.avatar_url:
        embed.set_thumbnail(url=fighter.avatar_url)
    embed.add_field(name="Side", value=fighter.side, inline=True)
    embed.add_field(name="HP", value=f"{fighter.hp}/{fighter.max_hp}", inline=True)
    embed.add_field(name="Sanity", value=str(fighter.sanity), inline=True)
    embed.add_field(name="Speed", value=str(fighter.speed), inline=True)
    if fighter.statuses:
        lines = [
            f"{status_emoji(s.name)} {s.name.capitalize()}: {s.potency}/{s.count}"
            for s in fighter.statuses.values()
        ]
        embed.add_field(name="Statuses", value="\n".join(lines), inline=False)
    if not fighter.is_alive():
        embed.description = "Down"
    return embed


def apply_incoming_hit(attacker_skill: Skill, result: SkillResult, target: Fighter) -> tuple[int, list[str]]:
    """A fighter's skill just hit (or clash-won against) a target.

    Walks the skill's coins ONE AT A TIME, in order, since each coin is
    its OWN hit for status purposes:

    For each coin, in sequence:
      1. That coin's raw damage gets reduced by the target's resistance
         for the skill's damage_type.
      2. If the target currently has Rupture, it triggers on THIS coin's
         hit: deals its stored Potency as bonus damage, then decays.
         A multi-coin skill can trigger Rupture more than once, once per
         coin that lands, matching "next Y times this unit is hit."
      3. If THIS coin inflicts a status (coin_statuses[i] is set), it
         layers on top, with potency reduced by the target's resistance
         for that status name. Works for any status name, keyword or not.

    Returns (total_damage_to_apply, log_lines_describing_what_happened).
    """
    log: list[str] = []
    total_damage = 0

    for i, coin in enumerate(result.coin_results):
        raw = coin.damage_dealt
        resisted = apply_resistance(raw, target.resistances.get(attacker_skill.damage_type, 0))
        if resisted != raw:
            log.append(
                f"Coin {i + 1}: ({damage_type_emoji(attacker_skill.damage_type)} "
                f"{attacker_skill.damage_type.capitalize()} resistance: {raw} -> {resisted})"
            )
        coin_total = resisted

        decayed_rupture = None
        rupture = target.get_status("rupture")
        if rupture is not None:
            coin_total += rupture.potency
            log.append(
                f"Coin {i + 1}: {status_emoji('rupture')} Rupture triggers on "
                f"{target.name}: +{rupture.potency} damage"
            )
            decayed_rupture = decay_after_trigger(rupture)
            target.set_status_instance(decayed_rupture)

        status_name = attacker_skill.coin_statuses[i] if i < len(attacker_skill.coin_statuses) else None
        if status_name:
            resistance_pct = target.resistances.get(status_name, 0)
            raw_potency = (
                attacker_skill.coin_status_potencies[i]
                if i < len(attacker_skill.coin_status_potencies) else 0
            )
            added_potency = apply_resistance(raw_potency, resistance_pct)
            added_count = (
                attacker_skill.coin_status_counts[i]
                if i < len(attacker_skill.coin_status_counts) else 0
            )

            current = decayed_rupture if status_name == "rupture" else target.get_status(status_name)
            new_instance = apply_status(current, status_name, added_potency, added_count)
            target.set_status_instance(new_instance)
            log.append(
                f"Coin {i + 1}: {target.name} gains {status_emoji(status_name)} "
                f"{status_name.capitalize()}: {new_instance.potency}/{new_instance.count}"
            )

        total_damage += coin_total

    return total_damage, log


class BattleCog(commands.GroupCog, name="battle"):
    """Commands for creating and managing battles."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.battles: dict[int, Battle] = {}

    @app_commands.command(name="create", description="Start a new battle in this channel")
    async def create(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        if channel_id in self.battles:
            await interaction.response.send_message(
                "There's already an active battle in this channel. Use /battle end first.",
                ephemeral=True,
            )
            return

        self.battles[channel_id] = Battle(channel_id=channel_id)
        await interaction.response.send_message(
            "Battle created! Use /battle addfighter to add combatants."
        )

    @app_commands.command(name="addfighter", description="Add a fighter to the current battle")
    @app_commands.describe(
        side="A or B",
        character_name="Pull stats, avatar, and resistances from a saved character (optional)",
        name="Fighter's name, only used if not pulling from a saved character",
        hp="Starting HP, only used if not pulling from a saved character (default 100)",
    )
    async def addfighter(
        self,
        interaction: discord.Interaction,
        side: str,
        character_name: str | None = None,
        name: str | None = None,
        hp: int = 100,
    ):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message(
                "No active battle here yet. Use /battle create first.", ephemeral=True
            )
            return

        side = side.upper()
        if side not in ("A", "B"):
            await interaction.response.send_message("Side must be A or B.", ephemeral=True)
            return

        if character_name is None and name is None:
            await interaction.response.send_message(
                "Provide either character_name (a saved character) or name (a one-off fighter).",
                ephemeral=True,
            )
            return

        if character_name is not None and name is not None:
            await interaction.response.send_message(
                "Use character_name OR name, not both. character_name pulls a saved character, "
                "name creates a one-off fighter for just this battle.",
                ephemeral=True,
            )
            return

        if character_name is not None:
            character = load_character(character_name)
            if character is None:
                await interaction.response.send_message(
                    f"No character named {character_name}.", ephemeral=True
                )
                return

            if battle.get_fighter(character.name) is not None:
                await interaction.response.send_message(
                    f"A fighter named {character.name} already exists in this battle.", ephemeral=True
                )
                return

            fighter = Fighter.from_character(character, side)
            battle.add_fighter(fighter)
            await interaction.response.send_message(
                f"Added {fighter.name} to Side {side}, pulled from saved character "
                f"(HP {fighter.hp}, Sanity {fighter.sanity}, Speed {fighter.speed}, Power {fighter.power})."
            )
            return

        if battle.get_fighter(name) is not None:
            await interaction.response.send_message(
                f"A fighter named {name} already exists in this battle.", ephemeral=True
            )
            return

        fighter = Fighter(name=name, side=side, hp=hp, max_hp=hp)
        battle.add_fighter(fighter)
        await interaction.response.send_message(f"Added {name} to Side {side} ({hp} HP).")

    @app_commands.command(name="status", description="Show the current battle state")
    async def status(self, interaction: discord.Interaction):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message("No active battle here.", ephemeral=True)
            return
        if not battle.fighters:
            await interaction.response.send_message("No fighters in this battle yet.", ephemeral=True)
            return
        ordered = sorted(battle.fighters, key=lambda f: f.side)
        embeds = [build_fighter_embed(f) for f in ordered]
        await interaction.response.send_message(embeds=embeds)

    @app_commands.command(name="setstatus", description="Manually set a fighter's status (testing/admin tool)")
    @app_commands.describe(
        fighter="Fighter name",
        status_name="Which status to set, or 'none' to clear all statuses",
        potency="Potency (ignored if status_name is none)",
        count="Count (ignored if status_name is none)",
    )
    @app_commands.choices(
        status_name=[app_commands.Choice(name=s.capitalize(), value=s) for s in INFLICTABLE_STATUSES]
        + [app_commands.Choice(name="None (clear all)", value="none")]
    )
    async def setstatus(
        self,
        interaction: discord.Interaction,
        fighter: str,
        status_name: app_commands.Choice[str],
        potency: int = 0,
        count: int = 0,
    ):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message("No active battle here.", ephemeral=True)
            return

        target_fighter = battle.get_fighter(fighter)
        if target_fighter is None:
            await interaction.response.send_message(f"No fighter named {fighter}.", ephemeral=True)
            return

        if status_name.value == "none":
            target_fighter.statuses.clear()
            await interaction.response.send_message(f"Cleared all statuses on {target_fighter.name}.")
            return

        target_fighter.set_status_instance(
            StatusInstance(name=status_name.value, potency=potency, count=count)
        )
        await interaction.response.send_message(
            f"Set {target_fighter.name}'s {status_emoji(status_name.value)} "
            f"{status_name.name} to {potency}/{count}."
        )

    @app_commands.command(name="addskill", description="Give a fighter a skill")
    @app_commands.describe(
        fighter="Which fighter learns this skill",
        skill_name="Skill name",
        base_power="Base Power",
        coin_power="Coin Power",
        coins="Number of coins (1-4)",
        damage_type="Slash, Blunt, or Pierce",
        status_input=(
            "Per-coin statuses, comma-separated, one entry per coin. "
            "Each entry is 'none' or 'Name:Potency:Count'. Works for any status "
            "name, keyword (Rupture, Bleed...) or not (Fragile, Bind...). "
            "Example for 3 coins: none,Rupture:3:2,Bleed:1:1"
        ),
    )
    @app_commands.choices(
        damage_type=[app_commands.Choice(name=t.capitalize(), value=t) for t in DAMAGE_TYPES],
    )
    async def addskill(
        self,
        interaction: discord.Interaction,
        fighter: str,
        skill_name: str,
        base_power: int,
        coin_power: int,
        coins: int,
        damage_type: app_commands.Choice[str],
        status_input: str = "none",
    ):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message("No active battle here.", ephemeral=True)
            return

        target_fighter = battle.get_fighter(fighter)
        if target_fighter is None:
            await interaction.response.send_message(f"No fighter named {fighter}.", ephemeral=True)
            return

        if not (1 <= coins <= 4):
            await interaction.response.send_message("Coins must be between 1 and 4.", ephemeral=True)
            return

        tokens = [t.strip() for t in status_input.split(",")]
        # A single "none" (the default) applies to every coin, no need to
        # type it once per coin.
        if len(tokens) == 1 and tokens[0].lower() == "none":
            tokens = ["none"] * coins

        if len(tokens) != coins:
            await interaction.response.send_message(
                f"status_input needs exactly {coins} comma-separated entries (one per coin), "
                f"got {len(tokens)}. Use 'none' for a coin with no status. "
                f"Example: {','.join(['none'] * coins)}",
                ephemeral=True,
            )
            return

        coin_statuses: list[str | None] = []
        coin_status_potencies: list[int] = []
        coin_status_counts: list[int] = []

        for i, token in enumerate(tokens):
            if token.lower() == "none":
                coin_statuses.append(None)
                coin_status_potencies.append(0)
                coin_status_counts.append(0)
                continue

            parts = token.split(":")
            if len(parts) != 3:
                await interaction.response.send_message(
                    f"Coin {i + 1} entry '{token}' is invalid. Use 'Name:Potency:Count' or 'none'.",
                    ephemeral=True,
                )
                return

            status_name_raw, potency_raw, count_raw = parts
            try:
                potency = int(potency_raw)
                count = int(count_raw)
            except ValueError:
                await interaction.response.send_message(
                    f"Coin {i + 1} entry '{token}' has a non-numeric potency/count.",
                    ephemeral=True,
                )
                return

            coin_statuses.append(status_name_raw.strip().lower())
            coin_status_potencies.append(potency)
            coin_status_counts.append(count)

        skill = Skill(
            name=skill_name,
            base_power=base_power,
            coin_power=coin_power,
            coins=coins,
            damage_type=damage_type.value,
            coin_statuses=coin_statuses,
            coin_status_potencies=coin_status_potencies,
            coin_status_counts=coin_status_counts,
        )
        # NOT added to the fighter yet, that only happens if Confirm is
        # pressed, inside SkillConfirmView.confirm().

        coin_lines = []
        for i in range(coins):
            tag = coin_emoji("base")
            if coin_statuses[i]:
                tag += f" ({coin_status_potencies[i]}{status_emoji(coin_statuses[i])}{coin_status_counts[i]})"
            coin_lines.append(tag)

        preview_text = (
            f"{target_fighter.name} would learn {skill_name}\n"
            f"(Base {base_power}, +{coin_power} Coin Power, {coins} coins, "
            f"{damage_type_emoji(damage_type.value)} {damage_type.name})\n"
            + " ".join(coin_lines)
        )

        view = SkillConfirmView(target_fighter, skill, preview_text)
        await interaction.response.send_message(preview_text, view=view, ephemeral=True)

    @app_commands.command(name="declare", description="Declare a skill and target for this round")
    @app_commands.describe(
        fighter="Who is declaring",
        skill_name="Which skill they're using",
        target="Who they're targeting",
    )
    async def declare(
        self,
        interaction: discord.Interaction,
        fighter: str,
        skill_name: str,
        target: str,
    ):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message("No active battle here.", ephemeral=True)
            return

        caster = battle.get_fighter(fighter)
        if caster is None:
            await interaction.response.send_message(f"No fighter named {fighter}.", ephemeral=True)
            return

        if not caster.is_alive():
            await interaction.response.send_message(f"{caster.name} is down and can't act.", ephemeral=True)
            return

        skill = caster.get_skill(skill_name)
        if skill is None:
            await interaction.response.send_message(
                f"{caster.name} doesn't know a skill called {skill_name}.", ephemeral=True
            )
            return

        target_fighter = battle.get_fighter(target)
        if target_fighter is None:
            await interaction.response.send_message(f"No fighter named {target}.", ephemeral=True)
            return

        caster.declare(skill, target_fighter)
        await interaction.response.send_message(
            f"{caster.name} declares {skill.name} targeting {target_fighter.name}.",
            ephemeral=True,
        )
        await interaction.followup.send(f"**{caster.name} has finished their declaration.**")

    @app_commands.command(name="combat", description="Resolve everyone's declared actions this Combat Phase")
    async def combat(self, interaction: discord.Interaction):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message("No active battle here.", ephemeral=True)
            return

        if not battle.all_declared():
            await interaction.response.send_message(
                "Not everyone has declared yet. Use /battle declare for each fighter first.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"Combat Phase, Round {battle.round_number}",
            color=0x5865F2,
        )
        already_resolved: set[str] = set()

        acting_order = sorted(
            (f for f in battle.fighters if f.is_alive()),
            key=lambda f: f.speed,
            reverse=True,
        )

        for fighter in acting_order:
            if fighter.name in already_resolved:
                continue
            if fighter.declared_skill is None:
                continue

            target = fighter.declared_target

            is_clash = (
                target is not None
                and target.declared_skill is not None
                and target.declared_target is fighter
            )

            if is_clash:
                outcome = resolve_round_clash(
                    fighter.declared_skill, target.declared_skill,
                    heads_chance_a=fighter.heads_chance(),
                    heads_chance_b=target.heads_chance(),
                )
                winner = fighter if outcome.winner == "a" else target
                loser = target if winner is fighter else fighter

                winner.gain_sanity(SANITY_CLASH_WIN)
                loser.lose_sanity(SANITY_CLASH_LOSS)

                total_damage, status_log = apply_incoming_hit(
                    winner.declared_skill, outcome.winner_final_result, loser
                )
                loser.take_damage(total_damage)

                field_value = format_clash_rounds(outcome, fighter.name, target.name)
                field_value += (
                    f"\n\n**{winner.name}'s final attack** "
                    f"({outcome.winner_final_result.skill.coins} coins remaining):\n"
                )
                field_value += format_skill_result(outcome.winner_final_result)
                field_value += (
                    f"\n\n**{winner.name} wins the clash.** {loser.name} takes {total_damage} damage. "
                    f"({loser.name}: {loser.hp}/{loser.max_hp} HP)"
                )
                field_value += (
                    f"\n{winner.name} Sanity +{SANITY_CLASH_WIN} ({winner.sanity}), "
                    f"{loser.name} Sanity -{SANITY_CLASH_LOSS} ({loser.sanity})"
                )
                if status_log:
                    field_value += "\n" + "\n".join(status_log)

                if len(field_value) > 1024:
                    field_value = field_value[:1000] + "\n...(truncated)"

                embed.add_field(name=f"Clash: {fighter.name} vs {target.name}", value=field_value, inline=False)

                already_resolved.add(fighter.name)
                already_resolved.add(target.name)

            else:
                result = resolve_skill(fighter.declared_skill, fighter.heads_chance())
                heads_landed = sum(1 for c in result.coin_results if c.heads)
                sanity_gain = heads_landed * SANITY_PER_HEADS_UNOPPOSED
                fighter.gain_sanity(sanity_gain)

                total_damage, status_log = apply_incoming_hit(
                    fighter.declared_skill, result, target
                )
                target.take_damage(total_damage)

                field_value = format_skill_result(result)
                field_value += (
                    f"\n\n{target.name} takes {total_damage} damage. "
                    f"({target.name}: {target.hp}/{target.max_hp} HP)"
                )
                field_value += f"\n{fighter.name} Sanity +{sanity_gain} ({fighter.sanity})"
                if status_log:
                    field_value += "\n" + "\n".join(status_log)

                if len(field_value) > 1024:
                    field_value = field_value[:1000] + "\n...(truncated)"

                embed.add_field(
                    name=f"{fighter.name} attacks {target.name} (unopposed)",
                    value=field_value,
                    inline=False,
                )
                already_resolved.add(fighter.name)

        battle.start_new_round()
        embed.add_field(name="Battle State", value=battle.summary(), inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="end", description="End the battle in this channel")
    async def end(self, interaction: discord.Interaction):
        if interaction.channel_id in self.battles:
            del self.battles[interaction.channel_id]
            await interaction.response.send_message("Battle ended.")
        else:
            await interaction.response.send_message("No active battle here.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BattleCog(bot))