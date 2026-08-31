# Limbattle Bot

A Discord bot for running Limbus Company–style coin-flip combat, built around a
custom ruleset. Owner-operated project, developed in a GitHub Codespace.

> **Notes to self, kept here so I don't have to re-figure things out from
> scratch every time I come back to this**,
> `git log --oneline -15` plus this file is usually enough to remember where
> things stand. When something structural changes. A new command, a new
> mechanic, a bug whose fix isn't obvious from the diff alone worth jotting
> it down here while it's fresh, since by next session I've usually forgotten
> the "why."

## Combat Features

- **Coin-based skill resolution**: sequential coin tosses, Heads permanently
  raise Power for every hit after it (including its own), Tails still hits at
  current Power.
- **Sanity-driven coin odds** (`heads_chance = 50 + sanity`), clash win/loss
  and unopposed-hit Sanity economy, sanity drift toward 0 each round.
- **Round-based Clash attrition**: both sides re-toss ALL remaining coins each
  round, lower Power loses one coin permanently, repeats until one side hits
  zero coins. The winner then makes one fresh final toss with whatever coins
  they have left — that's the only toss that actually deals damage.
- **No scouting**: `/battle declare` never reveals what the enemy is bringing,
  and whether an action becomes a real Clash is only decided at `/battle
  combat` time — it only happens if both sides' declared actions target each
  other's exact slot back. If not, it resolves unopposed instead.
- **Clash-steal**: a faster ally can take over an existing ally-vs-enemy clash
  slot, with DM approval from the ally being displaced.
- **Conditional Triggers**: a bracket-tag natural-language syntax (e.g.
  `[On Use] If this unit's Sanity is 45+, Coin Power +1`), parsed by
  `game/conditions.py`, entered via the `/battle addskill` popup. See
  **Trigger Syntax** below for the full reference — this is the part most
  worth re-reading before touching `game/conditions.py`, `game/skills.py`, or
  the trigger-dispatch chain in `cogs/battle.py`'s `combat()`.
- **Poise-break Crit**: holding Poise makes coins crit for bonus damage,
  consuming one Poise count per crit, until it runs out.
- **Evasion**: holding Evasion dodges incoming coins one at a time (consumed
  per dodge), firing `[On Evade]` on the defender's own known skills via
  `fire_evade_triggers`.
- **Counter**: holding Counter lets the defender hit back on an incoming
  coin; `[Before Getting Hit]` fires off the defender's own skill list via
  `fire_counter_triggers`, same passive-sweep pattern as Combat Start/Turn
  Start (`fire_passive_triggers`) but scoped per-incoming-hit instead of
  per-round.
- **Animated Combat Phase**: `/battle combat` plays out the whole round as one
  continuously-edited message — coin-by-coin face reveals for every attrition
  round of a Clash, then the winner's final decisive toss gets its own
  face-reveal pass followed by a real-damage reveal pass (pulled from the
  actual `apply_incoming_hit` log, so it reflects resistance/Crit/Rupture/
  status/Counter exactly, not an approximation). Each unit locks into a
  permanent one-line summary as it finishes so earlier results never
  disappear while later ones are still animating. A single **Full Log**
  button (`CombatLogView`) shows the whole phase's complete breakdown as
  ephemeral embeds to whoever clicks it, chunked if long. Tuning knobs:
  `COIN_FACE_DELAY` / `COIN_DETAIL_DELAY` near the top of `combat()` in
  `cogs/battle.py` — a busy round with several Clashes can take a couple of
  minutes real-time at the current pacing.
- **Proelium Fatale GIF**: any `fatal`-type battle shows the Proelium Fatale
  GIF on its embed (`BATTLE_TYPES["fatal"]["image"]` in `game/battle.py`),
  on creation and every subsequent embed sync.

## Command Reference

### `/battle` group (`cogs/battle.py`)

| Command | Notes |
|---|---|
| `create` | Starts a battle in the channel (Spar / Standard / Fatal). One battle per channel. |
| `addfighter` | Adds a fighter to a side, either from a saved `/character` or as a one-off. |
| `addskill` | Takes only `fighter` — opens `AddSkillModal`, a single popup collecting name, stats (packed comma-separated: Discord caps a modal at 5 fields), per-coin statuses, and the Trigger text box. |
| `declare` | Locks a skill into one of your slots aimed at a target's slot. No scouting — see Combat Features above. |
| `undeclare` | Clears one declared slot. |
| `removefighter` | Removes a fighter from the battle entirely. Gated to that fighter's own owner or the admin role (`ADMIN_ROLE_ID = 1468446442430533737`) via `_can_manage_fighter`. |
| `setstatus` | Admin/testing tool. Directly sets HP, Sanity, Speed (min+max together), resistances (comma-separated multi-set, same pattern as `/character resistance`), Power, and/or one status — whichever fields you actually pass. |
| `combat` | Resolves the round. See **Animated Combat Phase** above. |
| `end` | Ends the battle in the channel. |

`/battle status` was removed — the synced battle embed (`build_battle_embed`,
kept current via `sync_battle_message`) already shows everything it did.

### `/character` group (`cogs/character.py`)

| Command | Notes |
|---|---|
| `create` | New saved character, owned by whoever ran it. |
| `list` | Lists your own saved characters. |
| `view` | Shows a character's stats. Owner or admin only; `public:True` posts it to the channel. |
| `edit` | Owner-or-admin. No more flat `speed` param — `speed_min`/`speed_max` must be given together (no more implicit "just speed_min alone = flat" shortcut). Command description itself says "only fill in the fields you want to change" instead of tagging every single param `(optional)`. |
| `resistance` | Owner-or-admin. Set one **or several** resistances at once: `resistance_types` and `values` are both comma-separated and line up 1:1 positionally (e.g. `resistance_types:slash,burn values:20,-10`). |
| `delete` | Owner-or-admin. |
| `say` | Owner-or-admin. Speaks as the character via webhook (name + avatar). |

### `/roll`

Plain dice roller (`1d20+4+2` style), not tied to battle state at all.

## Trigger Syntax (`game/conditions.py`)

One line per Trigger, pasted into `/battle addskill`'s popup text box. Blank
lines ignored.

```
[<Timing>] <optional condition,> <effect>
:Coin<N>: [<Timing>] <optional condition,> <effect>     # per-coin timings only
[<Flag>]                                                 # skill-metadata flag, own line
```

**Skill-level timings** (`SKILL_LEVEL_TIMINGS`, no `:CoinN:` prefix): `Turn
Start`, `Combat Start`, `Turn End`, `Before Use`, `On Use`, `Clash Start`,
`Clash Win`, `Clash Lose`, `Before Attack`, `On Unopposed Attack`, `Attack
End`, `On Evade`, `Before Getting Hit`.

**Per-coin timings** (`PER_COIN_TIMINGS`, need a `:CoinN:` prefix): `Coin
Start`, `On Hit`, `Heads Hit`, `Tails Hit`, `Hit After Clash Win`, `Current
Coin Attack End`, `Heads Attack End`, `Tails Attack End`, `On Crit`, `On Crit
- Heads Hit`, `On Crit - Tails Hit`.

**`UNSUPPORTED_TIMINGS` is currently empty** — every timing the parser
recognizes has real engine dispatch behind it (Counter and Evade were the
last two to land). If I ever add a new timing name to the parser without
wiring its dispatch too, it should go in `UNSUPPORTED_TIMINGS` with a clear
reason string instead of silently getting accepted — that's the convention
I've been using for "parsed but not built yet."

**Self-buff resources** (`SELF_BUFF_STATUSES`): `Poise`, `Charge`,
`Evasion`, `Counter`. These are the only things `Gain N <X>` / `At N+ <X>`
currently recognize as a caster-held resource; anything else named there is
an unmodeled custom resource and gets rejected with a clear message.

**Skill-flag tags** (`SKILL_FLAG_TAGS`, own line, never take a `:CoinN:`
prefix): `Target Fixed`, `Unclashable`, `Indiscriminate`, `Clashable
Counter`. **Known gap**: these are parsed and stored on `Skill.tags` (and
shown in skill previews), but nothing currently reads `.tags` anywhere in
declare/clash-matching logic — they're metadata waiting on the targeting
rules that would actually enforce them.

## Known Gaps (as of the last time I updated this — might drift, worth checking against the code if it matters)

- `Skill.tags` flags (`target_fixed`, `unclashable`, `indiscriminate`,
  `clashable_counter`) are stored but not enforced anywhere yet.
- `Combat Start`/`Turn Start` fire via a full sweep of a fighter's entire
  known skill list each round (`fire_passive_triggers` in `cogs/battle.py`),
  not tied to what's actually declared. `On Evade`/`Before Getting Hit`
  similarly sweep the defender's full skill list per-incoming-hit
  (`fire_evade_triggers`/`fire_counter_triggers`) rather than needing the
  matching skill to be currently declared. This is deliberate and correct,
  not a gap — just worth knowing the mechanism before assuming these only
  fire off declared skills.
- Animated combat reveal pacing (`COIN_FACE_DELAY`/`COIN_DETAIL_DELAY`) is
  untuned beyond "verified it works" — a busy round can run long. Revisit if
  it feels too slow in real play.

## Project Structure

```
├── main.py           # Bot entry point; loads cogs, starts the connection, syncs the command tree on_ready
├── cogs/              # Discord-facing commands, grouped by feature
│   ├── battle.py       # /battle group -- by far the largest file, combat engine glue lives here
│   ├── character.py    # /character group -- persistent character CRUD
│   └── roll.py         # /roll -- standalone dice roller
├── game/              # Game logic, no Discord imports here
│   ├── battle.py        # Fighter, Battle, DeclaredAction, BATTLE_TYPES
│   ├── skills.py         # Skill, SkillResult, resolve_skill, resolve_round_clash, resolve_triggers
│   ├── conditions.py     # Trigger, TriggerContext, parse_trigger_text -- the trigger syntax parser
│   ├── character.py      # Character (persistent, JSON-backed), separate from the per-battle Fighter
│   ├── statuses.py       # StatusInstance, apply_status, decay_after_trigger
│   ├── resistances.py    # DAMAGE_TYPES, ALL_RESISTANCE_TYPES, apply_resistance
│   ├── emojis.py          # Every custom Discord emoji ID this bot uses, one place to fill in/update
│   └── colors.py          # Embed color helpers
├── data/characters/   # One JSON file per saved Character. Gitignored on
│                        purpose -- this is personal data (names, avatars,
│                        stats tied to real people), stays local, never
│                        goes in the repo.
├── requirements.txt
├── .env.example
└── .gitignore
```

## Setup

1. Clone the repo and enter it:
```bash
   git clone https://github.com/dopp000/custom-limbus-dnd-bot.git
   cd custom-limbus-dnd-bot
```
2. Create and activate a virtual environment:
```bash
   python -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
```
3. Install dependencies:
```bash
   pip install -r requirements.txt
```
4. Copy `.env.example` to `.env` and paste in your bot token:
```bash
   cp .env.example .env
```
5. Run it:
```bash
   python main.py
```
Watch for `Synced N slash command(s)` in the output — `main.py` calls
`bot.tree.sync()` on every `on_ready`, so a code change to a command's
signature is live again as soon as the bot restarts.

## Debugging Notes Worth Keeping

- **Character data is private and gitignored (`data/characters/`).** It used
  to be tracked in git by accident, which meant `git add .` would sweep
  character saves into whatever code commit I was making. Fixed by adding
  `data/characters/` to `.gitignore` and running `git rm --cached -r
  data/characters/` to untrack the files already committed (they stay on
  disk locally, just stop being tracked going forward). Worth remembering:
  that only stops FUTURE commits from including this data — anything already
  pushed still lives in the repo's git history and would need an actual
  history rewrite (`git filter-repo` or similar, plus a force-push) to
  really scrub. Haven't done that; deciding whether it's worth the hassle.
- **Discord modal field limits are a real, easy-to-hit trap**: a
  `discord.ui.TextInput` label over 45 chars or placeholder over 100 chars
  makes Discord reject the ENTIRE modal with a 400, which the caller only
  ever sees as a generic "The application did not respond" — no exception
  surfaces unless the command body is wrapped in an explicit try/except.
  `_check_modal_field_limits()` in `cogs/battle.py` runs once at import time
  against `AddSkillModal` specifically to catch this class of bug immediately
  on bot startup instead of silently at some future command call. Worth
  running any new Modal subclass through the same check.
