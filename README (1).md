# MHA × Limbus Battle Bot

A Discord bot for running Limbus Company–style coin-flip combat, built around the
custom ruleset in `limbus-mha-combat-foundation.md`.

## Project Structure

```
.
├── main.py           # Bot entry point — loads cogs, starts the connection
├── cogs/              # Discord-facing commands, grouped by feature (Lesson 2+)
├── game/              # Pure game logic — battle state, coin resolution, status effects
│                       #   (kept separate from Discord code so it can be tested on its own)
├── data/              # Any persistent storage (Lesson 6+)
├── requirements.txt
├── .env.example        # Template — copy to .env and fill in your real token
└── .gitignore
```

## Setup

1. Clone the repo and enter it:
   ```bash
   git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
   cd YOUR_REPO
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

## Roadmap

- [x] Lesson 1 — bot connects, responds to `/ping`
- [ ] Lesson 2 — Cogs + `Battle` data class
- [ ] Lesson 3 — admin commands (`/battle create`, add fighters)
- [ ] Lesson 4 — declare phase + sequential coin resolution
- [ ] Lesson 5 — status effects & SP economy
- [ ] Lesson 6 — embeds, buttons, persistence
