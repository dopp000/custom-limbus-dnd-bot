# Limbattle Bot

A Discord bot for running Limbus Company–style coin-flip combat, built around the
custom ruleset.

## Combat Features

- Coin-based skill resolution: sequential coin tosses, Heads permanently raise Power, Tails still hit at current Power
- Sanity-driven coin odds, with clash win/loss and unopposed-hit economy
- Round-based Clash attrition, slot-targeted `/battle declare` with scouting previews
- Clash-steal: a faster ally can take over an existing clash with DM approval
- Conditional Triggers: a bracket-tag natural language syntax (e.g. `[On Use] If this unit's Sanity is 45+, Coin Power +1`) entered via a modal on `/battle addskill`
- Poise-break Crit: holding Poise makes coins crit for bonus damage until it runs out
- Evasion: holding Evasion dodges incoming coins one at a time, firing `[On Evade]` on the defender's own skills

## Project Structure

```
├── main.py # Bot entry point; loads cogs, starts the connection
├── cogs/ # Discord-facing commands, grouped by feature
├── game/ # Game logic; battle state, coin resolution, status effects
│
├── data/ # Any persistent storage
├── requirements.txt
├── .env.example # Template
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