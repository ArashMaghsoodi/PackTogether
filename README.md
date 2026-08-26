# PackTogether

PackTogether is a Persian-first collaborative Telegram packing checklist. It keeps one editable checklist message in each group, supports multiple contributors per item, and permanently locks the list at departure.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Create a virtual environment and install dependencies:

	```bash
	python3 -m venv .venv
	. .venv/bin/activate
	pip install -r requirements.txt
	```

3. Copy `.env.example` to `.env`, set `TELEGRAM_BOT_TOKEN`, and optionally set `DATABASE_PATH`.
4. Run the bot:

	```bash
	set -a; . .env; set +a
	python3 -m packtogether.bot
	```

## Usage

In a group, use `/newtrip`, then send the Persian trip name, a Jalali date such as `1405.06.17`, and a time such as `14:30`. Only the member who started setup can answer or cancel it. Private-chat `/newtrip` is rejected and is not included in the private command menu. Members can add one or several items by sending one item per line, tap an item to claim or unclaim it, inspect activity, select several items for batch removal, or start the trip immediately. All UI text is Persian and display dates use Jalali format.

Departure timestamps are stored as UTC ISO-8601 values after converting the supplied Jalali date/time in the `Asia/Tehran` IANA timezone. Users never need to enter Gregorian dates or timezone offsets. Departures must be strictly in the future. The service checks due trips during every read and write, so a restart after departure still locks the trip. Historical trips remain stored; only one `packing` trip is allowed per group.

## Tests and architecture

Run `python3 -m pytest -q`. `packtogether/db.py` owns the SQLite schema and atomic transactions, `service.py` contains language-independent domain operations, `ui.py` renders the Persian checklist, and `bot.py` is the Telegram adapter. SQLite WAL mode and `BEGIN IMMEDIATE` serialize concurrent mutations; the contribution primary key prevents duplicate claims.

For deployment, run the polling process under a supervisor such as systemd, Docker, or a managed worker, and persist the SQLite database on a durable volume. The bot only uses `/start` and `/newtrip`; the normal workflow is inline buttons.
