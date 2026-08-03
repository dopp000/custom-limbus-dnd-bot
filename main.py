import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

# Loads the DISCORD_TOKEN variable out of the .env file, so your real
# token never has to be typed directly into this script (or committed to git).
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Intents = what kinds of Discord events your bot is allowed to receive.
# We only need the defaults for slash commands right now.
intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    # Fires once, when the bot successfully logs in.
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        # Slash commands need to be "synced" to Discord before they show up.
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


@bot.tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    # interaction.response.send_message replies to the slash command.
    await interaction.response.send_message("Pong! 🏓")


bot.run(TOKEN)
