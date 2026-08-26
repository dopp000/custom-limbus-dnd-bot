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
        # Nobody clicked within the timeout window. The skill was never
        # added (that only happens inside confirm()), so there's nothing
        # to undo, just let the buttons go stale silently.
        for child in self.children:
            child.disabled = True


def format_skill_result(result: SkillResult, survived: list[bool] | None = None) -> str:
    """Discord-facing version of SkillResult.log() (game/skills.py).
    same coin-by-coin breakdown, but with real Heads/Tails emoji instead
    of plain text. Kept separate from .log() on purpose: .log() stays
    plain-text so it's still usable for quick REPL testing without any
    Discord/emoji setup involved.

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
        line = f"  Coin {i}: {face} Power {c.power_after}, {c.damage_dealt} damage"
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


def apply_incoming_hit(attacker_skill: Skill, raw_damage: int, target: Fighter) -> tuple[int, list[str]]:
    """One fighter just landed a hit on another. This handles everything
    that happens to the TARGET as a result:

    1. The attacking skill's damage_type gets reduced by the target's
       resistance for that type.
    2. If the target currently has Rupture, it triggers: deals its stored
       Potency as bonus damage, then decays (Count -1, or fully clears if
       that was the last stack). Rupture is the only status with an
       automatic on-hit trigger wired up so far.
    3. If the attacking skill inflicts a NEW status, it layers on top
       (after step 2's decay, if the status happens to be Rupture, so the
       stacking rules from game/statuses.py apply correctly), with the
       incoming Potency reduced by the target's resistance for that status.

    Returns (total_damage_to_apply, log_lines_describing_what_happened).
    """
    log = []

    resisted_damage = apply_resistance(raw_damage, target.resistances.get(attacker_skill.damage_type, 0))
    total_damage = resisted_damage
    if resisted_damage != raw_damage:
        log.append(
            f"({damage_type_emoji(attacker_skill.damage_type)} "
            f"{attacker_skill.damage_type.capitalize()} resistance: {raw_damage} -> {resisted_damage})"
        )

    decayed_rupture = None
    rupture = target.get_status("rupture")
    if rupture is not None:
        total_damage += rupture.potency
        log.append(f"{status_emoji('rupture')} Rupture triggers on {target.name}: +{rupture.potency} damage")
        decayed_rupture = decay_after_trigger(rupture)
        target.set_status_instance(decayed_rupture)

    if attacker_skill.status_name:
        resistance_pct = target.resistances.get(attacker_skill.status_name, 0)
        added_potency = apply_resistance(attacker_skill.status_potency, resistance_pct)
        added_count = attacker_skill.status_count

        if attacker_skill.status_name == "rupture":
            current = decayed_rupture
        else:
            current = target.get_status(attacker_skill.status_name)

        new_instance = apply_status(current, attacker_skill.status_name, added_potency, added_count)
        target.set_status_instance(new_instance)
        log.append(
            f"{target.name} gains {status_emoji(attacker_skill.status_name)} "
            f"{attacker_skill.status_name.capitalize()}: {new_instance.potency}/{new_instance.count}"
        )

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
                f"(HP {fighter.hp}, SP {fighter.sp}, Speed {fighter.speed}, Power {fighter.power})."
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
        status_name="Status this skill inflicts on hit, if any (optional)",
        status_potency="Potency added per hit, before resistance (optional, default 0)",
        status_count="Count/stacks added per hit (optional, default 0)",
    )
    @app_commands.choices(
        damage_type=[app_commands.Choice(name=t.capitalize(), value=t) for t in DAMAGE_TYPES],
        status_name=[app_commands.Choice(name=s.capitalize(), value=s) for s in INFLICTABLE_STATUSES],
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
        status_name: app_commands.Choice[str] | None = None,
        status_potency: int = 0,
        status_count: int = 0,
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

        skill = Skill(
            name=skill_name,
            base_power=base_power,
            coin_power=coin_power,
            coins=coins,
            damage_type=damage_type.value,
            status_name=status_name.value if status_name else None,
            status_potency=status_potency,
            status_count=status_count,
        )
        # NOT added to the fighter yet, that only happens if Confirm is
        # pressed, inside SkillConfirmView.confirm(). This whole preview
        # is ephemeral, only you can see it until you confirm.

        coin_preview = " ".join(coin_emoji("base") for _ in range(coins))
        status_note = ""
        if status_name:
            status_note = (
                f", inflicts {status_emoji(status_name.value)} {status_name.name} "
                f"({status_potency} potency, +{status_count} count)"
            )
        preview_text = (
            f"{target_fighter.name} would learn {skill_name} {coin_preview}\n"
            f"(Base {base_power}, +{coin_power} Coin Power, {coins} coins, "
            f"{damage_type_emoji(damage_type.value)} {damage_type.name}{status_note})."
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
        # Separate public followup, visible to everyone, but with no
        # details about which skill or who was targeted, that stays
        # private to the caller above.
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
                    winner.declared_skill, outcome.total_damage(), loser
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
                    fighter.declared_skill, result.total_damage, target
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