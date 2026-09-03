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
  they have left. That's the only toss that actually deals damage.
- **No scouting**: `/battle declare` never reveals what the enemy is bringing,
  and whether an action becomes a real Clash is only decided at `/battle
  combat` time, it only happens if both sides' declared actions target each
  other's exact slot back. If not, it resolves unopposed instead.
- **Clash-steal**: a faster ally can take over an existing ally-vs-enemy clash
  slot, with DM approval from the ally being displaced.
- **Conditional Triggers**: a bracket-tag natural-language syntax (e.g.
  `[On Use] If this unit's Sanity is 45+, Coin Power +1`), parsed by
  `game/conditions.py`, entered via the `/battle addskill` popup. See
  **Trigger Syntax** below for the full reference. This is the part most
  worth re-reading before touching `game/conditions.py`, `game/skills.py`, or
  the trigger-dispatch chain in `cogs/battle.py`'s `combat()`.
- **Poise-break Crit**: holding Poise makes coins crit for bonus damage,
  consuming one Poise count per crit, until it runs out.
- **Evasion**: holding Evasion dodges incoming coins one at a time (consumed
  per dodge), firing `[On Evade]` on the defender's own known skills via
  `fire_evade_triggers`.
- **Counter / Clashable Counter**: two distinct Skill-flag mechanics, not a
  status/resource. `[Counter]`: declared into any slot, triggers regardless
  of which one -- reactive, fires against any incoming unopposed attack on
  the holder if the Counter skill's own slot speed beats the attacker's,
  fully redirecting the attack (it never lands on its original target) and
  striking back with the Counter skill, bypassing the attacker's Evasion
  entirely (`skip_evasion` on `apply_incoming_hit`). Single-use per round.
  `[Clashable Counter]`: declared normally, resolves in regular speed
  order; if its own declared target clashes back it's just a normal Clash,
  but if it would otherwise go unopposed, it scans the holder's OTHER slots
  for any unopposed attack against them and forces a real Clash (full
  attrition via `resolve_round_clash`) against that instead, or fizzles
  with zero effect if there's nothing to intercept. Also single-use per
  round. Both reset every round via `Fighter.clear_declaration`. See
  `apply_counter_redirects` in `cogs/battle.py`.
- **Stagger**: up to 3 HP% thresholds per fighter (`Fighter.stagger_thresholds`,
  default 55%/40%/25% for Tier 1/2/3, Tier 1 highest/mildest/first
  crossed as HP drops, Tier 3 lowest/harshest/last crossed), checked via
  `Fighter.check_stagger()` right after damage lands. Each ENABLED
  tier's threshold is checked independently against current HP% (not a
  sequential crossing), and the DEEPEST tier reached becomes the active
  one -- if a tier is disabled (`Fighter.stagger_tiers_enabled`), HP in
  its range falls back to whichever shallower tier is still enabled
  instead of skipping Stagger entirely. While staggered, the NEXT
  incoming hit's total damage gets multiplied (`STAGGER_MULTIPLIERS`:
  1.5x/2.0x/2.5x for Tier 1/2/3) -- checked BEFORE this hit's own
  Stagger state updates, so the hit that first triggers Stagger doesn't
  get the bonus itself, only hits landing while ALREADY staggered do.
  Tier 1 clears at the end of the same round it triggers; Tier 2/3
  persist through one full extra round, clearing at the end of the
  NEXT round instead (`Fighter.clear_expired_stagger`, called in
  `combat()` right before `start_new_round()`). `[On Stagger]` fires on
  the ATTACKER's own skill right after damage lands if the target is
  now staggered, freshly or still. Thresholds and which tiers are
  enabled are settable per fighter via `/battle setstatus`
  (`stagger_thresholds`, `stagger_disabled_tiers` -- Tier 1 can never
  be disabled, enforced at validation time). Not yet wired into an
  actual Build Point purchase system (see Character Creation below --
  Build Points aren't built as commands anywhere yet), tier removal is
  just a directly-settable admin/testing flag for now, same as every
  other stat this bot lets you set directly.
- **Animated Combat Phase**: `/battle combat` plays out the whole round as one
  continuously-edited message. Coin-by-coin face reveals for every attrition
  round of a Clash, then the winner's final decisive toss gets its own
  face-reveal pass followed by a real-damage reveal pass (pulled from the
  actual `apply_incoming_hit` log, so it reflects resistance/Crit/Rupture/
  status exactly, not an approximation). Each unit locks into a
  permanent one-line summary as it finishes so earlier results never
  disappear while later ones are still animating. A single **Full Log**
  button (`CombatLogView`) shows the whole phase's complete breakdown as
  ephemeral embeds to whoever clicks it, chunked if long. Tuning knobs:
  `COIN_FACE_DELAY` / `COIN_DETAIL_DELAY` near the top of `combat()` in
  `cogs/battle.py`, a busy round with several Clashes can take a couple of
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
| `addskill` | Takes only `fighter` opens `AddSkillModal`, a single popup collecting name, stats (packed comma-separated: Discord caps a modal at 5 fields), per-coin statuses, and the Trigger text box. |
| `declare` | Locks a skill into one of your slots aimed at a target's slot. No scouting. see Combat Features above. |
| `undeclare` | Clears one declared slot. |
| `removefighter` | Removes a fighter from the battle entirely. Gated to that fighter's own owner or the admin role (`ADMIN_ROLE_ID = 1468446442430533737`) via `_can_manage_fighter`. |
| `setstatus` | Admin/testing tool. Directly sets HP, Sanity, Speed (min+max together), resistances (comma-separated multi-set, same pattern as `/character resistance`), Power, Stagger thresholds/which-tiers-are-enabled, and/or one status, whichever fields you actually pass. |
| `combat` | Resolves the round. See **Animated Combat Phase** above. |
| `end` | Ends the battle in the channel. |

`/battle status` was removed, the synced battle embed (`build_battle_embed`,
kept current via `sync_battle_message`) already shows everything it did.

### `/character` group (`cogs/character.py`)

| Command | Notes |
|---|---|
| `create` | New saved character, owned by whoever ran it. |
| `list` | Lists your own saved characters. |
| `view` | Shows a character's stats. Owner or admin only; `public:True` posts it to the channel. |
| `edit` | Owner-or-admin. No more flat `speed` param, `speed_min`/`speed_max` must be given together (no more implicit "just speed_min alone = flat" shortcut). Command description itself says "only fill in the fields you want to change" instead of tagging every single param `(optional)`. |
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
End`, `On Evade`, `Before Getting Hit`, `On Stagger`.

**Per-coin timings** (`PER_COIN_TIMINGS`, need a `:CoinN:` prefix): `Coin
Start`, `On Hit`, `Heads Hit`, `Tails Hit`, `Hit After Clash Win`, `Current
Coin Attack End`, `Heads Attack End`, `Tails Attack End`, `On Crit`, `On Crit
- Heads Hit`, `On Crit - Tails Hit`.

**`UNSUPPORTED_TIMINGS` is currently empty**. Every timing the parser
recognizes has real engine dispatch behind it. If I ever add a new timing
name to the parser without wiring its dispatch too, it should go in
`UNSUPPORTED_TIMINGS` with a clear reason string instead of silently
getting accepted, that's the convention I've been using for "parsed but
not built yet." `Before Getting Hit` specifically fires on a `[Counter]`
skill right after it successfully redirects and lands its retaliation
strike -- see Combat Features above.

**Self-buff resources** (`SELF_BUFF_STATUSES`): `Poise`, `Charge`,
`Evasion`. These are the only things `Gain N <X>` / `At N+ <X>`
currently recognize as a caster-held resource; anything else named there is
an unmodeled custom resource and gets rejected with a clear message.
`Counter` used to be in this list but isn't a status anymore -- see
Skill-flag tags below.

**Skill-flag tags** (`SKILL_FLAG_TAGS`, own line, never take a `:CoinN:`
prefix): `Target Fixed`, `Unclashable`, `Indiscriminate`, `Counter`,
`Clashable Counter`. All five are now enforced -- see Combat Features
above for `Counter`/`Clashable Counter`, and `declare()`/`combat()` in
`cogs/battle.py` for the other three.

## Known Gaps (as of the last time I updated this. Might drift, worth checking against the code if it matters)

- The animated visuals for a Counter redirect / Clashable Counter
  interception (the 🔁/⚡ flavor text and log wording) are only verified at
  the logic level so far, not eyeballed in an actual live Discord run yet.
  Worth a real playtest before trusting the wording reads well.
- `Skill.tags`: all five flags (`unclashable`, `target_fixed`,
  `indiscriminate`, `counter`, `clashable_counter`) are now enforced -- see
  Combat Features and Trigger Syntax above.
- `Combat Start`/`Turn Start` fire via a full sweep of a fighter's entire
  known skill list each round (`fire_passive_triggers` in `cogs/battle.py`),
  not tied to what's actually declared. `On Evade` works the same way
  (`fire_evade_triggers`, full sweep). `Before Getting Hit` is different
  now: it fires only on the ONE specific `[Counter]` skill that actually
  redirected and landed, not a sweep. This is deliberate and correct, not
  a gap, just worth knowing the mechanism before assuming these only fire
  off declared skills.
- Animated combat reveal pacing (`COIN_FACE_DELAY`/`COIN_DETAIL_DELAY`) is
  untuned beyond "verified it works", a busy round can run long. Revisit if
  it feels too slow in real play.

## Design Divergences From Canon Limbus, and Where This Is Headed

Cross-referenced the [Limbus wiki's Battles page](https://limbuscompany.wiki.gg/wiki/Battles)
against what's actually built here, deliberately skipping Deployment
Order, Offense/Defense Levels, Unbreakable/Excision Coins, Durante, and
the Backup system -- none of those fit what this bot is for. Notes on
what's the same, what's deliberately different, and what's still
genuinely missing:

**Deliberately different from canon, not gaps:**

- **Sin Affinity is replaced with Status Resistance.** Canon Limbus
  Skills carry a Sin Affinity (Wrath/Lust/Sloth/etc.) on top of a
  physical damage type, and both stack additively into the resistance
  formula. This server doesn't use Sin Affinity at all -- the five
  status-resistance types (Burn/Bleed/Tremor/Rupture/Sinking, see
  `ALL_RESISTANCE_TYPES` in `game/resistances.py`) cover that role
  instead, and only ONE resistance value ever applies to a given hit
  (the skill's own `damage_type`), never two stacked together the way
  canon's Sin Affinity + Physical Type combo works.
- **E.G.O is reframed as Supermoves.** This server's setting is an MHA x
  Project Moon crossover, so wherever canon Limbus would have E.G.O
  Skills (Awakening/Corrosion, Sanity-and-resource-gated), the
  equivalent concept here is a Supermove. No distinct E.G.O resource
  system, Sanity cost formula, or Overclocking mechanic is planned to
  be ported over as-is -- Supermoves would need their own design pass
  whenever that's actually built, not a straight reskin of E.G.O's math.
- **Corrosion isn't implemented.** Not planned for the near term either.
- **No Skill Deck / pull system.** Canon Limbus draws Skills from a
  deck (weighted by Skill Rank, refreshing once empty). This bot's
  combat isn't built around that kind of randomness -- `/battle
  declare` picking a known skill directly is the intended design, not a
  placeholder for a future deck system.
- **Evade doesn't "stay active after a clean dodge" like canon.**
  Canon Evade, if it manages to dodge every Coin of one incoming Skill,
  stays up and keeps evading against further attacks the same Turn.
  Deliberately not doing that here -- Evasion is a flat Count-based
  resource (`Gain N Evasion`, one Count consumed per dodged coin, stops
  working once it hits 0), not something that can go on indefinitely
  just because it's been lucky so far. This was a conscious choice, not
  an oversight.
- **Resistance now uses the real canon formula, asymmetric around
  Normal.** Fixed to match the wiki's actual math: a weakness
  (`resistance_pct` negative) scales damage linearly and fully, but an
  actual resistance (`resistance_pct` positive) is only HALF as
  effective per point as a weakness of the same size -- entering 20
  now reduces damage by 10%, not 20% (see `apply_resistance` in
  `game/resistances.py` for the derivation, checked against the wiki's
  own worked example). Real consequence worth remembering: because of
  that halving, no finite `resistance_pct` value ever reaches true 0
  damage on its own -- the formula asymptotically approaches 50%
  damage and never gets there. Canon Limbus's "Immune" tier is a
  separate discrete `x0` value, not something reached by stacking a
  big resistance number, and this bot doesn't have that discrete-tier
  system, just the continuous formula. If a genuine "takes zero damage
  of this type" case is ever needed, it'll need its own explicit
  override rather than just cranking the percentage up.

**Actually on the roadmap, not built yet:**

- **Guard** (a full third defense-skill type alongside Evade/Counter):
  rolls its own coins, generates Shield HP equal to Final Power, which
  soaks damage the same as regular HP but sits as a separate overhead
  pool that clears at the end of the Turn rather than persisting.
  Nothing resembling Shield exists in the engine yet.
- **Clashable Guard / Power Guard**: the Guard-family equivalent of
  Clashable Counter -- reactively clashes against the first incoming
  attack; winning raises the target's Stagger Threshold, losing reduces
  the attacker's Final Power (mitigation, not a hit-back). Depends on
  Guard/Shield existing first.
- **Offset**: when two Defense skills end up facing each other, both
  are simply cancelled with no effect. Not meaningful until there's a
  real concept of "declaring a defense skill this turn" separate from
  an attack skill, i.e. depends on Guard existing.
- **Attack Weight / multi-target Skills**: a single Skill hitting more
  than one enemy slot at once. Everything right now is strictly
  one-attacker-one-target-one-slot.
- **Kill-triggered timings**: `[On Kill]`, `[On Crit Kill]`, `[On Crit
  Kill Against Enemy]` aren't in `conditions.py` at all yet.
- **`[Failed ...]` trigger prefix**: fires when a conditional (like a
  Kill) would have activated but didn't. Not present.
- **`[Ally ...]` trigger prefix**: scopes an existing timing to only
  fire against allies, for support-style effects. Not present --
  `Indiscriminate` is targeting-only, this would be a trigger-scoping
  concept instead.
- **Panic / Low Morale**: -30 SP (Low Morale) / -45 SP (Panic)
  thresholds. Default behavior once built: Panic makes the target
  unable to act for that Turn. Both effects need to be turn-limited by
  design -- they should NOT permanently apply stat buffs/debuffs just
  because a character panics or drops to Low Morale again later; the
  effect should expire and need to be reapplied, not stack into a
  permanent state change. Also needs a customization layer similar to
  canon's [Panic Type Changing Effects](https://limbuscompany.wiki.gg/wiki/Status_Effects)
  -- both for a Sinking-user inflicting it on someone else, and for a
  character who can gain Panic/Low Morale themselves needing their own
  version of how it behaves. Not built yet.
- **Parts / Core**: for a future multi-part NPC, where each of its
  skill slots represents a distinct body part with its own separate
  resistances (damage to a Part also damages the shared Core HP pool,
  same relationship canon Focused Encounters use). Not built -- no
  NPC needing it exists yet.
- **A real guide for converting a Limbus-style passive into this bot's
  Conditional Trigger syntax.** The Trigger Syntax section above
  documents the mechanical grammar, but there's no worked-examples
  guide yet for going the other direction -- "here's a real Limbus kit
  passive, here's how you'd actually write that as `[Timing]
  condition, effect` lines." Worth writing once there's a backlog of
  real characters to draw examples from.

**Confirmed already true, not a gap:** Sanity can never be set to a
nonzero value at battle start through any normal command -- `Fighter.sanity`
defaults to 0 and `/battle addfighter` has no Sanity parameter at all. The
only way a fighter's Sanity differs from 0 at the start of Round 1 is a
passive that explicitly says so (e.g. "if User X is present, heal 5 SP on
Turn Start", via `fire_passive_triggers`), never a flat override. `/battle
setstatus` can still manually set Sanity as the admin/testing tool it's
always been -- that's a deliberate escape hatch, not a loophole in the rule.

Emoji IDs for Shield, Panic, and Low Morale (Panic and Low Morale share one
icon) are filled in now in `STATUS_EMOJI_IDS` (`game/emojis.py`) even
though the mechanics themselves aren't built yet. `guard` is still a
`None` placeholder, same pattern as `tremor_burst` -- upload the icon and
fill in the ID whenever Guard actually gets built.

## Character Creation & Progression (Level 1)

Design notes for the leveling system -- a Level 1 character here is an MHA
hero-university student with no hero license yet. None of this is built
into the bot as commands/validation right now, it's the target ruleset
`/character create`/`edit` should eventually enforce or at least help
check against.

**Two separate Build Point pools**: Stat Points and Skill Points, spent
independently.

Stat Points cover: HP, Speed range (max 8 wide), and Stagger -- both the
HP% position of each of the 3 Stagger thresholds AND whether a character
even keeps all 3.

**Stagger tier trade-off** (this is the one worth remembering in detail,
since it's a real strategic choice, not just a stat dump): every
character has all 3 Stagger checkpoints active by default, meaning up to
3 separate Staggers in one fight. Tier 1 is a guaranteed, permanent part
of every kit -- it can never be paid off. Tiers 2 and 3 CAN be stripped
for good using Build Points: remove Tier 3 and you never eat its harsh
2.5x penalty again, only the milder Tier 1/2 multipliers (1.5x/2.0x).
Remove both Tier 2 and Tier 3 and Tier 1 is the only Stagger that
character will ever face, in any fight. Duration is the other half of the
trade-off: Tier 1 clears by the end of the same Turn it triggers. Tiers 2
and 3, if kept, persist through one full extra Turn -- they don't clear
until the END of the NEXT Turn. Paying Build Points to strip a tier isn't
just "remove a downside," it's "spend upfront to guarantee you only ever
face the mild, same-turn version of Stagger."

**Sanity, in combat** (already matches the actual code in
`game/battle.py`/`cogs/battle.py` as of this writing -- if these two ever
drift apart, the code is the bug):

| Event | Change |
|---|---|
| Win a Clash | +2 SP per coin in the winning skill |
| Unopposed attack | +2 SP per coin that flips Heads, +0 for Tails |
| Lose a Clash | -3 SP flat, ignores the floor below (can push a low positive into negative) |
| Turn end, while positive | -4 SP, floored at 0 (never overshoots into negative) |
| Turn end, while negative | +2 SP, capped at 0 (recovering out of negative is slower than falling into it) |

**Character creation rules:**

- No single stat above 40% of the character's total BP pool.
- Power + Speed combined, 60% of the BP pool or less.
- Power + HP combined, 60% of the BP pool or less.
- Maximum 2 Status Effect Archetypes.
- Maximum 2 Defense Skill Archetypes.
- Status resistance refund capped at 6 BP total.
- Stagger Tier 1 can never be removed (see Stagger tier trade-off above).

**Pacing**: battles are meant to run at least 4-6 Turns for a non-serious
(spar-weight) fight, longer for anything actually serious.

**Point economy**: Build Points are earned through literacy training
(with weekly caps) and through participating in events -- not something
handed out freely or all at once.

**Still an open design question, not decided yet**: how "Power" itself
should actually work. Specifically still unresolved: how Base/Coin/Final
Power values should scale against character level and desired damage
output; how much Power budget a mid-battle skill should trade away for
carrying a status effect, and how that trade should scale; the Build
Point (or Sanity/self-debuff/limited-use) cost of building a Supermove
(this server's E.G.O-equivalent, see Design Divergences above) and what
real drawbacks (Sanity loss, a self-debuff, capped uses per battle) should
gate a strong one; and the cost of attaching extra skill tags or
amplifications on top of a base skill. None of this has concrete numbers
yet -- worth a dedicated design pass before it's treated as settled,
rather than inventing figures for it here.

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
Watch for `Synced N slash command(s)` in the output `main.py` calls
`bot.tree.sync()` on every `on_ready`, so a code change to a command's
signature is live again as soon as the bot restarts.

## Debugging Notes Worth Keeping

- **Character data is private and gitignored (`data/characters/`).** It used
  to be tracked in git by accident, which meant `git add .` would sweep
  character saves into whatever code commit I was making. Fixed by adding
  `data/characters/` to `.gitignore` and running `git rm --cached -r
  data/characters/` to untrack the files already committed (they stay on
  disk locally, just stop being tracked going forward). Worth remembering:
  that only stops FUTURE commits from including this data, anything already
  pushed still lives in the repo's git history and would need an actual
  history rewrite (`git filter-repo` or similar, plus a force-push) to
  really scrub. Haven't done that; deciding whether it's worth the hassle.
- **Discord modal field limits are a real, easy-to-hit trap**: a
  `discord.ui.TextInput` label over 45 chars or placeholder over 100 chars
  makes Discord reject the ENTIRE modal with a 400, which the caller only
  ever sees as a generic "The application did not respond". No exception
  surfaces unless the command body is wrapped in an explicit try/except.
  `_check_modal_field_limits()` in `cogs/battle.py` runs once at import time
  against `AddSkillModal` specifically to catch this class of bug immediately
  on bot startup instead of silently at some future command call. Worth
  running any new Modal subclass through the same check.
