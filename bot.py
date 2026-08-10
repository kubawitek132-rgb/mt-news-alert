import os, sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID=os.environ["TELEGRAM_CHAT_ID"]
THREAD_ID=os.getenv("TELEGRAM_MESSAGE_THREAD_ID")
TZ=ZoneInfo(os.getenv("TIMEZONE","Europe/Warsaw"))

URL="https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACTS={"Medium","High"}
CURRENCIES={"USD","EUR","GBP","JPY","AUD","CAD","CHF","NZD"}
WINDOWS={"30":(27,33),"15":(12,18)}
DB="state.db"

def connect():
    c=sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS sent(
        event_key TEXT, alert_type TEXT, sent_at TEXT,
        PRIMARY KEY(event_key,alert_type))""")
    c.commit()
    return c

def send(text):
    payload={"chat_id":CHAT_ID,"text":text,"disable_web_page_preview":True}
    if THREAD_ID:
        payload["message_thread_id"]=int(THREAD_ID)
    r=requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json=payload,timeout=20)
    r.raise_for_status()

def instruments(cur):
    return {
        "USD":"🟡 XAUUSD • 📊 NAS100 • 💵 USD pairs",
        "EUR":"💵 EUR pairs","GBP":"💵 GBP pairs","JPY":"💵 JPY pairs",
        "AUD":"💵 AUD pairs","CAD":"💵 CAD pairs","CHF":"💵 CHF pairs",
        "NZD":"💵 NZD pairs"
    }.get(cur,"💵 FX")

def main():
    con=connect()
    now=datetime.now(timezone.utc)
    data=requests.get(URL,headers={"User-Agent":"MT-News-Alerts/1.0"},timeout=20).json()
    sent=0

    for x in data:
        impact=x.get("impact"); cur=x.get("country")
        if impact not in IMPACTS or cur not in CURRENCIES or not x.get("title"):
            continue
        try:
            dt=datetime.fromisoformat(x["date"].replace("Z","+00:00"))
        except Exception:
            continue

        mins=(dt-now).total_seconds()/60
        for label,(lo,hi) in WINDOWS.items():
            if lo <= mins <= hi:
                key=f'{x["date"]}|{cur}|{x["title"]}|{impact}'
                if con.execute("SELECT 1 FROM sent WHERE event_key=? AND alert_type=?",
                               (key,label)).fetchone():
                    continue
                local=dt.astimezone(TZ)
                icon="🔴" if impact=="High" else "🟠"
                msg=(
                    "🚨 MT NEWS ALERT\n\n"
                    f"{cur} • {icon} {impact.upper()} IMPACT\n\n"
                    f"📅 {x['title']}\n"
                    f"⏰ {local.strftime('%H:%M')} ({TZ.key})\n\n"
                    f"📊 Forecast: {x.get('forecast') or '—'}\n"
                    f"📌 Previous: {x.get('previous') or '—'}\n\n"
                    f"⏱ NEWS IN {label} MIN\n\n"
                    f"{instruments(cur)}\n\n"
                    "⚠️ Volatility may increase. Manage your risk."
                )
                send(msg)
                con.execute("INSERT INTO sent VALUES(?,?,?)",
                            (key,label,datetime.now(timezone.utc).isoformat()))
                con.commit()
                sent+=1
    print(f"Checked calendar. Sent {sent} alert(s).")

if __name__=="__main__":
    main()
