import discord
from discord.ext import commands
from discord import app_commands

from game.battle import Battle, Fighter
from game.skills import Skill


class BattleCog(commands.GroupCog, name="battle"):
    """Commands for creating and managing battles."""

    # commands.GroupCog automatically registers every @app_commands.command
    # below as a SUBCOMMAND of /battle — so you get /battle create,
    # /battle addfighter, etc. for free, without manually building an
    # app_commands.Group by hand. Note: the class docstring above becomes
    # the group's description shown in Discord, which is why it's kept short —
    # Discord enforces a 100-character limit on it.

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.battles: dict[int, Battle] = {}

    @app_commands.command(name="create", description="Start a new battle in this channel")
    async def create(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        if channel_id in self.battles:
            await interaction.response.send_message(
                "There's already an active battle in this channel. Use `/battle end` first.",
                ephemeral=True,
            )
            return

        self.battles[channel_id] = Battle(channel_id=channel_id)
        await interaction.response.send_message(
            "⚔️ Battle created! Use `/battle addfighter` to add combatants."
        )

    @app_commands.command(name="addfighter", description="Add a fighter to the current battle")
    @app_commands.describe(
        name="Fighter's name",
        side="A or B",
        hp="Starting HP (optional, default 100)",
    )
    async def addfighter(
        self,
        interaction: discord.Interaction,
        name: str,
        side: str,
        hp: int = 100,
    ):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message(
                "No active battle here yet. Use `/battle create` first.", ephemeral=True
            )
            return

        side = side.upper()
        if side not in ("A", "B"):
            await interaction.response.send_message("Side must be `A` or `B`.", ephemeral=True)
            return

        if battle.get_fighter(name) is not None:
            await interaction.response.send_message(
                f"A fighter named **{name}** already exists in this battle.", ephemeral=True
            )
            return

        fighter = Fighter(name=name, side=side, hp=hp, max_hp=hp)
        battle.add_fighter(fighter)
        await interaction.response.send_message(f"Added **{name}** to Side {side} ({hp} HP).")

    @app_commands.command(name="status", description="Show the current battle state")
    async def status(self, interaction: discord.Interaction):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message("No active battle here.", ephemeral=True)
            return
        await interaction.response.send_message(battle.summary())

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
            await interaction.response.send_message(f"No fighter named **{fighter}**.", ephemeral=True)
            return

        if not (1 <= coins <= 4):
            await interaction.response.send_message("Coins must be between 1 and 4.", ephemeral=True)
            return

        skill = Skill(name=skill_name, base_power=base_power, coin_power=coin_power, coins=coins)
        target_fighter.add_skill(skill)
        await interaction.response.send_message(
            f"**{target_fighter.name}** learned **{skill_name}** "
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
            await interaction.response.send_message(f"No fighter named **{fighter}**.", ephemeral=True)
            return

        if not caster.is_alive():
            await interaction.response.send_message(f"**{caster.name}** is down and can't act.", ephemeral=True)
            return

        skill = caster.get_skill(skill_name)
        if skill is None:
            await interaction.response.send_message(
                f"**{caster.name}** doesn't know a skill called **{skill_name}**.", ephemeral=True
            )
            return

        target_fighter = battle.get_fighter(target)
        if target_fighter is None:
            await interaction.response.send_message(f"No fighter named **{target}**.", ephemeral=True)
            return

        caster.declare(skill, target_fighter)
        await interaction.response.send_message(
            f"**{caster.name}** declares **{skill.name}** targeting **{target_fighter.name}**."
        )

    @app_commands.command(name="end", description="End the battle in this channel")
    async def end(self, interaction: discord.Interaction):
        if interaction.channel_id in self.battles:
            del self.battles[interaction.channel_id]
            await interaction.response.send_message("Battle ended.")
        else:
            await interaction.response.send_message("No active battle here.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BattleCog(bot))