import discord
from discord.ext import commands
from discord import app_commands

from game.battle import (
    Battle, Fighter, DeclaredAction, BATTLE_TYPES,
    SANITY_CLASH_WIN, SANITY_CLASH_LOSS, SANITY_PER_HEADS_UNOPPOSED,
)
from game.skills import Skill, SkillResult, ClashOutcome, resolve_skill, resolve_round_clash
from game.colors import get_status_color
from game.character import load_character
from game.statuses import StatusInstance, decay_after_trigger, apply_status, INFLICTABLE_STATUSES
from game.resistances import DAMAGE_TYPES, apply_resistance
from game.emojis import status_emoji, coin_emoji, damage_type_emoji, stat_emoji, skill_slot_emoji

LOG_CHANNEL_ID = 1538071557560213595  # #bot-combat-logs
MAX_EMBED_FIELDS = 25  # Discord's hard limit on fields per embed


class SkillConfirmView(discord.ui.View):
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


def _skill_preview_text(skill: Skill) -> str:
    """Short, readable summary of a skill's numbers, used both in the
    addskill confirmation and in the clash scouting preview shown to a
    caster before they lock in a declare.
    """
    coin_bits = []
    for i in range(skill.coins):
        tag = coin_emoji("base")
        if skill.coin_statuses[i]:
            tag += f" ({skill.coin_status_potencies[i]}{status_emoji(skill.coin_statuses[i])}{skill.coin_status_counts[i]})"
        coin_bits.append(tag)
    return (
        f"**{skill.name}** (Base {skill.base_power}, +{skill.coin_power} Coin Power, "
        f"{skill.coins} coins, {damage_type_emoji(skill.damage_type)} {skill.damage_type.capitalize()})\n"
        + " ".join(coin_bits)
    )


class ClashDeclareView(discord.ui.View):
    """Shown only to the declaring user, never the target. Confirms
    locking in a slot-targeted declare, either an informed choice
    (caster's slot was fast enough to scout the target's slot) or a
    blind one (too slow, or the target hasn't assigned that slot yet),
    in which case this doubles as the "this may resolve unopposed"
    warning.
    """
    def __init__(
        self,
        caster: Fighter,
        skill: Skill,
        target: Fighter,
        slot: int,
        target_slot: int,
        bot: commands.Bot,
        battle: Battle,
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.caster = caster
        self.skill = skill
        self.target = target
        self.slot = slot
        self.target_slot = target_slot
        self.bot = bot
        self.battle = battle

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.caster.declare_in_slot(self.slot, self.skill, self.target, self.target_slot)

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(
                f"**Locked in.** {self.caster.name}'s Slot {self.slot} ({self.skill.name}) "
                f"targets {self.target.name}'s Slot {self.target_slot}."
            ),
            view=self,
        )
        self.stop()
        await sync_battle_message(self.bot, self.battle)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. Nothing was declared.", view=self)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


def format_skill_result(result: SkillResult, survived: list[bool] | None = None) -> str:
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
    embed.add_field(
        name="Slot Speeds",
        value=" ".join(f"`{s}`" for s in fighter.slot_speeds),
        inline=True,
    )
    if fighter.statuses:
        lines = [
            f"{status_emoji(s.name)} {s.name.capitalize()}: {s.potency}/{s.count}"
            for s in fighter.statuses.values()
        ]
        embed.add_field(name="Statuses", value="\n".join(lines), inline=False)
    if not fighter.is_alive():
        embed.description = "Down"
    return embed


def build_battle_embed(battle: Battle) -> discord.Embed:
    """The persistent, edit-in-place battlefield view. Each fighter's
    slot line shows that slot's own rolled Speed next to its icon
    (filled = coin icon, unfilled = numbered slot icon) since slot
    speeds are public information both sides can see and plan around.
    """
    type_info = BATTLE_TYPES.get(battle.battle_type, BATTLE_TYPES["spar"])
    embed = discord.Embed(title=type_info["title"], color=type_info["color"])

    for side_name in ("A", "B"):
        fighters = sorted(battle.side(side_name), key=lambda f: max(f.slot_speeds, default=0), reverse=True)
        if not fighters:
            continue

        lines = []
        for f in fighters:
            filled = f.slots_filled()
            slot_parts = []
            for i in range(f.skill_slots):
                slot_num = i + 1
                speed_val = f.slot_speeds[i] if i < len(f.slot_speeds) else "?"
                icon = coin_emoji("base") if slot_num in f.declared_actions else skill_slot_emoji(slot_num)
                slot_parts.append(f"{icon}`{speed_val}`")
            slot_line = " ".join(slot_parts)

            if filled == 0:
                tag = ""
            elif filled >= f.skill_slots:
                tag = " -- **DECLARED**"
            else:
                tag = f" -- **{filled}/{f.skill_slots} declared**"
            down_tag = " (Down)" if not f.is_alive() else ""
            lines.append(
                f"**{f.name}**{down_tag} ({stat_emoji('hp')} {f.hp}/{f.max_hp}, "
                f"{stat_emoji('sanity')} {f.sanity}){tag}\n{slot_line}"
            )
        embed.add_field(name=f"Side {side_name}", value="\n\n".join(lines), inline=False)

    if not battle.fighters:
        embed.description = "No fighters added yet. Use /battle addfighter."

    embed.set_footer(text=f"Round {battle.round_number}")
    return embed


async def sync_battle_message(bot: commands.Bot, battle: Battle):
    if battle.message_id is None:
        return
    channel = bot.get_channel(battle.channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(battle.message_id)
    except (discord.NotFound, discord.Forbidden):
        return
    await message.edit(embed=build_battle_embed(battle))


def apply_incoming_hit(attacker_skill: Skill, result: SkillResult, target: Fighter) -> tuple[int, list[str]]:
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
    @app_commands.describe(battle_type="Spar, Standard Encounter, or Fatal Battle")
    @app_commands.choices(
        battle_type=[
            app_commands.Choice(name="Spar", value="spar"),
            app_commands.Choice(name="Standard Encounter (Proelium Commune)", value="standard"),
            app_commands.Choice(name="Fatal Battle (Proelium Fatale)", value="fatal"),
        ]
    )
    async def create(self, interaction: discord.Interaction, battle_type: app_commands.Choice[str] = None):
        channel_id = interaction.channel_id
        if channel_id in self.battles:
            await interaction.response.send_message(
                "There's already an active battle in this channel. Use /battle end first.",
                ephemeral=True,
            )
            return

        battle = Battle(channel_id=channel_id, battle_type=battle_type.value if battle_type else "spar")
        self.battles[channel_id] = battle

        await interaction.response.send_message(embed=build_battle_embed(battle))
        message = await interaction.original_response()
        battle.message_id = message.id

    @app_commands.command(name="addfighter", description="Add a fighter to the current battle")
    @app_commands.describe(
        side="A or B",
        character_name="Pull stats, avatar, and resistances from a saved character (optional)",
        name="Fighter's name, only used if not pulling from a saved character",
        hp="Starting HP, only used if not pulling from a saved character (default 100)",
        speed_min="Lowest a skill slot's Speed can roll (default: flat 10 if omitted)",
        speed_max="Highest a skill slot's Speed can roll (default: same as speed_min)",
    )
    async def addfighter(
        self,
        interaction: discord.Interaction,
        side: str,
        character_name: str | None = None,
        name: str | None = None,
        hp: int = 100,
        speed_min: int | None = None,
        speed_max: int | None = None,
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
            if speed_min is not None:
                fighter.speed_min = speed_min
                fighter.speed_max = speed_max if speed_max is not None else speed_min
                fighter.roll_slot_speeds()
            battle.add_fighter(fighter)
            await interaction.response.send_message(
                f"Added {fighter.name} to Side {side}.", ephemeral=True
            )
            await sync_battle_message(self.bot, battle)
            return

        if battle.get_fighter(name) is not None:
            await interaction.response.send_message(
                f"A fighter named {name} already exists in this battle.", ephemeral=True
            )
            return

        fighter = Fighter(name=name, side=side, hp=hp, max_hp=hp)
        if speed_min is not None:
            fighter.speed_min = speed_min
            fighter.speed_max = speed_max if speed_max is not None else speed_min
            fighter.roll_slot_speeds()
        battle.add_fighter(fighter)
        await interaction.response.send_message(f"Added {name} to Side {side} ({hp} HP).", ephemeral=True)
        await sync_battle_message(self.bot, battle)

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

        preview_text = f"{target_fighter.name} would learn {skill_name}\n" + _skill_preview_text(skill)

        view = SkillConfirmView(target_fighter, skill, preview_text)
        await interaction.response.send_message(preview_text, view=view, ephemeral=True)

    @app_commands.command(
        name="declare",
        description="Assign a skill to one of your slots, aimed at a specific slot on your target",
    )
    @app_commands.describe(
        fighter="Who is declaring",
        slot="Which of YOUR skill slots to use (1-3)",
        skill_name="Which skill they're using",
        target="Who they're targeting",
        target_slot="Which of the TARGET's skill slots to aim at (1-3)",
    )
    async def declare(
        self,
        interaction: discord.Interaction,
        fighter: str,
        slot: int,
        skill_name: str,
        target: str,
        target_slot: int,
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

        if not (1 <= slot <= caster.skill_slots):
            await interaction.response.send_message(
                f"{caster.name} only has skill slots 1-{caster.skill_slots}.", ephemeral=True
            )
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

        if target_fighter is caster:
            await interaction.response.send_message(
                f"{caster.name} can't target themselves.", ephemeral=True
            )
            return

        if target_fighter.side == caster.side:
            await interaction.response.send_message(
                f"{caster.name} can't target {target_fighter.name}, they're on the same side "
                f"(Side {caster.side}). Only enemies can be targeted.",
                ephemeral=True,
            )
            return

        if not (1 <= target_slot <= target_fighter.skill_slots):
            await interaction.response.send_message(
                f"{target_fighter.name} only has skill slots 1-{target_fighter.skill_slots}.",
                ephemeral=True,
            )
            return

        caster_speed = caster.slot_speed(slot)
        target_speed = target_fighter.slot_speed(target_slot)
        scouted_skill = target_fighter.get_declared_skill_in_slot(target_slot)

        can_scout = caster_speed >= target_speed and scouted_skill is not None

        view = ClashDeclareView(caster, skill, target_fighter, slot, target_slot, self.bot, battle)

        if can_scout:
            preview = (
                f"{caster.name}'s Slot {slot} (Speed {caster_speed}) is fast enough to read "
                f"{target_fighter.name}'s Slot {target_slot} (Speed {target_speed}) before committing.\n\n"
                f"They're bringing:\n{_skill_preview_text(scouted_skill)}\n\n"
                f"Confirm to lock in {skill.name} against it?"
            )
        else:
            reason = (
                "your slot is slower than theirs" if caster_speed < target_speed
                else f"{target_fighter.name} hasn't assigned a skill to that slot yet"
            )
            preview = (
                f"You can't scout {target_fighter.name}'s Slot {target_slot} ({reason}). "
                f"If they don't end up targeting your Slot {slot} back, this will resolve as an "
                f"**unopposed** attack instead of a Clash.\n\n"
                f"Confirm to lock in {skill.name} against it anyway?"
            )

        await interaction.response.send_message(preview, view=view, ephemeral=True)

    @app_commands.command(name="undeclare", description="Cancel one of your declared skill slots")
    @app_commands.describe(fighter="Who is cancelling", slot="Which slot to clear (1-3)")
    async def undeclare(self, interaction: discord.Interaction, fighter: str, slot: int):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message("No active battle here.", ephemeral=True)
            return

        caster = battle.get_fighter(fighter)
        if caster is None:
            await interaction.response.send_message(f"No fighter named {fighter}.", ephemeral=True)
            return

        if caster.undeclare(slot):
            await interaction.response.send_message(
                f"Cleared {caster.name}'s Slot {slot}.", ephemeral=True
            )
            await sync_battle_message(self.bot, battle)
        else:
            await interaction.response.send_message(
                f"{caster.name}'s Slot {slot} wasn't declared.", ephemeral=True
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

        embed = discord.Embed(
            title=f"Combat Phase, Round {battle.round_number}",
            color=0x5865F2,
        )
        truncated = False

        def add_field_safe(name: str, value: str):
            nonlocal truncated
            if len(embed.fields) >= MAX_EMBED_FIELDS:
                truncated = True
                return
            embed.add_field(name=name, value=value, inline=False)

        entries = []
        for f in battle.fighters:
            if not f.is_alive():
                continue
            for slot_num, action in f.declared_actions.items():
                entries.append({
                    "caster": f, "skill": action.skill, "target": action.target,
                    "slot": slot_num, "target_slot": action.target_slot, "used": False,
                })

        units = []
        for i, entry in enumerate(entries):
            if entry["used"]:
                continue
            match = None
            for other in entries[i + 1:]:
                if other["used"]:
                    continue
                if (
                    other["caster"] is entry["target"]
                    and other["target"] is entry["caster"]
                    and other["target_slot"] == entry["slot"]
                    and entry["target_slot"] == other["slot"]
                ):
                    match = other
                    break
            entry["used"] = True
            if match is not None:
                match["used"] = True
                units.append(("clash", entry, match))
            else:
                units.append(("solo", entry))

        def unit_speed(u):
            if u[0] == "clash":
                a_speed = u[1]["caster"].slot_speed(u[1]["slot"])
                b_speed = u[2]["caster"].slot_speed(u[2]["slot"])
                return max(a_speed, b_speed)
            return u[1]["caster"].slot_speed(u[1]["slot"])

        units.sort(key=unit_speed, reverse=True)

        for u in units:
            if u[0] == "clash":
                _, entry_a, entry_b = u
                fighter_a, fighter_b = entry_a["caster"], entry_b["caster"]
                if not fighter_a.is_alive() or not fighter_b.is_alive():
                    continue

                outcome = resolve_round_clash(
                    entry_a["skill"], entry_b["skill"],
                    heads_chance_a=fighter_a.heads_chance(),
                    heads_chance_b=fighter_b.heads_chance(),
                )
                winner = fighter_a if outcome.winner == "a" else fighter_b
                loser = fighter_b if winner is fighter_a else fighter_a
                winner_skill = entry_a["skill"] if outcome.winner == "a" else entry_b["skill"]

                winner.gain_sanity(SANITY_CLASH_WIN)
                loser.lose_sanity(SANITY_CLASH_LOSS)

                total_damage, status_log = apply_incoming_hit(
                    winner_skill, outcome.winner_final_result, loser
                )
                loser.take_damage(total_damage)

                field_value = format_clash_rounds(outcome, fighter_a.name, fighter_b.name)
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

                add_field_safe(
                    f"Clash: {fighter_a.name} Slot{entry_a['slot']} vs {fighter_b.name} Slot{entry_b['slot']}",
                    field_value,
                )

            else:
                _, entry = u
                fighter = entry["caster"]
                target = entry["target"]
                if not fighter.is_alive() or not target.is_alive():
                    continue

                result = resolve_skill(entry["skill"], fighter.heads_chance())
                heads_landed = sum(1 for c in result.coin_results if c.heads)
                sanity_gain = heads_landed * SANITY_PER_HEADS_UNOPPOSED
                fighter.gain_sanity(sanity_gain)

                total_damage, status_log = apply_incoming_hit(entry["skill"], result, target)
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

                add_field_safe(
                    f"{fighter.name} Slot{entry['slot']} attacks {target.name} Slot{entry['target_slot']} (unopposed)",
                    field_value,
                )

        if truncated:
            embed.set_footer(text="Some actions this round were too numerous to display and were skipped.")

        battle.start_new_round()
        await interaction.response.send_message(embed=embed)
        await sync_battle_message(self.bot, battle)

    @app_commands.command(name="end", description="End the battle in this channel")
    async def end(self, interaction: discord.Interaction):
        if interaction.channel_id in self.battles:
            del self.battles[interaction.channel_id]
            await interaction.response.send_message("Battle ended.")
        else:
            await interaction.response.send_message("No active battle here.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BattleCog(bot))