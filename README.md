# PackTogether

PackTogether is a Persian-first collaborative Telegram packing checklist. It supports multiple trips per group (up to 3 active), keeps one editable checklist message per trip, supports multiple contributors per item, and permanently locks each trip checklist at departure.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Create a virtual environment and install dependencies:

	```bash
	python3 -m venv .venv
	. .venv/bin/activate
	pip install -r requirements.txt
	```

3. Copy `.env.example` to `.env` and set:
   - `TELEGRAM_BOT_TOKEN`
   - `DATABASE_PATH` (local SQLite, primary runtime store)
   - Optional mirror: `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`
   - Optional sync telemetry: `DEV_ID`, `SYNC_LOG_LEVEL`, `SYNC_LOG_VERBOSE`.
4. Run the bot:

	```bash
	set -a; . .env; set +a
	python3 -m packtogether.bot
	```

## Usage

In a group, use `/newtrip`, then send the Persian trip name, a Jalali date such as `1405.06.17`, and a time such as `14:30`. Only the member who started setup can answer or cancel it. Private-chat `/newtrip` is rejected and is not included in the private command menu. Members can add one or several items by sending one item per line, tap an item to claim or unclaim it, inspect activity, select several items for batch removal, or start the trip immediately. All UI text is Persian and display dates use Jalali format.

Departure timestamps are stored as UTC ISO-8601 values after converting the supplied Jalali date/time in the `Asia/Tehran` IANA timezone. Users never need to enter Gregorian dates or timezone offsets. Departures must be strictly in the future. The service checks due trips during every read and write, so a restart after departure still locks the trip. Historical trips remain stored; each group can keep up to 3 active (`packing`) trips at once.

## Tests and architecture

Run `python3 -m pytest -q`. `packtogether/db.py` owns local SQLite + optional Supabase mirror connectivity, `service.py` contains language-independent domain operations, `ui.py` renders the Persian checklist, and `bot.py` is the Telegram adapter. Writes are local-first and mirrored to Supabase after 30s inactivity (with safety triggers).

For deployment, run the polling process under a supervisor such as systemd, Docker, or a managed worker. The included `Dockerfile` starts the bot with `python -m packtogether.bot`. Set `TELEGRAM_BOT_TOKEN` and `DATABASE_PATH`; add `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` if you want background mirroring to Supabase. Add `DEV_ID` if you want sync logs in Telegram DM. The bot only uses `/start` and `/newtrip`; the normal workflow is inline buttons.
