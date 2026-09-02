import asyncio
import random

import discord
from discord.ext import commands
from discord import app_commands

from game.battle import (
    Battle, Fighter, DeclaredAction, BATTLE_TYPES,
    SANITY_CLASH_WIN, SANITY_CLASH_LOSS, SANITY_PER_HEADS_UNOPPOSED,
)
from game.skills import Skill, SkillResult, ClashOutcome, resolve_skill, resolve_round_clash, resolve_triggers
from game.conditions import Trigger, TriggerContext, parse_trigger_text, TriggerParseError
from game.character import load_character
from game.statuses import StatusInstance, decay_after_trigger, apply_status, INFLICTABLE_STATUSES
from game.resistances import DAMAGE_TYPES, ALL_RESISTANCE_TYPES, apply_resistance
from game.emojis import status_emoji, coin_emoji, coin_roll_emoji, damage_type_emoji, stat_emoji, skill_slot_emoji, hint_emoji

LOG_CHANNEL_ID = 1538071557560213595  # #bot-combat-logs
ADMIN_ROLE_ID = 1468446442430533737  # can manage any fighter, not just their own

# Magnitude (potency * count) thresholds for the status-based half of a
# fighter's Hint tier, checked highest first. Rupture then gets a flat
# +1 on top of whatever tier its own magnitude lands on, since its
# automatic on-hit trigger makes it a real threat even at low numbers,
# not because it's inherently worse than the others at equal magnitude.
STATUS_HINT_THRESHOLDS = [(15, 3), (6, 2), (1, 1)]


def _can_manage_fighter(interaction: discord.Interaction, fighter: Fighter) -> bool:
    """True if whoever's invoking this is either the fighter's own linked
    owner, or holds the server's admin role (ADMIN_ROLE_ID). Used for
    destructive fighter-management actions like /battle removefighter,
    where "your own fighter, or an admin" is the right bar -- same
    pattern /character edit/delete already use, just role-based instead
    of Discord's built-in manage_guild permission, since that's what was
    asked for here specifically.
    """
    if fighter.owner_id is not None and interaction.user.id == fighter.owner_id:
        return True
    if isinstance(interaction.user, discord.Member):
        return any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)
    return False


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
    coin_bits = []
    for i in range(skill.coins):
        tag = coin_emoji("base")
        if skill.coin_statuses[i]:
            tag += f" ({skill.coin_status_potencies[i]}{status_emoji(skill.coin_statuses[i])}{skill.coin_status_counts[i]})"
        coin_bits.append(tag)
    text = (
        f"**{skill.name}** (Base {skill.base_power}, +{skill.coin_power} Coin Power, "
        f"{skill.coins} coins, {damage_type_emoji(skill.damage_type)} {skill.damage_type.capitalize()})\n"
        + " ".join(coin_bits)
    )
    if skill.triggers:
        trigger_lines = "\n".join(
            f"  Trigger: {t.condition.type} -> {t.effect_type}"
            f"{f' {t.effect_value}' if t.effect_type != 'inflict_status' else f' {t.status_name} {t.effect_value}/{t.status_count}'}"
            f" ({hint_emoji(t.hint_tier)})"
            for t in skill.triggers
        )
        text += f"\n{trigger_lines}"
    if skill.tags:
        text += f"\n  Flags: {', '.join(sorted(skill.tags))}"
    return text


def _parse_status_tokens(
    status_input: str, coins: int
) -> tuple[list[str | None], list[int], list[int], str | None]:
    """Parses the comma-separated per-coin status string ('none' or
    'Name:Potency:Count' per coin) into three aligned lists. Returns
    (coin_statuses, potencies, counts, error) -- error is None on
    success, or a user-facing message on failure (ignore the lists in
    that case). Pulled out of addskill's old body so the new popup's
    on_submit can share the exact same parsing/error text.
    """
    tokens = [t.strip() for t in status_input.split(",")]
    if len(tokens) == 1 and tokens[0].lower() == "none":
        tokens = ["none"] * coins

    if len(tokens) != coins:
        return [], [], [], (
            f"Statuses needs exactly {coins} comma-separated entries (one per coin), "
            f"got {len(tokens)}. Use 'none' for a coin with no status. "
            f"Example: {','.join(['none'] * coins)}"
        )

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
            return [], [], [], f"Coin {i + 1} entry '{token}' is invalid. Use 'Name:Potency:Count' or 'none'."

        status_name_raw, potency_raw, count_raw = parts
        try:
            potency = int(potency_raw)
            count = int(count_raw)
        except ValueError:
            return [], [], [], f"Coin {i + 1} entry '{token}' has a non-numeric potency/count."

        coin_statuses.append(status_name_raw.strip().lower())
        coin_status_potencies.append(potency)
        coin_status_counts.append(count)

    return coin_statuses, coin_status_potencies, coin_status_counts, None


class AddSkillModal(discord.ui.Modal, title="New Skill"):
    """The full skill-creation popup: everything /battle addskill used to
    collect as 6 required slash-command options (base_power, coin_power,
    coins, damage_type, status_input) PLUS the separate trigger modal is
    now just this one popup. addskill itself only takes `fighter` --
    who's learning it -- and opens this immediately.

    Discord caps a modal at 5 components. To fit within that, Base
    Power / Coin Power / Coins / Damage Type are packed into one
    comma-separated line ("5, 5, 3, Blunt") and parsed by hand in
    on_submit below, same pattern status_input already used for
    per-coin data -- there wasn't room to give each its own box.
    """

    skill_name = discord.ui.TextInput(
        label="Skill Name",
        style=discord.TextStyle.short,
        max_length=100,
    )
    stats_input = discord.ui.TextInput(
        label="Base Power, Coin Power, Coins, Damage Type",
        style=discord.TextStyle.short,
        placeholder="5, 5, 3, Blunt",
    )
    status_input = discord.ui.TextInput(
        label="Per-coin statuses, or 'none'",
        style=discord.TextStyle.short,
        placeholder="none   or   Tremor:2:0,Tremor:2:0,Tremor:2:0",
        default="none",
        required=False,
    )
    triggers_input = discord.ui.TextInput(
        label="Triggers (one per line, blank for none)",
        style=discord.TextStyle.paragraph,
        placeholder=(
            "[On Use] Coin Power +1\n"
            ":Coin1: [On Hit] Inflict 2 Rupture Potency, 1 Count\n"
            "[Target Fixed]"
        ),
        required=False,
        max_length=4000,
    )

    def __init__(self, target_fighter: Fighter):
        super().__init__()
        self.target_fighter = target_fighter

    async def on_submit(self, interaction: discord.Interaction):
        stats_parts = [p.strip() for p in self.stats_input.value.split(",")]
        if len(stats_parts) != 4:
            await interaction.response.send_message(
                "Base Power, Coin Power, Coins, Damage Type needs exactly 4 comma-separated "
                "values, e.g. '5, 5, 3, Blunt'.",
                ephemeral=True,
            )
            return

        base_power_raw, coin_power_raw, coins_raw, damage_type_raw = stats_parts
        try:
            base_power = int(base_power_raw)
            coin_power = int(coin_power_raw)
            coins = int(coins_raw)
        except ValueError:
            await interaction.response.send_message(
                "Base Power, Coin Power, and Coins must all be whole numbers.",
                ephemeral=True,
            )
            return

        if not (1 <= coins <= 4):
            await interaction.response.send_message("Coins must be between 1 and 4.", ephemeral=True)
            return

        damage_type = damage_type_raw.strip().lower()
        if damage_type not in DAMAGE_TYPES:
            await interaction.response.send_message(
                f"'{damage_type_raw}' isn't a valid damage type. Choose one of: "
                f"{', '.join(t.capitalize() for t in DAMAGE_TYPES)}.",
                ephemeral=True,
            )
            return

        coin_statuses, coin_status_potencies, coin_status_counts, status_error = _parse_status_tokens(
            self.status_input.value or "none", coins
        )
        if status_error:
            await interaction.response.send_message(status_error, ephemeral=True)
            return

        try:
            triggers, flags = parse_trigger_text(self.triggers_input.value or "")
        except TriggerParseError as e:
            await interaction.response.send_message(
                f"Trigger line error on `{e.line.strip()}`: {e.reason}",
                ephemeral=True,
            )
            return

        skill = Skill(
            name=self.skill_name.value,
            base_power=base_power,
            coin_power=coin_power,
            coins=coins,
            damage_type=damage_type,
            coin_statuses=coin_statuses,
            coin_status_potencies=coin_status_potencies,
            coin_status_counts=coin_status_counts,
            triggers=triggers,
            tags=flags,
        )

        preview_text = f"{self.target_fighter.name} would learn {self.skill_name.value}\n" + _skill_preview_text(skill)
        view = SkillConfirmView(self.target_fighter, skill, preview_text)
        await interaction.response.send_message(preview_text, view=view, ephemeral=True)


# Discord silently rejects an ENTIRE modal with a 400 (which the caller
# only ever sees as a generic "The application did not respond") if any
# single TextInput's label is over 45 chars or placeholder is over 100
# -- this bit us for real once (AddSkillModal's status_input label was
# 46 chars, triggers_input's placeholder was 119). This check runs once
# at import time and fails loudly and immediately if it ever regresses,
# instead of silently again at some future /battle addskill call.
def _check_modal_field_limits(modal_cls: type[discord.ui.Modal]) -> None:
    for field_name in dir(modal_cls):
        field = getattr(modal_cls, field_name, None)
        if not isinstance(field, discord.ui.TextInput):
            continue
        label = field.label or ""
        if len(label) > 45:
            raise ValueError(
                f"{modal_cls.__name__}.{field_name} label is {len(label)} chars "
                f"(Discord's limit is 45): {label!r}"
            )
        placeholder = field.placeholder or ""
        if len(placeholder) > 100:
            raise ValueError(
                f"{modal_cls.__name__}.{field_name} placeholder is {len(placeholder)} chars "
                f"(Discord's limit is 100): {placeholder!r}"
            )


_check_modal_field_limits(AddSkillModal)


class ClashDeclareView(discord.ui.View):
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


class StealApprovalView(discord.ui.View):
    def __init__(
        self,
        stealer: Fighter,
        stealer_skill: Skill,
        stealer_slot: int,
        ally: Fighter,
        ally_slot: int,
        target: Fighter,
        target_slot: int,
        bot: commands.Bot,
        battle: Battle,
        timeout: float = 120,
    ):
        super().__init__(timeout=timeout)
        self.stealer = stealer
        self.stealer_skill = stealer_skill
        self.stealer_slot = stealer_slot
        self.ally = ally
        self.ally_slot = ally_slot
        self.target = target
        self.target_slot = target_slot
        self.bot = bot
        self.battle = battle
        self.responded = False
        self.message: discord.Message | None = None

    def _lock_in_unopposed(self):
        self.stealer.declare_in_slot(
            self.stealer_slot, self.stealer_skill, self.target, self.target_slot
        )

    async def _notify_overtaken(self, previous_holder: Fighter):
        if previous_holder.owner_id is None:
            return
        owner = self.bot.get_user(previous_holder.owner_id)
        if owner is None:
            try:
                owner = await self.bot.fetch_user(previous_holder.owner_id)
            except discord.NotFound:
                return
        try:
            await owner.send(
                f"{self.stealer.name} has taken over the clash against {self.target.name}'s "
                f"Slot {self.target_slot} instead of {previous_holder.name}. {previous_holder.name}'s "
                f"action there will resolve unopposed unless you declare something else."
            )
        except discord.Forbidden:
            pass

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.responded:
            await interaction.response.send_message("This request was already answered.", ephemeral=True)
            return
        self.responded = True

        target_action = self.target.declared_actions.get(self.target_slot)
        previous_holder = target_action.target if target_action is not None else None

        if previous_holder is self.ally:
            self.ally.undeclare(self.ally_slot)

        if target_action is not None:
            target_action.target = self.stealer
            target_action.target_slot = self.stealer_slot

        self.stealer.declare_in_slot(
            self.stealer_slot, self.stealer_skill, self.target, self.target_slot
        )

        if previous_holder is not None and previous_holder is not self.ally:
            await self._notify_overtaken(previous_holder)

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(
                f"Approved. {self.stealer.name}'s Slot {self.stealer_slot} now clashes "
                f"{self.target.name}'s Slot {self.target_slot} instead. Your Slot {self.ally_slot} "
                f"was cleared, use /battle declare again if you want to act elsewhere."
            ),
            view=self,
        )
        self.stop()
        await sync_battle_message(self.bot, self.battle)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.responded:
            await interaction.response.send_message("This request was already answered.", ephemeral=True)
            return
        self.responded = True
        self._lock_in_unopposed()

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Declined. Your clash against {self.target.name} continues as declared.",
            view=self,
        )
        self.stop()
        await sync_battle_message(self.bot, self.battle)

    async def on_timeout(self):
        if self.responded:
            return
        self.responded = True
        self._lock_in_unopposed()

        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "No response in time, treated as a decline. Your clash "
                        f"against {self.target.name} continues as declared."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass
        await sync_battle_message(self.bot, self.battle)


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


def _status_hint_tier(fighter: Fighter) -> int:
    """The worse of a fighter's currently active statuses, mapped to a
    tier by magnitude (potency * count). Rupture gets a flat +1 on top
    of its magnitude tier, capped at 3, since its automatic on-hit
    trigger makes any active Rupture a live threat regardless of size.
    """
    best = 0
    for status in fighter.statuses.values():
        if status.count <= 0:
            continue
        magnitude = status.potency * status.count
        tier = 0
        for threshold, t in STATUS_HINT_THRESHOLDS:
            if magnitude >= threshold:
                tier = t
                break
        if status.name == "rupture":
            tier = min(3, max(tier, 1) + 1) if tier > 0 else 2
        best = max(best, tier)
    return best


def _skill_hint_tier(fighter: Fighter, battle: Battle) -> int:
    """The highest hint_tier among the fighter's known skills whose
    trigger condition currently reads true against at least one living
    enemy. Speculative, this doesn't require the skill to actually be
    declared, it's meant to warn "this fighter COULD hit hard right now
    if they use this."
    """
    enemies = [f for f in battle.fighters if f.side != fighter.side and f.is_alive()]
    if not enemies:
        return 0
    best = 0
    for skill in fighter.skills.values():
        for trigger in skill.triggers:
            for enemy in enemies:
                context = TriggerContext(caster=fighter, target=enemy, battle=battle)
                from game.conditions import evaluate_condition
                if evaluate_condition(trigger.condition, context):
                    best = max(best, trigger.hint_tier)
                    break
    return best


def compute_hint_tier(fighter: Fighter, battle: Battle) -> int | None:
    """The tier to show on this fighter's Hint line: the higher of their
    live-triggerable skill danger and their active status danger.
    Returns None if there's nothing worth flagging.
    """
    tier = max(_skill_hint_tier(fighter, battle), _status_hint_tier(fighter))
    return tier if tier > 0 else None


def build_battle_embed(battle: Battle) -> discord.Embed:
    type_info = BATTLE_TYPES.get(battle.battle_type, BATTLE_TYPES["spar"])
    embed = discord.Embed(title=type_info["title"], color=type_info["color"])
    if "image" in type_info:
        embed.set_image(url=type_info["image"])

    for side_name in ("A", "B"):
        fighters = sorted(battle.side(side_name), key=lambda f: max(f.slot_speeds, default=0), reverse=True)
        if not fighters:
            continue

        speed_icon = stat_emoji("speed")
        lines = []
        for f in fighters:
            filled = f.slots_filled()
            slot_parts = []
            for i in range(f.skill_slots):
                slot_num = i + 1
                speed_val = f.slot_speeds[i] if i < len(f.slot_speeds) else "?"
                icon = coin_emoji("base") if slot_num in f.declared_actions else skill_slot_emoji(slot_num)
                slot_parts.append(f"{icon}`{speed_val}`{speed_icon}")
            slot_line = " ".join(slot_parts)

            if filled == 0:
                tag = ""
            elif filled >= f.skill_slots:
                tag = " -- **DECLARED**"
            else:
                tag = f" -- **{filled}/{f.skill_slots} declared**"
            down_tag = " (Down)" if not f.is_alive() else ""

            hint_tier = compute_hint_tier(f, battle)
            # Just the icon now, no trailing " Hint" text -- the emoji
            # alone is the signal, spelling it out was redundant.
            hint_line = f"\n-# {hint_emoji(hint_tier)}" if hint_tier else ""

            lines.append(
                f"**{f.name}**{down_tag} ({stat_emoji('hp')} {f.hp}/{f.max_hp}, "
                f"{stat_emoji('sanity')} {f.sanity}){tag}\n{slot_line}{hint_line}"
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


def apply_incoming_hit(
    attacker_skill: Skill, result: SkillResult, target: Fighter, caster: Fighter,
    skip_evasion: bool = False,
) -> tuple[int, list[str], list[Trigger], int]:
    """Applies each landed coin's damage/status, and collects whichever
    per-coin Triggers (on_hit/heads_hit/tails_hit) fired on that coin
    (see CoinResult.fired_triggers), so the caller can hand them to
    apply_trigger_effects alongside the skill-level ones. `result` here
    is always the coins that actually landed -- attrition rounds during
    a clash never reach this function, only the final toss does -- so
    every coin iterated below is a real hit.

    `caster` is needed so a Crit coin (see CoinResult.is_crit) can
    consume 1 count off the caster's real Poise stack here -- resolve_skill
    only computed is_crit against a local copy, it never touches the
    Fighter object (see its docstring in game/skills.py). Crit bonus
    damage is folded into the SAME resistance check as the coin's
    normal damage (still a hit of that skill's damage_type, just a
    harder one), unlike Rupture's bonus below, which is the TARGET's
    own reaction and deliberately bypasses resistance entirely.

    Returns evade_count too: how many of this result's coins had
    is_evaded set (computed in resolve_skill, see its Evasion docstring).
    An evaded coin is skipped entirely -- no resistance check, no
    Rupture, no coin status, doesn't add to total_damage -- and just
    logs the dodge, decaying 1 count off the target's real Evasion
    stack per dodge.

    skip_evasion=True bypasses the target's Evasion check entirely
    (every coin lands as if they had none) -- used specifically for a
    Counter skill's retaliation strike against the original attacker,
    since Counter is explicitly meant to bypass whatever defensive
    skills its target has active, not just deal normal damage to them.

    Note: the OLD Counter status/resource mechanic (flat retaliation
    damage whenever the target held a "counter" status) has been
    removed entirely -- Counter is now a Skill-level mechanic (see the
    [Counter]/[Clashable Counter] flags and find_eligible_counter /
    find_eligible_clashable_counter / apply_counter_redirects below),
    not something baked into every single hit here.
    """
    log: list[str] = []
    total_damage = 0
    per_coin_triggers: list[Trigger] = []
    evade_count = 0
    poise = caster.get_status("poise")
    evasion = None if skip_evasion else target.get_status("evasion")

    for i, coin in enumerate(result.coin_results):
        if coin.is_evaded and not skip_evasion:
            evade_count += 1
            log.append(
                f"Coin {i + 1}: {status_emoji('evasion')} {target.name} evades the hit."
            )
            if evasion is not None:
                evasion = decay_after_trigger(evasion)
                target.set_status_instance(evasion)
            continue

        per_coin_triggers.extend(coin.fired_triggers)
        raw = coin.damage_dealt + (coin.crit_bonus_damage if coin.is_crit else 0)
        resisted = apply_resistance(raw, target.resistances.get(attacker_skill.damage_type, 0))
        if resisted != raw:
            log.append(
                f"Coin {i + 1}: ({damage_type_emoji(attacker_skill.damage_type)} "
                f"{attacker_skill.damage_type.capitalize()} resistance: {raw} -> {resisted})"
            )
        coin_total = resisted

        if coin.is_crit:
            log.append(
                f"Coin {i + 1}: {status_emoji('poise')} Crit! {caster.name}'s Poise adds "
                f"+{coin.crit_bonus_damage} damage."
            )
            if poise is not None:
                poise = decay_after_trigger(poise)
                caster.set_status_instance(poise)

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

    return total_damage, log, per_coin_triggers, evade_count


def fire_evade_triggers(defender: Fighter, attacker: Fighter, battle: Battle, evade_count: int) -> list[str]:
    """Fires [On Evade] once per coin the defender actually evaded this
    hit (evade_count, from apply_incoming_hit above), swept across ALL
    of the defender's own known skills -- same passive-sweep pattern as
    fire_passive_triggers uses for [Combat Start]/[Turn Start], since
    the defender isn't the one whose skill is being resolved right now,
    they're reacting to someone else's attack, so there's no single
    "current skill" of theirs to check triggers against.

    caster on the context is the defender (the one who reacted), target
    is the attacker (so a condition like "if attacker has Rupture" or
    "if attacker has Fragile" reads naturally off the existing
    target_status condition type). Called once per evaded coin rather
    than once per hit, so a trigger like "[On Evade] Gain 2 Sanity"
    correctly stacks if a multi-coin skill gets partially evaded.
    """
    if evade_count <= 0:
        return []
    log: list[str] = []
    context = TriggerContext(caster=defender, target=attacker, battle=battle)
    for _ in range(evade_count):
        for skill in defender.skills.values():
            _, post_hit = resolve_triggers(skill, context, "on_evade")
            log.extend(apply_trigger_effects(post_hit, defender, attacker))
    return log


def find_eligible_counter(defender: Fighter, attacker_slot_speed: int) -> tuple[int, Skill] | None:
    """Counter is a reactive Skill-level mechanic, not a status: any
    skill flagged [Counter] that `defender` has DECLARED this round (in
    ANY of their slots -- it doesn't matter which one) is a standing
    threat against every incoming unopposed attack, all round, until it
    fires once. Eligible if defender hasn't already used their Counter
    this round (counter_used_this_round, reset every round in
    Fighter.clear_declaration) AND the declared Counter skill's OWN
    slot speed beats the attacker's slot speed. Returns the first
    eligible (slot, skill) found in declared_actions order, or None.

    Doesn't mark it used -- the caller (apply_counter_redirects) only
    commits that once it actually claims this specific incoming attack,
    since which attack claims a defender's single Counter charge
    depends on speed-priority resolution order across the whole round,
    not just this one comparison.
    """
    if defender.counter_used_this_round:
        return None
    for slot_num, action in defender.declared_actions.items():
        if "counter" in action.skill.tags and defender.slot_speed(slot_num) > attacker_slot_speed:
            return slot_num, action.skill
    return None


def find_eligible_clashable_counter(defender: Fighter) -> tuple[int, Skill] | None:
    """Same idea as find_eligible_counter, for [Clashable Counter]. Not
    speed-gated the way Counter is -- just needs to be declared this
    round and not yet used (clashable_counter_used_this_round).
    """
    if defender.clashable_counter_used_this_round:
        return None
    for slot_num, action in defender.declared_actions.items():
        if "clashable_counter" in action.skill.tags:
            return slot_num, action.skill
    return None


def apply_counter_redirects(units: list, battle: Battle) -> list:
    """Runs once, right after `units` is built and speed-sorted, BEFORE
    any resolution happens -- transforms the list to reflect Counter and
    Clashable Counter interceptions, so the main resolution loop further
    down never needs to know either mechanic exists; it just sees
    ordinary ('solo', entry) / ('clash', entry_a, entry_b) tuples,
    exactly the shape it already handles.

    Only ever touches 'solo' units -- a mutual Clash already means both
    sides are actively fighting back, so neither reactive mechanic
    applies there. Processes in the SAME speed order the main loop will
    use, since both mechanics are single-use per round and which
    incoming attack claims that single use depends on order.

    Pass 1 -- Counter: for every solo unit (attacker -> defender,
    would otherwise be unopposed), check find_eligible_counter against
    the defender. If eligible, this unit is entirely replaced: instead
    of the attacker's skill hitting the defender, the defender's OWN
    Counter skill now attacks the attacker back (skip_evasion=True on
    the eventual apply_incoming_hit call, since Counter explicitly
    bypasses the target's defenses) -- represented here as a normal
    'solo' unit with caster/target swapped and 'is_counter_retaliation'
    tagged on the entry so the main loop knows to skip evasion and use
    different flavor text. The attacker's original action never
    resolves at all: it was redirected, not merely blocked.

    Pass 2 -- Clashable Counter: for a fighter with one declared, if
    THEIR OWN action is (still, after pass 1) a solo unit -- i.e. its
    own declared target never clashed back -- it does NOT just resolve
    as a normal unopposed hit. Instead, scan every OTHER solo unit for
    one where someone else is unopposedly attacking any of this
    fighter's OTHER slots. If found, both units are consumed and
    replaced with a single real 'clash' unit between the Clashable
    Counter skill and that intercepted attacker's skill. If nothing to
    intercept, this fighter's own action simply doesn't resolve at all
    this round (it "does not activate" -- no consumption, no effect).
    """
    # Pass 1: Counter.
    pass1: list = []
    for u in units:
        if u[0] != "solo":
            pass1.append(u)
            continue
        entry = u[1]
        attacker, defender = entry["caster"], entry["target"]
        attacker_slot_speed = attacker.slot_speed(entry["slot"])
        found = find_eligible_counter(defender, attacker_slot_speed)
        if found is None:
            pass1.append(u)
            continue
        counter_slot, counter_skill = found
        defender.counter_used_this_round = True
        redirected_entry = {
            "caster": defender, "skill": counter_skill, "target": attacker,
            "slot": counter_slot, "target_slot": entry["slot"], "used": True,
            "is_counter_retaliation": True,
        }
        pass1.append(("solo", redirected_entry))

    # Pass 2: Clashable Counter.
    final_units: list = []
    consumed: set[int] = set()
    for idx, u in enumerate(pass1):
        if idx in consumed:
            continue
        if u[0] != "solo":
            final_units.append(u)
            continue
        entry = u[1]
        caster = entry["caster"]
        if "clashable_counter" not in entry["skill"].tags:
            final_units.append(u)
            continue
        found = find_eligible_clashable_counter(caster)
        if found is None:
            final_units.append(u)
            continue

        target_idx = None
        for j, other in enumerate(pass1):
            if j == idx or j in consumed:
                continue
            if other[0] != "solo":
                continue
            if other[1]["target"] is caster:
                target_idx = j
                break

        if target_idx is None:
            # "Does not activate" -- fizzles entirely, no consumption.
            consumed.add(idx)
            continue

        caster.clashable_counter_used_this_round = True
        consumed.add(idx)
        consumed.add(target_idx)
        intercepted_entry = pass1[target_idx][1]
        intercepted_entry["is_clashable_counter_intercept"] = True
        entry["is_clashable_counter_intercept"] = True
        final_units.append(("clash", entry, intercepted_entry))

    return final_units


# Every pre-roll (pre-toss) skill-level timing, in firing order, for a
# side that's about to enter a Clash. combat_start/turn_start used to
# live in this list too, but only ever fired for whatever skill was
# actually declared that round -- see fire_passive_triggers below,
# which now covers ALL of a fighter's known skills instead and is
# called once per fighter per round, before this per-entry chain ever
# runs. Keeping them here as well would double-fire them for any skill
# that happens to be both declared AND carries one of those tags.
#
# [Before Attack] is deliberately NOT in this list. It used to be
# bundled in here alongside [On Use], which meant it fired for BOTH
# sides before attrition even started -- wrong, since the loser never
# actually attacks. It's now evaluated inside resolve_round_clash
# itself, for the winner only, immediately before their final decisive
# toss (see that function's docstring in game/skills.py). [Clash Start]
# stays here though: it's genuinely a "the clash begins" moment for
# both sides, before ANY toss (attrition or final), which is exactly
# what this pre-roll window represents.
PRE_ROLL_CLASH_TIMINGS = ("before_use", "on_use", "clash_start")

# Same idea for a side making an unopposed attack. [Before Attack]
# stays here for the solo path -- there's no attrition to distinguish
# it from, the single toss IS the attack, so firing it at the same
# pre-roll moment as [On Use] is already correct.
PRE_ROLL_SOLO_TIMINGS = ("before_use", "on_use", "before_attack")


def fire_passive_triggers(battle: Battle) -> list[str]:
    """The persistent Fighter-level buff store this engine was missing:
    fires [Combat Start] (once per battle) and [Turn Start] (every
    round) against EVERY skill a living fighter knows, not just
    whichever one they happened to declare this round. This is what
    lets a passive like "[Combat Start] Gain 3 Charge" sitting on a
    skill that never gets used still actually do something.

    There's no per-fighter "turn" separate from the shared round
    structure in this engine, so [Turn Start] is mapped onto "start of
    this round's Combat Phase" -- a documented simplification, not a
    real per-turn system.

    Each fighter's own skills are evaluated with target=None (there's
    no specific enemy at this moment), so any trigger whose condition
    actually depends on a target (target_status, speed_faster, ...)
    correctly never fires here -- see evaluate_condition's handling of
    a missing target in game/conditions.py. Only effect types that make
    sense with no live coin toss in progress actually do anything:
    sanity_gain and gain_status. inflict_status is skipped by
    apply_trigger_effects (no target to inflict onto); bonus_power/
    bonus_coin_power are evaluated but have nothing to apply to
    (there's no skill resolution in progress right now), so writing one
    against these timings is simply a no-op.

    Returns the combined log lines from every fighter, in fighter order.
    """
    timings = ["turn_start"]
    if not battle.started:
        timings.append("combat_start")

    log: list[str] = []
    for fighter in battle.fighters:
        if not fighter.is_alive():
            continue
        for skill in fighter.skills.values():
            context = TriggerContext(caster=fighter, target=None, battle=battle)
            for timing in timings:
                _, post_hit = resolve_triggers(skill, context, timing)
                log.extend(apply_trigger_effects(post_hit, fighter, None))
    return log


def _resolve_pre_roll_chain(
    skill: Skill, context: TriggerContext, timings: tuple[str, ...]
) -> tuple[Skill, list[Trigger]]:
    """Chains resolve_triggers across every pre-roll timing in `timings`,
    in order, folding each stage's bonus_power/bonus_coin_power into the
    skill before the next stage evaluates against it (so e.g. a [Clash
    Start] Power buff is visible to [On Use]'s own condition check), and
    merging every stage's post-hit triggers into one list for the caller
    to apply once the hit actually lands.

    combat_start/turn_start are NOT in `timings` anymore -- they're
    fired once per fighter per round, across ALL of that fighter's
    known skills, by fire_passive_triggers above, before this function
    ever runs. This function only ever sees the skill actually declared
    this round, so it no longer needs a `battle` param to check
    battle.started against.
    """
    post_hit: list[Trigger] = []
    for timing in timings:
        skill, fired = resolve_triggers(skill, context, timing)
        post_hit.extend(fired)
    return skill, post_hit


def apply_trigger_effects(triggers: list[Trigger], caster: Fighter, target: Fighter | None) -> list[str]:
    """Applies the post-hit effects (inflict_status, sanity_gain,
    gain_status) from whichever triggers fired AND actually landed a
    hit. Pre-roll effects (bonus_power/bonus_coin_power) are already
    baked into the skill by resolve_triggers before this ever runs, so
    there's nothing to do for those here.

    target is optional: passive timings that don't reference an enemy
    (Combat Start, Turn Start) call this with target=None, since there's
    nobody to inflict a status onto at that moment -- an inflict_status
    trigger written against one of those timings is simply skipped
    rather than crashing (writing one there is a modeling mistake on
    the skill author's part, not something the engine can resolve).

    gain_status was previously parsed by parse_trigger_text but never
    actually applied anywhere -- a self-buff trigger (e.g. "[Combat
    Start] Gain 3 Charge") would silently do nothing. Fixed here: it
    lands on the CASTER's own statuses dict, same layering as
    inflict_status but with no resistance applied (Poise/Charge are
    self-buffs, not something an opponent resists -- see the note on
    INFLICTABLE_STATUSES in game/statuses.py).
    """
    log: list[str] = []
    for t in triggers:
        if t.effect_type == "inflict_status":
            if target is None:
                continue
            resistance_pct = target.resistances.get(t.status_name, 0)
            added_potency = apply_resistance(t.effect_value, resistance_pct)
            current = target.get_status(t.status_name)
            new_instance = apply_status(current, t.status_name, added_potency, t.status_count)
            target.set_status_instance(new_instance)
            log.append(
                f"Trigger: {target.name} gains {status_emoji(t.status_name)} "
                f"{t.status_name.capitalize()}: {new_instance.potency}/{new_instance.count}"
            )
        elif t.effect_type == "sanity_gain":
            caster.gain_sanity(t.effect_value)
            log.append(f"Trigger: {caster.name} Sanity +{t.effect_value} ({caster.sanity})")
        elif t.effect_type == "gain_status":
            current = caster.get_status(t.status_name)
            new_instance = apply_status(current, t.status_name, t.effect_value, t.status_count)
            caster.set_status_instance(new_instance)
            log.append(
                f"Trigger: {caster.name} gains {status_emoji(t.status_name)} "
                f"{t.status_name.capitalize()}: {new_instance.potency}/{new_instance.count}"
            )
    return log


class CombatLogView(discord.ui.View):
    """One button, "Full Log" -- anyone can click it to see the FULL
    breakdown of everything that happened this Combat Phase (every
    attrition round, every coin's face, every Trigger that fired,
    across every action), as an ephemeral reply to whoever clicked.
    Replaces the old per-action CombatRevealView (one button per
    action) with a single consolidated log, per the design decision to
    have one place to review the whole phase rather than action by
    action.

    Discord caps a single embed description at 4096 chars and a single
    message at 10 embeds -- with enough actions in one round the full
    log can genuinely blow past even that, so entries are packed into
    as many embeds as fit (up to 10) and anything beyond that is noted
    rather than silently dropped.
    """

    def __init__(self, entries: list[tuple[str, str]], timeout: float = 600):
        super().__init__(timeout=timeout)
        self.entries = entries

    @discord.ui.button(label="Full Log", style=discord.ButtonStyle.secondary)
    async def full_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.entries:
            await interaction.response.send_message("Nothing happened this Combat Phase.", ephemeral=True)
            return

        embeds: list[discord.Embed] = []
        current_lines: list[str] = []
        current_len = 0

        def flush():
            if current_lines:
                title = "Combat Log" if not embeds else f"Combat Log (cont. {len(embeds) + 1})"
                embeds.append(discord.Embed(title=title, description="\n".join(current_lines)[:4000], color=0x5865F2))

        for label, detail in self.entries:
            block = f"**{label}**\n{detail}"
            if current_lines and current_len + len(block) > 3800:
                flush()
                current_lines = []
                current_len = 0
            current_lines.append(block)
            current_len += len(block)
        flush()

        if len(embeds) > 10:
            embeds = embeds[:10]
            embeds[-1].set_footer(text="Some actions this round were too numerous to include in full.")

        await interaction.response.send_message(embeds=embeds, ephemeral=True)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


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
        character_name="Pull stats, avatar, and resistances from a saved character",
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

        fighter = Fighter(name=name, side=side, hp=hp, max_hp=hp, owner_id=interaction.user.id)
        if speed_min is not None:
            fighter.speed_min = speed_min
            fighter.speed_max = speed_max if speed_max is not None else speed_min
            fighter.roll_slot_speeds()
        battle.add_fighter(fighter)
        await interaction.response.send_message(f"Added {name} to Side {side} ({hp} HP).", ephemeral=True)
        await sync_battle_message(self.bot, battle)

    @app_commands.command(
        name="setstatus",
        description="Admin/testing tool: directly set a fighter's stats, resistances, and/or a status",
    )
    @app_commands.describe(
        fighter="Fighter name",
        status_name="A status to set, or 'none' to clear all statuses",
        potency="Potency for status_name",
        count="Count for status_name",
        hp="Set current HP",
        max_hp="Set max HP",
        sanity="Set Sanity",
        speed_min="Set the low end of this fighter's Speed range (needs speed_max too, re-rolls slots)",
        speed_max="Set the high end of this fighter's Speed range (needs speed_min too, re-rolls slots)",
        power="Set Power",
        resistance_input="Resistance(s) to set: 'Type:Value', comma-separated for several, e.g. 'slash:20,burn:-10'",
    )
    @app_commands.choices(
        status_name=[app_commands.Choice(name=s.capitalize(), value=s) for s in INFLICTABLE_STATUSES]
        + [app_commands.Choice(name="None (clear all statuses)", value="none")]
    )
    async def setstatus(
        self,
        interaction: discord.Interaction,
        fighter: str,
        status_name: app_commands.Choice[str] | None = None,
        potency: int = 0,
        count: int = 0,
        hp: int | None = None,
        max_hp: int | None = None,
        sanity: int | None = None,
        speed_min: int | None = None,
        speed_max: int | None = None,
        power: int | None = None,
        resistance_input: str | None = None,
    ):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message("No active battle here.", ephemeral=True)
            return

        target_fighter = battle.get_fighter(fighter)
        if target_fighter is None:
            await interaction.response.send_message(f"No fighter named {fighter}.", ephemeral=True)
            return

        if (speed_min is None) != (speed_max is None):
            await interaction.response.send_message(
                "speed_min and speed_max must be set together, or not at all.", ephemeral=True
            )
            return

        # Parsed and validated up front, before anything gets mutated --
        # same "all-or-nothing" principle as the rest of this command:
        # a bad resistance entry shouldn't leave hp/sanity/etc already
        # applied while resistances silently fail.
        resistance_changes: list[tuple[str, int]] = []
        if resistance_input is not None:
            for token in resistance_input.split(","):
                token = token.strip()
                if not token:
                    continue
                parts = token.split(":")
                if len(parts) != 2:
                    await interaction.response.send_message(
                        f"Resistance entry '{token}' is invalid. Use 'Type:Value', comma-separated "
                        f"for several, e.g. 'slash:20,burn:-10'.",
                        ephemeral=True,
                    )
                    return
                r_type, r_value_raw = parts[0].strip().lower(), parts[1].strip()
                if r_type not in ALL_RESISTANCE_TYPES:
                    await interaction.response.send_message(
                        f"'{r_type}' isn't a valid resistance type. Choose from: "
                        f"{', '.join(ALL_RESISTANCE_TYPES)}.",
                        ephemeral=True,
                    )
                    return
                try:
                    r_value = int(r_value_raw)
                except ValueError:
                    await interaction.response.send_message(
                        f"Resistance value '{r_value_raw}' for {r_type} isn't a whole number.",
                        ephemeral=True,
                    )
                    return
                resistance_changes.append((r_type, r_value))

        changes = []

        if status_name is not None:
            if status_name.value == "none":
                target_fighter.statuses.clear()
                changes.append("all statuses cleared")
            else:
                target_fighter.set_status_instance(
                    StatusInstance(name=status_name.value, potency=potency, count=count)
                )
                changes.append(
                    f"{status_emoji(status_name.value)} {status_name.name} -> {potency}/{count}"
                )

        if hp is not None:
            target_fighter.hp = hp
            changes.append(f"HP -> {hp}")

        if max_hp is not None:
            target_fighter.max_hp = max_hp
            changes.append(f"Max HP -> {max_hp}")

        if sanity is not None:
            target_fighter.sanity = sanity
            changes.append(f"Sanity -> {sanity}")

        if speed_min is not None:
            target_fighter.speed_min = speed_min
            target_fighter.speed_max = speed_max
            target_fighter.roll_slot_speeds()
            changes.append(f"Speed range -> {speed_min}-{speed_max} (slots re-rolled)")

        if power is not None:
            target_fighter.power = power
            changes.append(f"Power -> {power}")

        for r_type, r_value in resistance_changes:
            target_fighter.resistances[r_type] = r_value
            changes.append(f"{r_type.capitalize()} resistance -> {r_value}%")

        if not changes:
            await interaction.response.send_message(
                "Nothing to change, no fields were provided.", ephemeral=True
            )
            return

        await interaction.response.send_message(f"Updated {target_fighter.name}: {', '.join(changes)}.")
        await sync_battle_message(self.bot, battle)

    @app_commands.command(name="addskill", description="Give a fighter a skill (opens a popup)")
    @app_commands.describe(fighter="Which fighter learns this skill")
    async def addskill(
        self,
        interaction: discord.Interaction,
        fighter: str,
    ):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message("No active battle here.", ephemeral=True)
            return

        target_fighter = battle.get_fighter(fighter)
        if target_fighter is None:
            await interaction.response.send_message(f"No fighter named {fighter}.", ephemeral=True)
            return

        # The ONLY response for this interaction -- everything about the
        # skill (name, stats, damage type, statuses, triggers) is now
        # collected in one popup instead of 6 required slash-command
        # options plus a second modal. See AddSkillModal above.
        await interaction.response.send_modal(AddSkillModal(target_fighter))

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

        # Indiscriminate skills hit a random enemy slot, not one the
        # caster chooses -- whatever target_slot was typed in gets
        # thrown out and replaced here, before it's ever validated.
        indiscriminate_note = ""
        if "indiscriminate" in skill.tags:
            target_slot = random.randint(1, target_fighter.skill_slots)
            indiscriminate_note = (
                f" ({skill.name} is Indiscriminate -- randomly hits Slot {target_slot})"
            )

        if not (1 <= target_slot <= target_fighter.skill_slots):
            await interaction.response.send_message(
                f"{target_fighter.name} only has skill slots 1-{target_fighter.skill_slots}.",
                ephemeral=True,
            )
            return

        caster_speed = caster.slot_speed(slot)
        target_speed = target_fighter.slot_speed(target_slot)

        partner_info = battle.find_mutual_clash_partner(target_fighter, target_slot)
        if partner_info is not None:
            partner, partner_slot = partner_info
            partner_action = partner.declared_actions.get(partner_slot)
            partner_is_target_fixed = (
                partner_action is not None and "target_fixed" in partner_action.skill.tags
            )
            if (
                partner is not caster
                and partner.side == caster.side
                and caster_speed > partner.slot_speed(partner_slot)
                and caster_speed > target_speed
            ):
                if partner_is_target_fixed:
                    caster.declare_in_slot(slot, skill, target_fighter, target_slot)
                    await interaction.response.send_message(
                        f"{target_fighter.name}'s Slot {target_slot} is already clashing "
                        f"{partner.name}'s **{partner_action.skill.name}**, which is Target Fixed "
                        f"and can't be taken over. Falling through to unopposed against "
                        f"{target_fighter.name} instead.",
                        ephemeral=True,
                    )
                    await sync_battle_message(self.bot, battle)
                    return

                if partner.owner_id is None:
                    caster.declare_in_slot(slot, skill, target_fighter, target_slot)
                    await interaction.response.send_message(
                        f"{target_fighter.name}'s Slot {target_slot} is already clashing "
                        f"{partner.name}, and taking it over needs {partner.name}'s owner to "
                        f"approve, but {partner.name} has no linked owner. Falling through to "
                        f"unopposed against {target_fighter.name} instead.",
                        ephemeral=True,
                    )
                    await sync_battle_message(self.bot, battle)
                    return

                owner = self.bot.get_user(partner.owner_id)
                if owner is None:
                    try:
                        owner = await self.bot.fetch_user(partner.owner_id)
                    except discord.NotFound:
                        owner = None

                steal_view = StealApprovalView(
                    caster, skill, slot, partner, partner_slot, target_fighter, target_slot,
                    self.bot, battle,
                )

                sent = False
                if owner is not None:
                    try:
                        dm_message = await owner.send(
                            f"**{caster.name}** (Slot {slot}, {stat_emoji('speed')}{caster_speed}) wants to take "
                            f"over your **{partner.name}**'s clash against **{target_fighter.name}**'s "
                            f"Slot {target_slot}, using **{skill.name}**.\n\n"
                            f"Approve gives it to them (your Slot {partner_slot} is cleared). "
                            f"Decline keeps your clash as declared, and their action falls through "
                            f"to unopposed instead.",
                            view=steal_view,
                        )
                        steal_view.message = dm_message
                        sent = True
                    except discord.Forbidden:
                        sent = False

                if not sent:
                    caster.declare_in_slot(slot, skill, target_fighter, target_slot)
                    await interaction.response.send_message(
                        f"Couldn't DM {partner.name}'s owner to ask for approval (DMs closed or "
                        f"user not found), so this falls through to unopposed against "
                        f"{target_fighter.name} instead.",
                        ephemeral=True,
                    )
                    await sync_battle_message(self.bot, battle)
                    return

                await interaction.response.send_message(
                    f"{target_fighter.name}'s Slot {target_slot} is already clashing "
                    f"{partner.name}. {partner.name}'s owner has been DMed to ask whether "
                    f"{caster.name} can take it over instead. Your action locks in either way, "
                    f"you'll just find out later whether it's a clash or unopposed.",
                    ephemeral=True,
                )
                return

        # Deliberately no preview of the target's own skill here, ever --
        # you don't get to see what an enemy is bringing before you
        # commit, regardless of Speed. Whether this ends up a real Clash
        # is also genuinely unknown at declare time: it only becomes one
        # if the target's own action in target_slot targets this exact
        # (caster, slot) back (see combat()'s mutual-match logic). If it
        # doesn't, this resolves as an unopposed attack instead -- and
        # you won't know which until /battle combat actually runs.
        view = ClashDeclareView(caster, skill, target_fighter, slot, target_slot, self.bot, battle)
        speed_icon = stat_emoji("speed")
        preview = (
            f"**{caster.name}**'s Slot {slot} ({speed_icon}{caster_speed}) locks in **{skill.name}** "
            f"aimed at {target_fighter.name}'s Slot {target_slot}.{indiscriminate_note}\n\n"
            f"This only becomes a Clash if {target_fighter.name} targets your Slot {slot} back with "
            f"their Slot {target_slot}. Otherwise it resolves unopposed. You won't know which until "
            f"combat actually runs.\n\n"
            f"Confirm to lock it in?"
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

    @app_commands.command(name="removefighter", description="Remove a fighter from the current battle")
    @app_commands.describe(fighter="Fighter to remove")
    async def removefighter(self, interaction: discord.Interaction, fighter: str):
        battle = self.battles.get(interaction.channel_id)
        if battle is None:
            await interaction.response.send_message("No active battle here.", ephemeral=True)
            return

        target_fighter = battle.get_fighter(fighter)
        if target_fighter is None:
            await interaction.response.send_message(f"No fighter named {fighter}.", ephemeral=True)
            return

        if not _can_manage_fighter(interaction, target_fighter):
            await interaction.response.send_message(
                "Only this fighter's own owner or an admin can remove them.", ephemeral=True
            )
            return

        battle.fighters.remove(target_fighter)
        await interaction.response.send_message(f"Removed {target_fighter.name} from the battle.")
        await sync_battle_message(self.bot, battle)

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

        # This whole command can take a while now (animating every
        # attrition round coin-by-coin), so acknowledge the interaction
        # immediately and do everything else as followups -- the entire
        # animation lives inside ONE message that gets edited repeatedly
        # from here until the phase is done.
        await interaction.response.defer()

        # Fires [Combat Start] (first round of the battle only) and
        # [Turn Start] (every round) against every living fighter's
        # FULL skill list, not just whatever they declared -- see
        # fire_passive_triggers's docstring above. Deliberately happens
        # before any clash/unopposed resolution below, and before
        # battle.started flips to True at the end of this method.
        passive_log = fire_passive_triggers(battle)

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
            # An Unclashable skill on EITHER side forces this to resolve
            # unopposed, even if both sides' target/target_slot would
            # otherwise mutually match.
            if "unclashable" not in entry["skill"].tags:
                for other in entries[i + 1:]:
                    if other["used"]:
                        continue
                    if "unclashable" in other["skill"].tags:
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

        # Transforms `units` to reflect Counter / Clashable Counter
        # interceptions -- see apply_counter_redirects's docstring
        # above. Must run AFTER sorting (it depends on speed-priority
        # order) and BEFORE any resolution below, since it can replace
        # or merge units entirely.
        units = apply_counter_redirects(units, battle)

        # locked_lines holds everything PERMANENTLY decided so far this
        # Combat Phase: the passive-trigger block (if any), then one
        # one-line summary per unit as it finishes animating. It's
        # re-rendered on every single edit below alongside whatever
        # unit is currently mid-animation, so earlier results are never
        # lost while later ones are still resolving -- this is what
        # makes the whole phase feel like one continuous message rather
        # than N separate ones (the design choice made for this
        # rewrite: one message for the entire phase, not one per unit).
        locked_lines: list[str] = []
        if passive_log:
            locked_lines.append("**Passive Triggers**\n" + "\n".join(passive_log))

        combat_title = f"Combat Phase, Round {battle.round_number}"
        initial_embed = discord.Embed(
            title=combat_title,
            description="\n\n".join(locked_lines) or "Starting Combat Phase...",
            color=0x5865F2,
        )
        combat_message = await interaction.followup.send(embed=initial_embed, wait=True)

        async def render(live_block: str | None):
            """Re-renders the ONE shared combat_message: every locked
            (finished) line, plus whatever the current unit's live
            animation looks like right now. Called constantly during
            animation -- this is genuinely a lot of message edits for a
            busy round (every coin face, every round, every unit), which
            is an intentional trade-off for the full-fidelity animation
            the user asked for over a faster but less spectacle-driven
            reveal.
            """
            parts = []
            base = "\n\n".join(locked_lines)
            if base:
                parts.append(base)
            if live_block:
                parts.append(live_block)
            description = "\n\n".join(parts) if parts else "Nothing declared this round."
            if len(description) > 4000:
                description = description[-4000:]
            embed = discord.Embed(title=combat_title, description=description, color=0x5865F2)
            await combat_message.edit(embed=embed)

        # Coin-by-coin animation is pure PRESENTATION over results that
        # are already fully computed the instant resolve_skill/
        # resolve_round_clash/apply_incoming_hit run below -- same
        # principle the old "rolling" flavor message used, just carried
        # all the way through instead of stopping after one flourish.
        # Nothing here recomputes damage, crits, evasion, resistance,
        # or triggers; it only controls the PACING of revealing numbers
        # that already exist.
        COIN_FACE_DELAY = 0.55
        COIN_DETAIL_DELAY = 0.4

        async def animate_faces(live_header: str, coin_results: list) -> str:
            """Phase one: reveals each coin's face one at a time, rolling
            icon first, mirroring a real Limbus clash flipping its coins
            in sequence. Returns the finished face row.
            """
            n = len(coin_results)
            revealed: list[str] = []
            for c in coin_results:
                pending = revealed + [coin_roll_emoji()] * (n - len(revealed))
                await render(live_header + "\n  " + " ".join(pending))
                await asyncio.sleep(COIN_FACE_DELAY)
                revealed.append(coin_emoji("heads") if c.heads else coin_emoji("tails"))
            face_line = "  " + " ".join(revealed)
            await render(live_header + "\n" + face_line)
            await asyncio.sleep(0.35)
            return face_line

        async def animate_power(live_header: str, face_line: str, coin_results: list) -> str:
            """Phase two for an ATTRITION ROUND toss: once every coin's
            face is showing, reveal each coin's running Power one at a
            time (this is a round toss, nobody actually takes damage
            yet -- only Power is being compared to decide who loses a
            coin this round).
            """
            lines = []
            for i, c in enumerate(coin_results, start=1):
                lines.append(f"  Coin {i}: Power {c.power_after}")
                await render(live_header + "\n" + face_line + "\n" + "\n".join(lines))
                await asyncio.sleep(COIN_DETAIL_DELAY)
            return "\n".join(lines)

        async def animate_damage(live_header: str, face_line: str, coin_results: list, hit_log: list[str]) -> str:
            """Phase two for the FINAL DECISIVE toss -- "once all the
            coins break, it goes through the animation again for the
            damage output one by one per coin". hit_log is the flat log
            apply_incoming_hit already produced for this exact hit
            (every line prefixed "Coin N:", covering resistance,
            Crit/Rupture/status/Counter notes, and dodges) -- this
            never recomputes anything, it just reveals those
            already-applied lines grouped by coin, one coin at a time.
            """
            lines = []
            for i in range(1, len(coin_results) + 1):
                coin_lines = [l for l in hit_log if l.startswith(f"Coin {i}:")]
                lines.extend(coin_lines if coin_lines else [f"Coin {i}: no additional effect"])
                await render(live_header + "\n" + face_line + "\n" + "\n".join(lines))
                await asyncio.sleep(COIN_DETAIL_DELAY)
            return "\n".join(lines)

        summary_lines: list[str] = []
        full_log_entries: list[tuple[str, str]] = []
        # Tracks whether we've resolved anything yet this Combat Phase, for
        # the first_hit_of_round Trigger condition. One clash counts as a
        # single event (both sides share the same flag value).
        first_action_done = False

        for u in units:
            if u[0] == "clash":
                _, entry_a, entry_b = u
                fighter_a, fighter_b = entry_a["caster"], entry_b["caster"]
                if not fighter_a.is_alive() or not fighter_b.is_alive():
                    continue

                live_header = f"⚔️ **{fighter_a.name}** vs **{fighter_b.name}**"
                await render(live_header)
                await asyncio.sleep(0.3)

                context_a = TriggerContext(
                    caster=fighter_a, target=fighter_b, battle=battle,
                    is_first_hit_of_round=not first_action_done,
                )
                context_b = TriggerContext(
                    caster=fighter_b, target=fighter_a, battle=battle,
                    is_first_hit_of_round=not first_action_done,
                )
                # Full pre-roll chain for both sides -- see
                # PRE_ROLL_CLASH_TIMINGS / _resolve_pre_roll_chain above
                # for firing order and the documented [Clash Start] /
                # [Before Attack] scope collapse.
                skill_a, pre_roll_post_hit_a = _resolve_pre_roll_chain(
                    entry_a["skill"], context_a, PRE_ROLL_CLASH_TIMINGS
                )
                skill_b, pre_roll_post_hit_b = _resolve_pre_roll_chain(
                    entry_b["skill"], context_b, PRE_ROLL_CLASH_TIMINGS
                )
                first_action_done = True

                # Fully resolved instantly, same as always -- everything
                # below this point is presentation over already-decided
                # numbers, see the animate_* helpers' docstrings above.
                outcome = resolve_round_clash(
                    skill_a, skill_b,
                    heads_chance_a=fighter_a.heads_chance(),
                    heads_chance_b=fighter_b.heads_chance(),
                    context_a=context_a,
                    context_b=context_b,
                )
                winner = fighter_a if outcome.winner == "a" else fighter_b
                loser = fighter_b if winner is fighter_a else fighter_a
                winner_skill = skill_a if outcome.winner == "a" else skill_b
                loser_skill = skill_b if outcome.winner == "a" else skill_a
                winner_context = context_a if outcome.winner == "a" else context_b
                loser_context = context_b if outcome.winner == "a" else context_a
                winner_pre_roll_post_hit = pre_roll_post_hit_a if outcome.winner == "a" else pre_roll_post_hit_b

                winner.gain_sanity(SANITY_CLASH_WIN)
                loser.lose_sanity(SANITY_CLASH_LOSS)

                # [Clash Win] and [Attack End] only fire for the winner --
                # the loser never actually lands a hit, so neither an
                # on-hit-family Trigger nor an "attack end" one makes
                # sense for them (resolve_triggers is only ever called
                # with the loser's own skill+context at "clash_lose").
                # [Turn End] is different: it fires for BOTH sides, since
                # it's about that fighter's own turn ending, not about
                # whether they landed a hit.
                _, clash_win_post_hit = resolve_triggers(winner_skill, winner_context, "clash_win")
                _, attack_end_post_hit = resolve_triggers(winner_skill, winner_context, "attack_end")
                _, turn_end_post_hit_winner = resolve_triggers(winner_skill, winner_context, "turn_end")
                _, clash_lose_post_hit = resolve_triggers(loser_skill, loser_context, "clash_lose")
                _, turn_end_post_hit_loser = resolve_triggers(loser_skill, loser_context, "turn_end")

                total_damage, status_log, per_coin_triggers, evade_count = apply_incoming_hit(
                    winner_skill, outcome.winner_final_result, loser, winner
                )
                loser.take_damage(total_damage)
                trigger_log = apply_trigger_effects(
                    winner_pre_roll_post_hit + clash_win_post_hit + attack_end_post_hit
                    + turn_end_post_hit_winner + per_coin_triggers
                    + outcome.winner_before_attack_post_hit,
                    winner, loser,
                )
                trigger_log += apply_trigger_effects(clash_lose_post_hit + turn_end_post_hit_loser, loser, winner)
                # [On Evade] is the LOSER's own reaction -- they're the
                # one who just got hit by the winner's final toss, see
                # fire_evade_triggers for why this needs its own call
                # rather than folding into apply_trigger_effects.
                trigger_log += fire_evade_triggers(loser, winner, battle, evade_count)

                # ---- Animate every attrition round in full ----
                round_summaries: list[str] = []
                for round_idx, r in enumerate(outcome.rounds, start=1):
                    round_header = (
                        live_header + "\n" + "\n".join(round_summaries)
                        + ("\n" if round_summaries else "")
                        + f"Round {round_idx}: {fighter_a.name} ({r.coins_a_before} coins) "
                        + f"vs {fighter_b.name} ({r.coins_b_before} coins)"
                    )

                    a_face = await animate_faces(f"{round_header}\n{fighter_a.name}:", r.result_a.coin_results)
                    a_power = await animate_power(f"{round_header}\n{fighter_a.name}:", a_face, r.result_a.coin_results)
                    a_block = f"{fighter_a.name}:\n{a_face}\n{a_power}"

                    b_face = await animate_faces(f"{round_header}\n{a_block}\n{fighter_b.name}:", r.result_b.coin_results)
                    b_power = await animate_power(f"{round_header}\n{a_block}\n{fighter_b.name}:", b_face, r.result_b.coin_results)

                    if r.loser == "a":
                        result_line = f"{fighter_a.name} loses a coin"
                    elif r.loser == "b":
                        result_line = f"{fighter_b.name} loses a coin"
                    else:
                        result_line = "Tie, nobody loses a coin"

                    full_round_block = f"{round_header}\n{a_block}\n{fighter_b.name}:\n{b_face}\n{b_power}\n{result_line}"
                    await render(full_round_block)
                    await asyncio.sleep(0.6)

                    round_summaries.append(
                        f"Round {round_idx}: {fighter_a.name} Power {r.result_a.final_power} vs "
                        f"{fighter_b.name} Power {r.result_b.final_power} -> {result_line}"
                    )

                # ---- Animate the winner's final decisive toss ----
                final_header = (
                    live_header + "\n" + "\n".join(round_summaries)
                    + f"\n\n**{winner.name}'s final attack** "
                    + f"({outcome.winner_final_result.skill.coins} coins remaining):"
                )
                final_face = await animate_faces(final_header, outcome.winner_final_result.coin_results)
                await animate_damage(final_header, final_face, outcome.winner_final_result.coin_results, status_log)
                await asyncio.sleep(0.4)

                # ---- Same full detail text + one-line summary as before, for the Full Log button and locked history ----
                field_value = format_clash_rounds(outcome, fighter_a.name, fighter_b.name)
                if entry_a.get("is_clashable_counter_intercept") or entry_b.get("is_clashable_counter_intercept"):
                    holder = entry_a["caster"] if entry_a.get("is_clashable_counter_intercept") else entry_b["caster"]
                    field_value = (
                        f"⚡ {holder.name}'s Clashable Counter intercepts an unopposed attack!\n\n"
                        + field_value
                    )
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
                if trigger_log:
                    field_value += "\n" + "\n".join(trigger_log)

                summary_line = (
                    f"⚔️ **{winner.name}** beats **{loser.name}** -- {total_damage} damage "
                    f"({loser.name}: {loser.hp}/{loser.max_hp} HP)"
                )
                summary_lines.append(summary_line)
                full_log_entries.append((f"{fighter_a.name} vs {fighter_b.name}", field_value))

                # Lock this unit's summary permanently into the message,
                # clear the live block, and move on to the next unit.
                locked_lines.append(summary_line)
                await render(None)

            else:
                _, entry = u
                fighter = entry["caster"]
                target = entry["target"]
                if not fighter.is_alive() or not target.is_alive():
                    continue

                is_counter = entry.get("is_counter_retaliation", False)
                if is_counter:
                    live_header = f"🔁 **{fighter.name}**'s Counter redirects -> strikes **{target.name}** back!"
                else:
                    live_header = f"🗡️ **{fighter.name}** -> **{target.name}** (unopposed)"
                await render(live_header)
                await asyncio.sleep(0.3)

                context = TriggerContext(
                    caster=fighter, target=target, battle=battle,
                    is_first_hit_of_round=not first_action_done,
                )

                # Full pre-roll chain -- see PRE_ROLL_SOLO_TIMINGS /
                # _resolve_pre_roll_chain above. A Counter retaliation
                # still goes through this same chain, since it's the
                # defender's own skill resolving normally -- the only
                # thing special about it is skip_evasion below.
                adjusted_skill, pre_roll_post_hit = _resolve_pre_roll_chain(
                    entry["skill"], context, PRE_ROLL_SOLO_TIMINGS
                )
                first_action_done = True

                result = resolve_skill(adjusted_skill, fighter.heads_chance(), context)
                heads_landed = sum(1 for c in result.coin_results if c.heads)
                sanity_gain = heads_landed * SANITY_PER_HEADS_UNOPPOSED
                fighter.gain_sanity(sanity_gain)

                total_damage, status_log, per_coin_triggers, evade_count = apply_incoming_hit(
                    adjusted_skill, result, target, fighter, skip_evasion=is_counter
                )
                target.take_damage(total_damage)
                if is_counter:
                    _, before_getting_hit_post = resolve_triggers(adjusted_skill, context, "before_getting_hit")
                    trigger_log = apply_trigger_effects(before_getting_hit_post, fighter, target)
                else:
                    _, unopposed_post_hit = resolve_triggers(adjusted_skill, context, "on_unopposed_attack")
                    _, attack_end_post_hit = resolve_triggers(adjusted_skill, context, "attack_end")
                    _, turn_end_post_hit = resolve_triggers(adjusted_skill, context, "turn_end")
                    trigger_log = apply_trigger_effects(
                        pre_roll_post_hit + unopposed_post_hit + attack_end_post_hit
                        + turn_end_post_hit + per_coin_triggers,
                        fighter, target,
                    )
                    # [On Evade] is the TARGET's own reaction to this
                    # unopposed attack -- doesn't apply to a Counter
                    # retaliation, which explicitly bypasses it
                    # (skip_evasion=True already means evade_count is 0).
                    trigger_log += fire_evade_triggers(target, fighter, battle, evade_count)

                # ---- Animate: no attrition rounds for an unopposed attack, straight to the decisive toss ----
                final_header = live_header
                final_face = await animate_faces(final_header, result.coin_results)
                await animate_damage(final_header, final_face, result.coin_results, status_log)
                await asyncio.sleep(0.4)

                field_value = format_skill_result(result)
                field_value += (
                    f"\n\n{target.name} takes {total_damage} damage. "
                    f"({target.name}: {target.hp}/{target.max_hp} HP)"
                )
                field_value += f"\n{fighter.name} Sanity +{sanity_gain} ({fighter.sanity})"
                if status_log:
                    field_value += "\n" + "\n".join(status_log)
                if trigger_log:
                    field_value += "\n" + "\n".join(trigger_log)

                if is_counter:
                    summary_line = (
                        f"🔁 **{fighter.name}**'s Counter redirects and strikes **{target.name}** back "
                        f"-- {total_damage} damage ({target.name}: {target.hp}/{target.max_hp} HP)"
                    )
                    full_log_entries.append((f"{fighter.name}'s Counter -> {target.name}", field_value))
                else:
                    summary_line = (
                        f"🗡️ **{fighter.name}** hits **{target.name}** (unopposed) -- {total_damage} damage "
                        f"({target.name}: {target.hp}/{target.max_hp} HP)"
                    )
                    full_log_entries.append((f"{fighter.name} -> {target.name} (unopposed)", field_value))
                summary_lines.append(summary_line)

                locked_lines.append(summary_line)
                await render(None)

        # Everything's already locked into locked_lines as the phase
        # went along -- this is just the final static render, no
        # "live" block left since the last unit already locked itself in.
        footer_note = None
        final_description = "\n\n".join(locked_lines)
        if len(final_description) > 4000:
            final_description = final_description[:4000] + "\n...(truncated)"
            footer_note = "Some results this round were too numerous to display fully."
        if len(full_log_entries) > 60:
            footer_note = (
                (footer_note + " " if footer_note else "")
                + "Some actions this round were too numerous to include in the Full Log."
            )

        final_embed = discord.Embed(
            title=f"{combat_title} -- Results",
            description=final_description or "Nothing declared this round.",
            color=0x5865F2,
        )
        if footer_note:
            final_embed.set_footer(text=footer_note)

        # Whatever [Combat Start] triggers were going to fire this battle
        # already fired above (or didn't, if nobody had one declared) --
        # this flips permanently so they never fire again in later rounds
        # of the same battle.
        battle.started = True

        battle.start_new_round()

        # Single consolidated "Full Log" button replaces the old
        # per-action CombatRevealView -- see CombatLogView's docstring.
        log_view = CombatLogView(full_log_entries) if full_log_entries else None
        await combat_message.edit(embed=final_embed, view=log_view)

        # Once the whole phase is done, post the updated battle status
        # as a FRESH message in the channel (not just a silent edit of
        # whatever the old tracked message was, which may be scrolled
        # far above the animation that just happened) -- and start
        # tracking THIS new message going forward, so future declares/
        # addfighter/etc. edit the freshest copy instead of an old one
        # buried above a wall of combat animation.
        status_message = await interaction.channel.send(embed=build_battle_embed(battle))
        battle.message_id = status_message.id

    @app_commands.command(name="end", description="End the battle in this channel")
    async def end(self, interaction: discord.Interaction):
        if interaction.channel_id in self.battles:
            del self.battles[interaction.channel_id]
            await interaction.response.send_message("Battle ended.")
        else:
            await interaction.response.send_message("No active battle here.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BattleCog(bot))