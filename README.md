# Limbattle Bot

A Discord bot for running Limbus Company–style coin-flip combat, built around the
custom ruleset in `limbus-mha-combat-foundation.md`.

## Project Structure

```
.
├── main.py           # Bot entry point, loads cogs, starts the connection
├── cogs/              # Discord-facing commands, grouped by feature
├── game/              # Game logic; battle state, coin resolution, status effects
│
├── data/              # Any persistent storage
├── requirements.txt
├── .env.example        # Template
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