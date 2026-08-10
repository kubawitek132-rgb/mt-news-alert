import os, sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID=os.environ["TELEGRAM_CHAT_ID"]
THREAD_ID=int(os.environ["TELEGRAM_MESSAGE_THREAD_ID"])
TZ=ZoneInfo(os.getenv("TIMEZONE","Europe/Warsaw"))

URL="https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CURRENCIES={"USD","EUR","GBP","JPY","AUD","CAD","CHF","NZD"}
IMPACTS={"Medium","High"}
WINDOWS={"30":(27,33),"15":(12,18)}
DB="state.db"

def db():
    c=sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS sent_alerts(
        event_key TEXT, alert_type TEXT, sent_at TEXT,
        PRIMARY KEY(event_key,alert_type))""")
    c.execute("""CREATE TABLE IF NOT EXISTS released_events(
        event_key TEXT PRIMARY KEY, released_at TEXT)""")
    c.commit()
    return c

def send(text):
    r=requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"message_thread_id":THREAD_ID,
              "text":text,"disable_web_page_preview":True},timeout=20)
    r.raise_for_status()

def normalize(x):
    try:
        dt=datetime.fromisoformat(x["date"].replace("Z","+00:00"))
    except Exception:
        return None
    if x.get("impact") not in IMPACTS or x.get("country") not in CURRENCIES or not x.get("title"):
        return None
    return {
        "dt":dt,"currency":x["country"],"impact":x["impact"],
        "title":(x.get("title") or "").strip(),
        "forecast":(x.get("forecast") or "").strip(),
        "previous":(x.get("previous") or "").strip(),
        "actual":(x.get("actual") or "").strip()
    }

def key(e):
    return f'{e["dt"].isoformat()}|{e["currency"]}|{e["title"]}|{e["impact"]}'

def flag(c):
    return {"USD":"🇺🇸","EUR":"🇪🇺","GBP":"🇬🇧","JPY":"🇯🇵",
            "AUD":"🇦🇺","CAD":"🇨🇦","CHF":"🇨🇭","NZD":"🇳🇿"}.get(c,"🌍")

def context(c):
    return {
        "USD":"🟡 XAUUSD • 📊 US indices • 💵 USD pairs",
        "EUR":"💵 EUR pairs • 🌍 Risk sentiment",
        "GBP":"💵 GBP pairs • 🌍 Risk sentiment",
        "JPY":"💵 JPY pairs • 🌍 Risk sentiment",
        "AUD":"💵 AUD pairs • 🌍 Risk sentiment",
        "CAD":"💵 CAD pairs • 🛢 Oil-sensitive FX",
        "CHF":"💵 CHF pairs • 🌍 Risk sentiment",
        "NZD":"💵 NZD pairs • 🌍 Risk sentiment"
    }.get(c,"💵 FX")

def rule(title,currency):
    t=title.lower()
    if currency=="USD" and any(k in t for k in ["cpi","consumer price index","core cpi","pce price","core pce"]):
        return ("inflation",
                ["USD ↑","XAUUSD ↓","US indices ↓"],
                ["USD ↓","XAUUSD ↑","US indices ↑"])
    if currency=="USD" and any(k in t for k in ["federal funds rate","fed interest rate","fomc","interest rate decision"]):
        return ("rates",
                ["USD ↑","XAUUSD ↓","US indices ↓"],
                ["USD ↓","XAUUSD ↑","US indices ↑"])
    if currency=="USD" and any(k in t for k in ["non-farm","nonfarm","non farm","nfp","unemployment rate"]):
        return ("labor market",
                ["USD ↑","XAUUSD ↓","US indices ↓"],
                ["USD ↓","XAUUSD ↑","US indices ↑"])
    if "gdp" in t or "gross domestic product" in t:
        return ("growth",
                [f"{currency} ↑","Risk assets may react ↑","Gold reaction can be mixed"],
                [f"{currency} ↓","Risk assets may react ↓","Gold reaction can be mixed"])
    if "pmi" in t:
        return ("business activity",
                [f"{currency} ↑","Risk sentiment may improve","Rates expectations may matter"],
                [f"{currency} ↓","Risk sentiment may weaken","Rates expectations may matter"])
    return None

def bias(title,currency):
    r=rule(title,currency)
    if not r:
        return "🧭 MT MARKET BIAS\n\n⚪ No clean directional rule for this event.\nWatch actual vs forecast and price reaction.\n\n⚠️ Scenario, not a prediction."
    _,hi,lo=r
    return ("🧭 MT MARKET BIAS\n\n📈 If ACTUAL > FORECAST:\n" +
            "\n".join("• "+x for x in hi) +
            "\n\n📉 If ACTUAL < FORECAST:\n" +
            "\n".join("• "+x for x in lo) +
            "\n\n⚠️ Scenario, not a prediction.")

def pre(e,mins):
    local=e["dt"].astimezone(TZ)
    icon="🔴" if e["impact"]=="High" else "🟠"
    return (f"🚨 MT NEWS ALERT\n\n{flag(e['currency'])} {e['currency']} • {icon} {e['impact'].upper()} IMPACT\n\n"
            f"📅 {e['title']}\n⏰ {local.strftime('%H:%M')} ({TZ.key})\n\n"
            f"📊 Forecast: {e['forecast'] or '—'}\n📌 Previous: {e['previous'] or '—'}\n\n"
            f"⏱ NEWS IN {mins} MIN\n\n{context(e['currency'])}\n\n{bias(e['title'],e['currency'])}")

def num(v):
    try: return float(str(v).replace("%","").replace(",","").strip())
    except: return None

def release(e):
    r=rule(e["title"],e["currency"])
    a,f=num(e["actual"]),num(e["forecast"])
    surprise="Not calculated" if a is None or f is None else f"{a-f:+g}"
    if r and a is not None and f is not None:
        _,hi,lo=r
        direction=("📈 Actual > Forecast\n" + "\n".join("• "+x for x in hi)
                   if a>f else "📉 Actual < Forecast\n" + "\n".join("• "+x for x in lo)
                   if a<f else "⚪ Actual = Forecast\n• Directional edge is unclear.")
    else:
        direction="⚪ No clean directional rule — watch the price reaction."
    local=e["dt"].astimezone(TZ)
    icon="🔴" if e["impact"]=="High" else "🟠"
    return (f"🔥 MT NEWS — RELEASED\n\n{flag(e['currency'])} {e['currency']} • {icon} {e['impact'].upper()} IMPACT\n\n"
            f"📅 {e['title']}\n⏰ {local.strftime('%H:%M')} ({TZ.key})\n\n"
            f"🔥 Actual: {e['actual'] or '—'}\n📊 Forecast: {e['forecast'] or '—'}\n📌 Previous: {e['previous'] or '—'}\n"
            f"📐 Surprise: {surprise}\n\n🧭 MT MARKET BIAS\n\n{direction}\n\n"
            f"⚠️ Initial reaction ≠ guaranteed direction.\n{context(e['currency'])}")

def main():
    c=db()
    now=datetime.now(timezone.utc)
    data=requests.get(URL,headers={"User-Agent":"MT-News-Alerts/2.0"},timeout=20).json()
    events=[e for x in data if (e:=normalize(x))]
    pre_sent=rel_sent=0

    for e in events:
        k=key(e)
        delta=(e["dt"]-now).total_seconds()/60
        for typ,(lo,hi) in WINDOWS.items():
            if lo<=delta<=hi and not c.execute(
                "SELECT 1 FROM sent_alerts WHERE event_key=? AND alert_type=?",(k,typ)).fetchone():
                send(pre(e,typ))
                c.execute("INSERT INTO sent_alerts VALUES(?,?,?)",(k,typ,datetime.now(timezone.utc).isoformat()))
                c.commit(); pre_sent+=1

        if e["actual"] and now>=e["dt"] and not c.execute(
            "SELECT 1 FROM released_events WHERE event_key=?",(k,)).fetchone():
            send(release(e))
            c.execute("INSERT INTO released_events VALUES(?,?)",(k,datetime.now(timezone.utc).isoformat()))
            c.commit(); rel_sent+=1

    print(f"Checked {len(events)} Medium/High events. Sent {pre_sent} pre-alert(s), {rel_sent} release alert(s).")

if __name__=="__main__":
    main()

