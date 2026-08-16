import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
THREAD_ID = int(os.environ["TELEGRAM_MESSAGE_THREAD_ID"])
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Warsaw"))

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"}
IMPACTS = {"Medium", "High"}

# GitHub Actions runs every 5 minutes.
# Windows are intentionally tolerant of runner/API delays.
ALERT_WINDOWS = {
    "30": (27, 33),
    "15": (12, 18),
    "5": (2, 8),
}

DB = "state.db"


# ----------------------------
# Database / state
# ----------------------------

def db_connect():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sent_alerts (
            event_key TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (event_key, alert_type)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS event_snapshots (
            event_key TEXT PRIMARY KEY,
            forecast TEXT,
            previous TEXT,
            actual TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS released_events (
            event_key TEXT PRIMARY KEY,
            released_at TEXT NOT NULL
        )
    """)
    con.commit()
    return con


def was_sent(con, event_key, alert_type):
    return con.execute(
        "SELECT 1 FROM sent_alerts WHERE event_key=? AND alert_type=?",
        (event_key, alert_type),
    ).fetchone() is not None


def mark_sent(con, event_key, alert_type):
    con.execute(
        "INSERT OR IGNORE INTO sent_alerts VALUES (?, ?, ?)",
        (event_key, alert_type, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()


# ----------------------------
# Telegram
# ----------------------------

def telegram_send(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "message_thread_id": THREAD_ID,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()


# ----------------------------
# Calendar parsing
# ----------------------------

def parse_datetime(raw):
    value = raw.get("date")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def normalize(raw):
    dt = parse_datetime(raw)
    if not dt:
        return None

    impact = str(raw.get("impact") or "").strip().title()
    currency = str(raw.get("country") or "").strip().upper()
    title = str(raw.get("title") or "").strip()

    if impact not in IMPACTS:
        return None

    if currency not in CURRENCIES:
        return None

    if not title:
        return None

    return {
        "dt": dt,
        "currency": currency,
        "impact": impact,
        "title": title,
        "forecast": str(raw.get("forecast") or "").strip(),
        "previous": str(raw.get("previous") or "").strip(),
        "actual": str(raw.get("actual") or "").strip(),
    }


def event_key(e):
    return (
        f'{e["dt"].isoformat()}|{e["currency"]}|'
        f'{e["title"]}|{e["impact"]}'
    )


# ----------------------------
# XAUUSD relevance engine
# ----------------------------

# Score 5 = major gold driver
# Score 4 = strong potential driver
# Score 3 = meaningful but less direct
# Score <3 = ignored

def xau_relevance(e):
    title = e["title"].lower()
    currency = e["currency"]
    impact = e["impact"]

    score = 0
    reasons = []

    # USD events are the core of the filter.
    if currency == "USD":
        if any(k in title for k in [
            "cpi", "consumer price index",
            "pce price", "core pce",
            "federal funds rate", "interest rate decision",
            "fed interest rate", "fomc",
            "powell", "fed chair", "federal reserve",
        ]):
            score = 5
            reasons.append("inflation / Fed / rates")

        elif any(k in title for k in [
            "non-farm", "nonfarm", "non farm", "nfp",
            "unemployment rate",
            "average hourly earnings",
        ]):
            score = 5
            reasons.append("US labor market")

        elif any(k in title for k in [
            "ppi", "producer price index",
            "retail sales", "core retail sales",
            "ism manufacturing", "ism services",
            "ism non-manufacturing",
        ]):
            score = 4
            reasons.append("US inflation / demand / activity")

        elif any(k in title for k in [
            "jobless claims", "unemployment claims",
            "initial claims", "continuing claims",
            "gdp", "gross domestic product",
            "consumer confidence",
            "durable goods",
            "manufacturing pmi", "services pmi",
            "jolts",
        ]):
            score = 3
            reasons.append("US growth / labor / sentiment")

        elif any(k in title for k in [
            "fomc member", "fed member",
            "treasury", "bond auction",
        ]):
            score = 3
            reasons.append("rates / yields / Fed")

        # Medium USD events can still cause an unexpected move.
        elif impact == "Medium":
            score = 3
            reasons.append("USD medium-impact event")

    # Selected non-USD central-bank events can affect global rates/risk
    # and therefore gold. Keep these selective.
    if currency in {"EUR", "GBP", "JPY", "CHF"}:
        if any(k in title for k in [
            "interest rate decision", "interest rate",
            "central bank", "monetary policy",
            "ecb", "boe", "boj", "snB", "snb",
            "press conference", "rate statement",
        ]):
            score = max(score, 4)
            reasons.append("central-bank / rates event")

    # Very broad risk events can matter, but do not automatically qualify
    # every foreign macro release.
    if any(k in title for k in [
        "geopolitical", "war", "emergency",
    ]):
        score = max(score, 4)
        reasons.append("risk / geopolitical")

    # High-impact non-USD macro events can be a secondary gold driver,
    # but only when the title looks like a major macro release.
    if impact == "High" and currency != "USD":
        if any(k in title for k in [
            "gdp", "inflation", "cpi", "ppi",
            "employment", "unemployment",
            "interest rate", "central bank",
        ]):
            score = max(score, 3)
            reasons.append("major global macro")

    return score, reasons


def should_alert_xau(e):
    score, reasons = xau_relevance(e)
    return score >= 3, score, reasons


# ----------------------------
# Market bias
# ----------------------------

def rule_for(title, currency):
    t = title.lower()

    if currency == "USD" and any(k in t for k in [
        "cpi", "consumer price index",
        "pce price", "core pce",
    ]):
        return (
            "inflation",
            ["USD ↑", "XAUUSD ↓", "US indices ↓"],
            ["USD ↓", "XAUUSD ↑", "US indices ↑"],
        )

    if currency == "USD" and any(k in t for k in [
        "federal funds rate", "interest rate decision",
        "fed interest rate", "fomc",
        "powell", "fed chair", "federal reserve",
    ]):
        return (
            "rates",
            ["USD ↑", "XAUUSD ↓", "US indices ↓"],
            ["USD ↓", "XAUUSD ↑", "US indices ↑"],
        )

    if currency == "USD" and any(k in t for k in [
        "non-farm", "nonfarm", "non farm", "nfp",
        "unemployment rate", "average hourly earnings",
    ]):
        return (
            "labor market",
            ["USD ↑", "XAUUSD ↓", "US indices ↓"],
            ["USD ↓", "XAUUSD ↑", "US indices ↑"],
        )

    if currency == "USD" and any(k in t for k in [
        "ppi", "producer price index",
        "retail sales", "core retail sales",
        "ism manufacturing", "ism services",
    ]):
        return (
            "US inflation / activity",
            ["USD ↑", "XAUUSD ↓", "US indices may ↓"],
            ["USD ↓", "XAUUSD ↑", "US indices may ↑"],
        )

    if currency == "USD" and any(k in t for k in [
        "jobless claims", "unemployment claims",
        "initial claims", "continuing claims",
        "gdp", "gross domestic product",
        "consumer confidence", "durable goods",
        "jolts",
    ]):
        return (
            "US macro",
            ["USD ↑", "XAUUSD ↓"],
            ["USD ↓", "XAUUSD ↑"],
        )

    if "gdp" in t or "gross domestic product" in t:
        return (
            "growth",
            [f"{currency} ↑", "Gold reaction can be mixed"],
            [f"{currency} ↓", "Gold reaction can be mixed"],
        )

    if any(k in t for k in [
        "interest rate", "central bank", "monetary policy",
        "ecb", "boe", "boj", "snb",
    ]):
        return (
            "global rates",
            [f"{currency} ↑", "Gold may face pressure if global yields rise"],
            [f"{currency} ↓", "Gold may benefit if global yields fall"],
        )

    return None


def bias_text(title, currency):
    rule = rule_for(title, currency)

    if not rule:
        return (
            "🧭 MT GOLD BIAS\n\n"
            "⚪ No clean directional rule for this event.\n"
            "Watch the actual-vs-forecast surprise and price reaction.\n\n"
            "⚠️ Scenario, not a prediction."
        )

    _, higher, lower = rule

    return (
        "🧭 MT GOLD BIAS\n\n"
        "📈 If ACTUAL > FORECAST:\n"
        + "\n".join("• " + x for x in higher)
        + "\n\n"
        "📉 If ACTUAL < FORECAST:\n"
        + "\n".join("• " + x for x in lower)
        + "\n\n"
        "⚠️ Scenario, not a prediction."
    )


def release_direction(e):
    rule = rule_for(e["title"], e["currency"])
    if not rule:
        return "⚪ No clean directional rule — watch the price reaction."

    a = to_number(e["actual"])
    f = to_number(e["forecast"])

    if a is None or f is None:
        return "⚪ Actual/Forecast is not numeric — watch the price reaction."

    _, higher, lower = rule

    if a > f:
        return (
            "📈 ACTUAL > FORECAST\n"
            + "\n".join("• " + x for x in higher)
        )

    if a < f:
        return (
            "📉 ACTUAL < FORECAST\n"
            + "\n".join("• " + x for x in lower)
        )

    return "⚪ ACTUAL = FORECAST\n• Directional edge is unclear."


def to_number(value):
    try:
        return float(
            str(value)
            .replace("%", "")
            .replace(",", "")
            .strip()
        )
    except Exception:
        return None


# ----------------------------
# Formatting
# ----------------------------

def flag(currency):
    return {
        "USD": "🇺🇸",
        "EUR": "🇪🇺",
        "GBP": "🇬🇧",
        "JPY": "🇯🇵",
        "AUD": "🇦🇺",
        "CAD": "🇨🇦",
        "CHF": "🇨🇭",
        "NZD": "🇳🇿",
    }.get(currency, "🌍")


def impact_icon(impact):
    return "🔴" if impact == "High" else "🟠"


def relevance_icon(score):
    if score >= 5:
        return "🔥"
    if score >= 4:
        return "🟠"
    return "🟡"


def relevance_text(score, reasons):
    reason_text = ", ".join(reasons[:2]) if reasons else "market relevance"
    return (
        f"{relevance_icon(score)} XAUUSD RELEVANCE: {score}/5\n"
        f"Why: {reason_text}"
    )


def pre_alert(e, minutes, score, reasons, update=False):
    local = e["dt"].astimezone(TZ)

    heading = (
        "🔄 MT NEWS UPDATE"
        if update
        else "🚨 MT NEWS ALERT"
    )

    timing = (
        "⚠️ DATA UPDATED"
        if update
        else f"⏱ NEWS IN {minutes} MIN"
    )

    return (
        f"{heading}\n\n"
        f"{flag(e['currency'])} {e['currency']} • "
        f"{impact_icon(e['impact'])} {e['impact'].upper()} IMPACT\n\n"
        f"📅 {e['title']}\n"
        f"⏰ {local.strftime('%H:%M')} ({TZ.key})\n\n"
        f"{relevance_text(score, reasons)}\n\n"
        f"📊 Forecast: {e['forecast'] or '—'}\n"
        f"📌 Previous: {e['previous'] or '—'}\n\n"
        f"{timing}\n\n"
        f"{bias_text(e['title'], e['currency'])}"
    )


def release_alert(e, score, reasons):
    local = e["dt"].astimezone(TZ)

    actual = e["actual"] or "—"
    forecast = e["forecast"] or "—"
    previous = e["previous"] or "—"

    a = to_number(e["actual"])
    f = to_number(e["forecast"])

    if a is not None and f is not None:
        surprise = f"{a - f:+g}"
    else:
        surprise = "Not calculated"

    return (
        "🔥 MT NEWS — RELEASED\n\n"
        f"{flag(e['currency'])} {e['currency']} • "
        f"{impact_icon(e['impact'])} {e['impact'].upper()} IMPACT\n\n"
        f"📅 {e['title']}\n"
        f"⏰ {local.strftime('%H:%M')} ({TZ.key})\n\n"
        f"{relevance_text(score, reasons)}\n\n"
        f"🔥 Actual: {actual}\n"
        f"📊 Forecast: {forecast}\n"
        f"📌 Previous: {previous}\n"
        f"📐 Surprise: {surprise}\n\n"
        f"🧭 MT GOLD BIAS\n\n"
        f"{release_direction(e)}\n\n"
        f"⚠️ Initial reaction ≠ guaranteed direction."
    )


# ----------------------------
# Main
# ----------------------------

def main():
    con = db_connect()
    now = datetime.now(timezone.utc)

    response = requests.get(
        CALENDAR_URL,
        headers={"User-Agent": "MT-News-Alerts/3.0"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    all_events = []
    for raw in data:
        event = normalize(raw)
        if event:
            all_events.append(event)

    xau_events = []
    for e in all_events:
        qualifies, score, reasons = should_alert_xau(e)
        if qualifies:
            xau_events.append((e, score, reasons))

    pre_sent = 0
    release_sent = 0
    updates_sent = 0

    for e, score, reasons in xau_events:
        key = event_key(e)
        delta_min = (e["dt"] - now).total_seconds() / 60.0

        # 30 / 15 / 5 minute alerts
        for alert_type, (low, high) in ALERT_WINDOWS.items():
            if low <= delta_min <= high:
                if not was_sent(con, key, alert_type):
                    telegram_send(
                        pre_alert(e, alert_type, score, reasons)
                    )
                    mark_sent(con, key, alert_type)
                    pre_sent += 1

        # Detect a meaningful calendar update before release.
        # We compare Forecast/Previous/Actual against the last snapshot.
        previous_snapshot = con.execute(
            "SELECT forecast, previous, actual FROM event_snapshots WHERE event_key=?",
            (key,),
        ).fetchone()

        current_snapshot = (
            e["forecast"],
            e["previous"],
            e["actual"],
        )

        if previous_snapshot is not None:
            changed = previous_snapshot != current_snapshot

            # Only send updates while the event is still in the future.
            # This prevents the release itself from generating an "update".
            if changed and now < e["dt"]:
                update_key = "UPDATE"
                if not was_sent(con, key, update_key):
                    telegram_send(
                        pre_alert(
                            e,
                            "",
                            score,
                            reasons,
                            update=True,
                        )
                    )
                    mark_sent(con, key, update_key)
                    updates_sent += 1

        con.execute("""
            INSERT INTO event_snapshots
                (event_key, forecast, previous, actual, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(event_key) DO UPDATE SET
                forecast=excluded.forecast,
                previous=excluded.previous,
                actual=excluded.actual,
                updated_at=excluded.updated_at
        """, (
            key,
            e["forecast"],
            e["previous"],
            e["actual"],
            datetime.now(timezone.utc).isoformat(),
        ))
        con.commit()

        # Release alert when Actual becomes available.
        if e["actual"] and now >= e["dt"]:
            already_released = con.execute(
                "SELECT 1 FROM released_events WHERE event_key=?",
                (key,),
            ).fetchone()

            if not already_released:
                telegram_send(
                    release_alert(e, score, reasons)
                )
                con.execute(
                    "INSERT INTO released_events VALUES (?, ?)",
                    (key, datetime.now(timezone.utc).isoformat()),
                )
                con.commit()
                release_sent += 1

    print(
        f"Checked {len(all_events)} Medium/High events. "
        f"XAUUSD-qualified: {len(xau_events)}. "
        f"Sent {pre_sent} pre-alert(s), "
        f"{updates_sent} update(s), "
        f"{release_sent} release alert(s)."
    )


if __name__ == "__main__":
    main()
