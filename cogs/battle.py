import discord
from discord.ext import commands
from discord import app_commands

from game.battle import Battle, Fighter
from game.skills import Skill, resolve_skill, resolve_clash
from game.colors import get_status_color
from game.character import load_character


def build_fighter_embed(fighter: Fighter) -> discord.Embed:
    """One embed per fighter, so each one's own avatar actually shows.

    Discord embeds only support a single image/thumbnail each, and embeds
    in one message always stack vertically, never side by side, so a
    merged "one card per side" view can only ever show one avatar. This
    keeps individual avatars correct, ordered so same-side fighters sit
    next to each other in the stack instead.
    """
    embed = discord.Embed(
        title=fighter.name,
        color=get_status_color(fighter.active_status),
    )
    if fighter.avatar_url:
        embed.set_thumbnail(url=fighter.avatar_url)
    embed.add_field(name="Side", value=fighter.side, inline=True)
    embed.add_field(name="HP", value=f"{fighter.hp}/{fighter.max_hp}", inline=True)
    embed.add_field(name="SP", value=str(fighter.sp), inline=True)
    embed.add_field(name="Speed", value=str(fighter.speed), inline=True)
    if fighter.active_status:
        embed.set_footer(text=f"Status: {fighter.active_status.capitalize()}")
    if not fighter.is_alive():
        embed.description = "Down"
    return embed


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
        character_name="Pull stats and avatar from a saved character (optional)",
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
        # Order by side so Side A fighters stack together, then Side B,
        # even though each is its own embed card.
        ordered = sorted(battle.fighters, key=lambda f: f.side)
        embeds = [build_fighter_embed(f) for f in ordered]
        await interaction.response.send_message(embeds=embeds)

    @app_commands.command(name="setstatus", description="Manually set a fighter's active status (testing only)")
    @app_commands.describe(
        fighter="Fighter name",
        status_name="burn, bleed, tremor, rupture, sinking, poise, charge, or none",
    )
    async def setstatus(self, interaction: discord.Interaction, fighter: str, status_name: str):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message("No active battle here.", ephemeral=True)
            return

        target_fighter = battle.get_fighter(fighter)
        if target_fighter is None:
            await interaction.response.send_message(f"No fighter named {fighter}.", ephemeral=True)
            return

        status_name = status_name.lower()
        target_fighter.set_status(None if status_name == "none" else status_name)
        await interaction.response.send_message(f"Set {target_fighter.name}'s active status to {status_name}.")

    @app_commands.command(name="addskill", description="Give a fighter a skill")
    @app_commands.describe(
        fighter="Which fighter learns this skill",
        skill_name="Skill name",
        base_power="Base Power",
        coin_power="Coin Power",
        coins="Number of coins (1-4)",
    )
    async def addskill(
        self,
        interaction: discord.Interaction,
        fighter: str,
        skill_name: str,
        base_power: int,
        coin_power: int,
        coins: int,
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

        skill = Skill(name=skill_name, base_power=base_power, coin_power=coin_power, coins=coins)
        target_fighter.add_skill(skill)
        await interaction.response.send_message(
            f"{target_fighter.name} learned {skill_name} "
            f"(Base {base_power}, +{coin_power} Coin Power, {coins} coins)."
        )

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
            f"{caster.name} declares {skill.name} targeting {target_fighter.name}."
        )

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

        log_lines = [f"Combat Phase, Round {battle.round_number}", ""]
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
                result_a = resolve_skill(fighter.declared_skill)
                result_b = resolve_skill(target.declared_skill)
                log_lines.append(f"Clash: {fighter.name} vs {target.name}")
                log_lines.append(result_a.log())
                log_lines.append(result_b.log())

                winner_result = resolve_clash(result_a, result_b)
                if winner_result is None:
                    log_lines.append("**It's a tie. No damage dealt.**")
                    log_lines.append("")
                else:
                    winner = fighter if winner_result is result_a else target
                    loser = target if winner is fighter else fighter
                    loser.take_damage(winner_result.total_damage)
                    log_lines.append(
                        f"**{winner.name} wins the clash.** {loser.name} takes {winner_result.total_damage} damage. "
                        f"({loser.name}: {loser.hp}/{loser.max_hp} HP)"
                    )
                    log_lines.append("")

                already_resolved.add(fighter.name)
                already_resolved.add(target.name)

            else:
                result = resolve_skill(fighter.declared_skill)
                log_lines.append(f"{fighter.name} attacks {target.name} (unopposed)")
                log_lines.append(result.log())
                target.take_damage(result.total_damage)
                log_lines.append(
                    f"{target.name} takes {result.total_damage} damage. "
                    f"({target.name}: {target.hp}/{target.max_hp} HP)"
                )
                log_lines.append("")
                already_resolved.add(fighter.name)

        battle.start_new_round()
        log_lines.append("")
        log_lines.append(battle.summary())

        full_log = "\n".join(log_lines)
        if len(full_log) <= 2000:
            await interaction.response.send_message(full_log)
        else:
            await interaction.response.send_message(full_log[:1990] + "\n...(truncated)")

    @app_commands.command(name="end", description="End the battle in this channel")
    async def end(self, interaction: discord.Interaction):
        if interaction.channel_id in self.battles:
            del self.battles[interaction.channel_id]
            await interaction.response.send_message("Battle ended.")
        else:
            await interaction.response.send_message("No active battle here.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BattleCog(bot))