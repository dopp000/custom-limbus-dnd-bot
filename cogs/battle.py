import discord
from discord.ext import commands
from discord import app_commands

from game.battle import Battle, Fighter


class BattleCog(commands.GroupCog, name="battle"):
    """Commands for creating and managing battles."""

    # commands.GroupCog automatically registers every @app_commands.command
    # below as a SUBCOMMAND of /battle. So you get /battle create,
    # /battle addfighter, etc. for free, without manually building an
    # app_commands.Group by hand.

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # channel_id -> Battle. This is the "database" for now. Resets if the bot restarts.
        self.battles: dict[int, Battle] = {}

    @app_commands.command(name="create", description="Start a new battle in this channel")
    async def create(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        if channel_id in self.battles:
            await interaction.response.send_message(
                "There's already an active battle in this channel. Use `/battle end` first.",
                ephemeral=True,  # only the person who ran the command can see this
            )
            return

        self.battles[channel_id] = Battle(channel_id=channel_id)
        await interaction.response.send_message(
            "Battle created! Use `/battle addfighter` to add combatants."
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

    @app_commands.command(name="end", description="End the battle in this channel")
    async def end(self, interaction: discord.Interaction):
        if interaction.channel_id in self.battles:
            del self.battles[interaction.channel_id]
            await interaction.response.send_message("Battle ended.")
        else:
            await interaction.response.send_message("No active battle here.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BattleCog(bot))