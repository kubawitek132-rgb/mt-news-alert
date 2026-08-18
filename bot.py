"""Gold-relevant USD economic-calendar alerts for Telegram.

Uses a public Forex Factory calendar feed.  It sends one alert around 30, 15,
and 5 minutes before each selected event, keeps a durable sent-alert state in
stan.db, and notes forecast changes between polling runs.
"""

import os
import re
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests


TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
THREAD_ID = os.getenv("TELEGRAM_MESSAGE_THREAD_ID")

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Warsaw"))
DB = os.getenv("STATE_DB", "stan.db")
TEST_MODE = os.getenv("NEWS_TEST_MODE", "").strip().lower() == "true"
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Each interval is intentionally wider than five minutes because GitHub Actions
# can start a scheduled run a few minutes late.
PRE_ALERT_WINDOWS = (
    ("30m", 25, 35),
    ("15m", 10, 20),
    ("5m", 0, 8),
)
POST_RELEASE_LOOKBACK_MINUTES = 180

# All High USD events are included.  These selected Medium events can also
# affect gold through USD, real-yield, oil, or risk-sentiment expectations.
GOLD_RELEVANT_MEDIUM_PATTERNS = (
    "unemployment claims",
    "jobless claims",
    "retail sales",
    "ppi",
    "durable goods",
    "gdp",
    "ism",
    "consumer confidence",
    "uom consumer sentiment",
    "jolts",
    "eia crude oil",
    "eia cushing",
    "fed chair",
    "powell speaks",
    "fomc",
)
REVERSED_USD_PATTERNS = ("unemployment rate", "unemployment claims", "jobless claims")
ENERGY_PATTERNS = ("eia crude oil", "eia cushing", "crude oil inventories")
RATE_PATTERNS = ("fomc", "federal funds rate", "fed chair", "powell speaks")


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
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS economic_calendar_snapshots (
            event_key TEXT PRIMARY KEY,
            forecast TEXT,
            previous TEXT,
            observed_at TEXT NOT NULL
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


def numeric_value(value):
    """Parse common calendar values such as 1.2%, 250K, or -3.1M."""
    text = printable(value).replace(",", ".")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group())
    lower = text.lower()
    if "k" in lower:
        number *= 1_000
    elif "m" in lower:
        number *= 1_000_000
    return number


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


def previous_snapshot(con, key):
    return con.execute(
        """
        SELECT forecast, previous FROM economic_calendar_snapshots
        WHERE event_key=?
        """,
        (key,),
    ).fetchone()


def save_snapshot(con, key, forecast, previous):
    con.execute(
        """
        INSERT INTO economic_calendar_snapshots
            (event_key, forecast, previous, observed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(event_key) DO UPDATE SET
            forecast=excluded.forecast,
            previous=excluded.previous,
            observed_at=excluded.observed_at
        """,
        (key, forecast, previous, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()


def is_gold_relevant(event):
    if str(event.get("country", "")).upper() != "USD":
        return False

    title = str(event.get("title", "")).lower()
    impact = str(event.get("impact", "")).lower()
    return impact == "high" or any(pattern in title for pattern in GOLD_RELEVANT_MEDIUM_PATTERNS)


def gold_bias(title, compared_value, reference_value, released=False):
    """Return a conditional market-reading, never a definitive price call."""
    title_lower = title.lower()
    compared = numeric_value(compared_value)
    reference = numeric_value(reference_value)

    if any(pattern in title_lower for pattern in ENERGY_PATTERNS):
        return "Oil data: possible indirect effect on inflation expectations and XAUUSD; direction is not reliable before the release."
    if any(pattern in title_lower for pattern in RATE_PATTERNS):
        return "Fed/rates event: XAUUSD direction depends mainly on the decision and tone versus market pricing."
    if compared is None or reference is None or compared == reference:
        return "No clear directional bias from the available forecast; the actual result versus forecast will matter most."

    stronger_reading = compared > reference
    if any(pattern in title_lower for pattern in REVERSED_USD_PATTERNS):
        stronger_reading = not stronger_reading

    if released:
        subject = "Actual above forecast" if stronger_reading else "Actual below forecast"
    else:
        subject = "Forecast above previous" if stronger_reading else "Forecast below previous"

    if stronger_reading:
        return f"{subject}: usually supports USD and may create downward pressure on XAUUSD."
    return f"{subject}: may weaken USD and create upward pressure on XAUUSD."


def forecast_change_text(snapshot, forecast, previous):
    if not snapshot:
        return "Forecast update: no earlier snapshot yet."
    old_forecast, old_previous = snapshot
    changes = []
    if printable(old_forecast) != printable(forecast):
        changes.append(f"Forecast changed: {printable(old_forecast)} → {printable(forecast)}")
    if printable(old_previous) != printable(previous):
        changes.append(f"Previous revised: {printable(old_previous)} → {printable(previous)}")
    return "\n".join(changes) if changes else "Forecast update: no change since the previous refresh."


def pre_release_message(event, event_time, minutes_until, snapshot):
    title = printable(event.get("title"))
    forecast = printable(event.get("forecast"))
    previous = printable(event.get("previous"))
    return (
        "🔔 MT NEWS ALERT — GOLD-RELEVANT USD EVENT\n\n"
        f"🇺🇸 {title}\n"
        f"⏰ In about {round(minutes_until)} min · {event_time.strftime('%H:%M')} Warsaw\n"
        f"🎯 Impact: {printable(event.get('impact'))}\n"
        f"📊 Forecast: {forecast}\n"
        f"📌 Previous: {previous}\n\n"
        f"🔄 {forecast_change_text(snapshot, forecast, previous)}\n\n"
        f"🟡 XAUUSD scenario: {gold_bias(title, forecast, previous)}\n\n"
        "This is a conditional market context, not a trade signal."
    )


def released_message(event, event_time):
    title = printable(event.get("title"))
    actual = printable(event.get("actual"))
    forecast = printable(event.get("forecast"))
    return (
        "📰 MT NEWS ALERT — USD DATA RELEASED\n\n"
        f"🇺🇸 {title}\n"
        f"⏰ Released at {event_time.strftime('%H:%M')} Warsaw\n"
        f"✅ Actual: {actual}\n"
        f"📊 Forecast: {forecast}\n"
        f"📌 Previous: {printable(event.get('previous'))}\n\n"
        f"🟡 XAUUSD scenario: {gold_bias(title, actual, forecast, released=True)}\n\n"
        "Market reaction can reverse quickly; this is not a trade signal."
    )


def fetch_calendar():
    response = requests.get(CALENDAR_URL, timeout=20)
    response.raise_for_status()
    events = response.json()
    if not isinstance(events, list):
        raise RuntimeError("Unexpected public calendar response")
    return events


def process_calendar(con, now):
    pre_alerts = 0
    released_alerts = 0

    for event in fetch_calendar():
        if not is_gold_relevant(event):
            continue

        event_time = parse_event_time(event.get("date"))
        if not event_time or printable(event.get("title")) == "N/A":
            continue

        key = event_key(event, event_time)
        forecast = printable(event.get("forecast"))
        previous = printable(event.get("previous"))
        snapshot = previous_snapshot(con, key)
        minutes_until = (event_time - now).total_seconds() / 60

        for alert_type, minimum, maximum in PRE_ALERT_WINDOWS:
            if not (minimum <= minutes_until <= maximum):
                continue
            if already_sent(con, key, alert_type):
                continue
            send_telegram(pre_release_message(event, event_time, minutes_until, snapshot))
            mark_sent(con, key, alert_type)
            pre_alerts += 1

        actual = printable(event.get("actual"))
        if (
            -POST_RELEASE_LOOKBACK_MINUTES <= minutes_until < 0
            and actual != "N/A"
            and not already_sent(con, key, "released")
        ):
            send_telegram(released_message(event, event_time))
            mark_sent(con, key, "released")
            released_alerts += 1

        save_snapshot(con, key, forecast, previous)

    return pre_alerts, released_alerts


def main():
    con = db_connect()
    now = datetime.now(TZ)
    try:
        if TEST_MODE:
            send_telegram(
                "✅ MT NEWS ALERT — TEST\n\n"
                "GitHub Actions → Telegram connection works.\n"
                "No market-news alert was generated by this test."
            )
            print("Test notification sent successfully.")
            return

        pre_alerts, released_alerts = process_calendar(con, now)
        print(
            f"Calendar refreshed at {now.isoformat()}. "
            f"Pre-release alerts: {pre_alerts}. "
            f"Released-data alerts: {released_alerts}."
        )
    except (requests.RequestException, RuntimeError) as exc:
        print(f"Calendar refresh failed: {exc}")
        raise
    finally:
        con.close()


if __name__ == "__main__":
    main()
