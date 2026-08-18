"""Telegram alerts for high-impact USD economic events only.

This bot deliberately contains no XAUUSD price, session, High/Low, or trading
logic. It uses Forex Factory's public calendar feed and remembers sent alerts
in stan.db so a five-minute GitHub Actions schedule does not spam Telegram.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests


TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
THREAD_ID = os.getenv("TELEGRAM_MESSAGE_THREAD_ID")

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Warsaw"))
DB = os.getenv("STATE_DB", "stan.db")
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
PRE_ALERT_MINUTES = 20
POST_RELEASE_LOOKBACK_MINUTES = 180


def db_connect():
    con = sqlite3.connect(DB)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS economic_calendar_alerts (
            event_key TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (event_key, alert_type)
        )
        """
    )
    con.commit()
    return con


def send_telegram(text):
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if THREAD_ID:
        payload["message_thread_id"] = int(THREAD_ID)

    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json=payload,
        timeout=20,
    )
    response.raise_for_status()


def printable(value):
    if value is None or str(value).strip().lower() in {"", "null", "none", "-"}:
        return "N/A"
    return str(value).strip()


def parse_event_time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(TZ)


def event_key(event, event_time):
    return "|".join(
        (
            event_time.astimezone(timezone.utc).isoformat(),
            str(event.get("country", "")).strip(),
            str(event.get("title", "")).strip(),
        )
    )


def already_sent(con, key, alert_type):
    return con.execute(
        """
        SELECT 1 FROM economic_calendar_alerts
        WHERE event_key=? AND alert_type=?
        """,
        (key, alert_type),
    ).fetchone() is not None


def mark_sent(con, key, alert_type):
    con.execute(
        """
        INSERT OR IGNORE INTO economic_calendar_alerts
        VALUES (?, ?, ?)
        """,
        (key, alert_type, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()


def upcoming_message(event, event_time):
    return (
        "🔔 MT NEWS ALERT — UPCOMING HIGH-IMPACT USD NEWS\n\n"
        f"🇺🇸 {printable(event.get('title'))}\n"
        f"⏰ {event_time.strftime('%d %b %Y, %H:%M')} Europe/Warsaw\n"
        f"📊 Forecast: {printable(event.get('forecast'))}\n"
        f"📌 Previous: {printable(event.get('previous'))}\n\n"
        "⚠️ Important USD release: expect increased market volatility."
    )


def released_message(event, event_time):
    return (
        "📰 MT NEWS ALERT — USD DATA RELEASED\n\n"
        f"🇺🇸 {printable(event.get('title'))}\n"
        f"⏰ {event_time.strftime('%d %b %Y, %H:%M')} Europe/Warsaw\n"
        f"✅ Actual: {printable(event.get('actual'))}\n"
        f"📊 Forecast: {printable(event.get('forecast'))}\n"
        f"📌 Previous: {printable(event.get('previous'))}\n\n"
        "Watch the market reaction; this is not a trade signal."
    )


def fetch_calendar():
    response = requests.get(CALENDAR_URL, timeout=20)
    response.raise_for_status()
    events = response.json()
    if not isinstance(events, list):
        raise RuntimeError("Unexpected public calendar response")
    return events


def process_calendar(con, now):
    upcoming_count = 0
    released_count = 0

    for event in fetch_calendar():
        if str(event.get("country", "")).upper() != "USD":
            continue
        if str(event.get("impact", "")).lower() != "high":
            continue

        event_time = parse_event_time(event.get("date"))
        if not event_time or printable(event.get("title")) == "N/A":
            continue

        key = event_key(event, event_time)
        minutes_until = (event_time - now).total_seconds() / 60

        if (
            0 <= minutes_until <= PRE_ALERT_MINUTES
            and not already_sent(con, key, "upcoming")
        ):
            send_telegram(upcoming_message(event, event_time))
            mark_sent(con, key, "upcoming")
            upcoming_count += 1

        if (
            -POST_RELEASE_LOOKBACK_MINUTES <= minutes_until < 0
            and printable(event.get("actual")) != "N/A"
            and not already_sent(con, key, "released")
        ):
            send_telegram(released_message(event, event_time))
            mark_sent(con, key, "released")
            released_count += 1

    return upcoming_count, released_count


def main():
    con = db_connect()
    now = datetime.now(TZ)
    try:
        upcoming_count, released_count = process_calendar(con, now)
    except (requests.RequestException, RuntimeError) as exc:
        print(f"Calendar refresh failed: {exc}")
        return
    finally:
        con.close()

    print(
        f"Calendar refreshed at {now.isoformat()}. "
        f"Upcoming alerts: {upcoming_count}. "
        f"Released-data alerts: {released_count}."
    )


if __name__ == "__main__":
    main()
