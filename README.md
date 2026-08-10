# MT News Alerts V1 — COMMUNITY / MT NEWS ALERT TOPIC

This version sends alerts directly into the Telegram Community forum topic:
**MT NEWS ALERT**

Features:
- 🟠 Medium Impact
- 🔴 High Impact
- 30 min + 15 min alerts
- Forecast + Previous
- Currency + event + time
- Duplicate protection

Telegram's Bot API supports `message_thread_id` for sending a message to a specific forum topic.

SETUP

1. Create the bot with @BotFather.
2. Add the bot to `MARKET & THEORY | COMMUNITY`.
3. Make it an administrator with permission to post messages. For topic management, give topic-management rights if needed.
4. In the Community, open/create the topic named `MT NEWS ALERT`.
5. We need two identifiers:
   - TELEGRAM_CHAT_ID = the Community supergroup ID
   - TELEGRAM_MESSAGE_THREAD_ID = the MT NEWS ALERT topic ID

6. In GitHub repository Secrets add:
   TELEGRAM_BOT_TOKEN
   TELEGRAM_CHAT_ID
   TELEGRAM_MESSAGE_THREAD_ID

The code sends `message_thread_id` with every alert, so alerts go into MT NEWS ALERT rather than General.

DATA SOURCE
V1 uses:
https://nfs.faireconomy.media/ff_calendar_thisweek.json

Check the source's current terms and limits before commercial or large-scale redistribution.

V2 can add:
- Actual after release
- market reaction
- daily calendar
- better instrument mapping
- admin test command
