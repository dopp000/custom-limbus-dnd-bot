import discord
from discord.ext import commands
from discord import app_commands

from game.character import (
    Character,
    save_character,
    load_character,
    character_exists,
    delete_character,
    list_characters_by_owner,
)
from game.resistances import ALL_RESISTANCE_TYPES, DAMAGE_TYPES
from game.emojis import status_emoji, damage_type_emoji

WEBHOOK_NAME = "Character Proxy"


async def resolve_owner_name(guild: discord.Guild | None, owner_id: int) -> str:
    """Tries the local member cache first, falls back to an API call if
    the member isn't cached yet. This is what fixes "Owner: Unknown".
    """
    if guild is None:
        return "Unknown"
    member = guild.get_member(owner_id)
    if member is None:
        try:
            member = await guild.fetch_member(owner_id)
        except discord.NotFound:
            return "Unknown"
    return member.display_name


def build_character_embed(character: Character, owner_name: str) -> discord.Embed:
    embed = discord.Embed(title=character.name)
    if character.avatar_url:
        embed.set_thumbnail(url=character.avatar_url)
    embed.add_field(name="HP", value=f"{character.hp}/{character.max_hp}", inline=True)
    embed.add_field(name="SP", value=str(character.sp), inline=True)
    embed.add_field(name="Speed", value=str(character.speed), inline=True)
    embed.add_field(name="Power", value=str(character.power), inline=True)

    resist_lines = []
    for k, v in character.resistances.items():
        icon = damage_type_emoji(k) if k in DAMAGE_TYPES else status_emoji(k)
        resist_lines.append(f"{icon} {k.capitalize()}: {v}%")
    embed.add_field(name="Resistances", value="\n".join(resist_lines), inline=False)

    embed.set_footer(text=f"Owner: {owner_name}")
    return embed


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.manage_guild


async def get_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook:
    """Reuses an existing proxy webhook in this channel if one already
    exists (created by a previous /character say call), otherwise makes
    a new one. Webhooks live on Discord's side and persist on their own,
    so we do not need to store anything about them ourselves.
    """
    existing = await channel.webhooks()
    for webhook in existing:
        if webhook.name == WEBHOOK_NAME:
            return webhook
    return await channel.create_webhook(name=WEBHOOK_NAME)


class CharacterCog(commands.GroupCog, name="character"):
    """Commands for creating and viewing persistent characters."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="create", description="Create a new character")
    @app_commands.describe(
        name="Character name",
        avatar="Character avatar image (optional)",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        avatar: discord.Attachment | None = None,
    ):
        if character_exists(name):
            await interaction.response.send_message(
                f"A character named {name} already exists.", ephemeral=True
            )
            return

        avatar_url = avatar.url if avatar else None
        character = Character(owner_id=interaction.user.id, name=name, avatar_url=avatar_url)
        save_character(character)

        embed = discord.Embed(
            title="Character Created",
            description=f"**{character.name}** is ready to fight.",
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        embed.set_footer(text="Use /character view to see stats. You and admins can view them.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="list", description="List your saved characters")
    async def list_characters(self, interaction: discord.Interaction):
        characters = list_characters_by_owner(interaction.user.id)
        if not characters:
            await interaction.response.send_message(
                "You haven't created any characters yet. Use /character create to make one.",
                ephemeral=True,
            )
            return

        lines = [f"- {c.name}" for c in characters]
        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s Characters",
            description="\n".join(lines),
        )
        embed.set_footer(text="Use /character view name:<name> for full details.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="view", description="View a character's stats")
    @app_commands.describe(
        name="Character name",
        public="Show this to everyone in the channel instead of just you",
    )
    async def view(self, interaction: discord.Interaction, name: str, public: bool = False):
        character = load_character(name)
        if character is None:
            await interaction.response.send_message(f"No character named {name}.", ephemeral=True)
            return

        is_owner = interaction.user.id == character.owner_id
        if not (is_owner or is_admin(interaction)):
            await interaction.response.send_message(
                "Only this character's owner or an admin can view its stats.", ephemeral=True
            )
            return

        owner_name = await resolve_owner_name(interaction.guild, character.owner_id)
        embed = build_character_embed(character, owner_name)
        await interaction.response.send_message(embed=embed, ephemeral=not public)

    @app_commands.command(name="edit", description="Edit one of your characters")
    @app_commands.describe(
        name="Character to edit",
        new_name="Rename the character (optional)",
        avatar="New avatar image (optional)",
        hp="New max HP, also heals to full (optional)",
        sp="New SP (optional)",
        speed="New Speed (optional)",
        power="New Power (optional)",
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        name: str,
        new_name: str | None = None,
        avatar: discord.Attachment | None = None,
        hp: int | None = None,
        sp: int | None = None,
        speed: int | None = None,
        power: int | None = None,
    ):
        character = load_character(name)
        if character is None:
            await interaction.response.send_message(f"No character named {name}.", ephemeral=True)
            return

        is_owner = interaction.user.id == character.owner_id
        if not (is_owner or is_admin(interaction)):
            await interaction.response.send_message(
                "Only this character's owner or an admin can edit it.", ephemeral=True
            )
            return

        changes = []

        if new_name is not None and new_name != character.name:
            if character_exists(new_name):
                await interaction.response.send_message(
                    f"A character named {new_name} already exists.", ephemeral=True
                )
                return
            delete_character(character.name)
            changes.append(f"name -> {new_name}")
            character.name = new_name

        if avatar is not None:
            character.avatar_url = avatar.url
            changes.append("avatar updated")

        if hp is not None:
            character.hp = hp
            character.max_hp = hp
            changes.append(f"HP -> {hp}")

        if sp is not None:
            character.sp = sp
            changes.append(f"SP -> {sp}")

        if speed is not None:
            character.speed = speed
            changes.append(f"Speed -> {speed}")

        if power is not None:
            character.power = power
            changes.append(f"Power -> {power}")

        if not changes:
            await interaction.response.send_message(
                "Nothing to change, no fields were provided.", ephemeral=True
            )
            return

        save_character(character)
        await interaction.response.send_message(
            f"Updated {character.name}: {', '.join(changes)}.", ephemeral=True
        )

    @app_commands.command(name="resistance", description="Set one of a character's resistances")
    @app_commands.describe(
        name="Character name",
        resistance_type="Which resistance to set",
        value="Resistance percent. Over 100 fully blocks it, negative is a weakness (takes more)",
    )
    @app_commands.choices(
        resistance_type=[app_commands.Choice(name=t.capitalize(), value=t) for t in ALL_RESISTANCE_TYPES]
    )
    async def resistance(
        self,
        interaction: discord.Interaction,
        name: str,
        resistance_type: app_commands.Choice[str],
        value: int,
    ):
        character = load_character(name)
        if character is None:
            await interaction.response.send_message(f"No character named {name}.", ephemeral=True)
            return

        is_owner = interaction.user.id == character.owner_id
        if not (is_owner or is_admin(interaction)):
            await interaction.response.send_message(
                "Only this character's owner or an admin can edit it.", ephemeral=True
            )
            return

        character.resistances[resistance_type.value] = value
        save_character(character)
        await interaction.response.send_message(
            f"Set {character.name}'s {resistance_type.name} resistance to {value}%.", ephemeral=True
        )

    @app_commands.command(name="delete", description="Delete one of your characters")
    @app_commands.describe(name="Character to delete")
    async def delete(self, interaction: discord.Interaction, name: str):
        character = load_character(name)
        if character is None:
            await interaction.response.send_message(f"No character named {name}.", ephemeral=True)
            return

        is_owner = interaction.user.id == character.owner_id
        if not (is_owner or is_admin(interaction)):
            await interaction.response.send_message(
                "Only this character's owner or an admin can delete it.", ephemeral=True
            )
            return

        delete_character(character.name)
        await interaction.response.send_message(f"Deleted {character.name}.", ephemeral=True)

    @app_commands.command(name="say", description="Speak in character, using this character's name and avatar")
    @app_commands.describe(
        name="Character name",
        message="What to say",
    )
    async def say(self, interaction: discord.Interaction, name: str, message: str):
        character = load_character(name)
        if character is None:
            await interaction.response.send_message(f"No character named {name}.", ephemeral=True)
            return

        is_owner = interaction.user.id == character.owner_id
        if not (is_owner or is_admin(interaction)):
            await interaction.response.send_message(
                "Only this character's owner or an admin can speak as it.", ephemeral=True
            )
            return

        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "This only works in a regular text channel.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            webhook = await get_or_create_webhook(interaction.channel)
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to manage webhooks in this channel. "
                "An admin needs to grant me the Manage Webhooks permission.",
                ephemeral=True,
            )
            return

        await webhook.send(
            content=message,
            username=character.name,
            avatar_url=character.avatar_url,
        )
        await interaction.followup.send("Sent.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CharacterCog(bot))