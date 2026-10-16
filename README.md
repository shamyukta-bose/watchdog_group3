# Group 3 Watchdog — Jacobs Industries Supply Chain Game

A local Python automation that scrapes the Responsive Learning Supply Chain
Game on a schedule, runs a seasonal demand forecast + reorder-point /
batch-size / capacity optimisation + end-game cleanout planner, and emails
you the recommended settings as an Excel attachment.

We compete in **monitor + recommend** mode: the bot does NOT submit
decisions to the game. It tells you what to set; you change the values in
the game UI.

## What's different from Group 7

| Area | Group 7 (rank 1, prior class) | Group 3 (this project) |
|------|------------------------------|------------------------|
| Forecast | 10 equal "phases" with flat mean/stdev per phase | Per-day-of-year seasonal index fit on the full 2-year history with a 7-day smoother |
| Volatility | Phase stdev (includes seasonal swings) | Residual stdev after removing the seasonal component → tighter, less-padded safety stock |
| Reorder point | `μ_phase × L + Z × σ_phase × √L` | `Σ forecast_mean[d..d+L-1] + Z × √Σ var[d..d+L-1]` (uses the actual seasonal mean over the lead-time window) |
| Batch size | Heuristic, often Q=150 to fill a truck | EOQ with explicit truck-fill vs mail break-even logic |
| End-game | A rule-of-thumb dashboard ("if inventory > 120, set OP=0") | Phased planner (GROWTH → STEADY → RAMPDOWN → CLEANOUT) tied to expected-remaining-demand and lead-time math |
| Reporting | Excel + Gmail SMTP via GitHub Actions | Excel + Gmail SMTP via local Python loop |

## Project layout

```
watchdog_group3/
├── run.py                   # entry point
├── requirements.txt
├── config.example.json      # copy to config.json and edit
├── data/
│   └── demand_history.csv   # 730 days extracted from Group 7's workbook
├── outputs/                 # generated Excel reports + raw scrape data
├── logs/                    # per-day log files
└── src/
    ├── forecasting.py       # seasonal demand model
    ├── rop_calculator.py    # ROP, EOQ, truck/mail, capacity
    ├── endgame.py           # day-1460 cleanout planner
    ├── scraper.py           # login + scrape the game
    ├── reporter.py          # Excel workbook + SMTP email
    └── main.py              # orchestrator
```

## Setup (one-time)

1. **Install Python 3.10+** (3.11 recommended).

2. **Install dependencies:**

   ```bash
   cd watchdog_group3
   python3 -m venv .venv
   source .venv/bin/activate         # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Configure:**

   ```bash
   cp config.example.json config.json
   # then edit config.json:
   #   - game.team_id / game.password are pre-filled with group3 / 0000
   #   - game.institution: set if your section uses one (e.g., "ucsb")
   #   - email.smtp_user / email.smtp_password: Gmail address + APP password
   #     (NOT your normal Gmail login). See README "Gmail app password".
   #   - email.recipients: who gets the report (your teammates' addresses).
   ```

   You can also use environment variables instead of editing config.json:
   `SC_TEAM_ID`, `SC_PASSWORD`, `SMTP_PASSWORD`.

4. **(Optional) Gmail app password.** Standard Gmail won't accept a script
   login. Create one at <https://myaccount.google.com/apppasswords> and put
   it in `email.smtp_password`.

## Run

Smoke-test the math without scraping the game (good for the first run):

```bash
python run.py --once --dry-run --no-scrape
```

Open `outputs/watchdog_report_latest.xlsx` to see what the report looks
like.

One full cycle (scrape + report + email):

```bash
python run.py --once
```

Continuous loop (scrape every 30 min, email every hour by default):

```bash
python run.py
```

Stop with Ctrl-C.

## Interpreting the report

The Excel workbook has these tabs:

- **Summary** — the headline numbers you change in the game UI:
  - Recommended Order Point
  - Recommended Batch Size
  - Recommended Shipping (Truck / Mail)
  - Recommended Target Capacity (only relevant for the first ~6 months;
    after day ~1370 the optimizer refuses to expand because the 90-day
    capacity lead-time eats too much of the remaining horizon).
- **Decision Notes** — narrated reasoning from the policy + end-game engines.
- **ROP Calculation** — Z-score, lead-time mean, lead-time stdev, etc.
- **Forecast (next 60d)** — what the model thinks demand will look like.
- **Seasonal Index** — 365-day seasonal multiplier (use to spot peak weeks).
- **Demand / Inventory / Cash Series** — the latest scrape's time series.
- **Team Standing** — current rankings (for trash talk in WhatsApp).

## Updating decisions in the game

Take the **Summary** sheet's recommended values and set them in the game:

1. Login at <https://op.responsive.net/SupplyChain/SCAccess> as group3 / 0000.
2. Open **Factory → Calopeia → Settings**:
   - Order point (reorder threshold)
   - Batch size (Q)
   - Shipping: Truck or Mail
   - Capacity expansion (only if the report says to add)
3. Submit.

The watchdog will pick up the new values on the next scrape cycle and
flow them through to the next report.

## Troubleshooting

- **Scrape login fails.** The game's POST endpoint changes between class
  deployments. Edit `src/scraper.py` → `ENDPOINTS["login_post_fallbacks"]`
  with the URL your section uses (check the form `action=` attribute in
  the browser dev tools after a manual login).

- **Empty time series in the report.** The scraper looks for JavaScript
  arrays named `days`, `x`, `values`, `y`, etc. If your section uses
  different names, edit `src/scraper.py` → `_series_from_arrays` candidate
  lists.

- **Gmail "Authentication Required".** You need an **app password** (see
  Setup step 4), not your normal Gmail password.

- **Forecast looks flat / wrong.** Check `data/demand_history.csv` has all
  730 days. The seasonal model needs at least one full year (365 days)
  before it does anything useful; with less, it falls back to a flat mean.

## Strategy cheat-sheet

Demand is highly seasonal (peaks ≈ days 60-120 each year) and there's no
trend. Two-year average is ~39 drums/day; peaks can hit 80+, troughs <10.

Day 730 → ~Day 1370: **GROWTH / STEADY.** Run the seasonal ROP, fill trucks
(Q=200) where EOQ approves it, expand capacity once if peak forecast >
current capacity × 1.10 AND remaining payback days cover it.

Day ~1370 → ~Day 1440: **RAMPDOWN.** Switch to mail (1-day transit).
Smaller batches. Don't restock more than expected remaining demand + 7-day
safety.

Day ~1440 → Day 1460: **CLEANOUT.** Set OP=0, Q=0, mail. Drain warehouse.
Every drum NOT sold by day 1460 is a pure loss.

## License / disclaimer

Built for MGT 267 / equivalent supply-chain class use. The strategy and
math are general; the specific URL/HTML scraping in `scraper.py` is
brittle by nature — expect to edit it once when you first try it against
the live game.
