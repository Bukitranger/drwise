"""
DrWise — Personal Health Coach Telegram Bot
Reads health data from Supabase (written by HAE via Edge Function).
Meals stored in Supabase via FatMaster.
No webhook server needed — pure Telegram bot.
"""

import os
import json
import logging
import threading
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from queue import Queue
import urllib.request
import urllib.error

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import anthropic

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
MY_CHAT_ID     = os.environ.get("MY_CHAT_ID")
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
message_queue: Queue = Queue()


# ── Supabase ──────────────────────────────────────────────────────────────────

def sb(method, table, data=None, filters=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if filters:
        url += "?" + "&".join(f"{k}={v}" for k, v in filters.items())
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    body = json.dumps(data, default=str).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        logger.error(f"Supabase {method} {table} error {e.code}: {e.read()[:200]}")
        return None
    except Exception as e:
        logger.error(f"Supabase request failed: {e}")
        return None


def get_recent_health(days=7):
    cutoff = str(date.today() - timedelta(days=days))
    records = sb("GET", "health_data", filters={
        "recorded_date": f"gte.{cutoff}",
        "order": "recorded_date",
        "select": "recorded_date,data"
    })
    if not records:
        return {}
    return {r["recorded_date"]: r["data"] for r in records}


def get_today_health():
    records = sb("GET", "health_data", filters={
        "recorded_date": f"eq.{date.today()}",
        "select": "data"
    })
    if records and len(records) > 0:
        return records[0]["data"]
    return {}


def save_meal(meal):
    today = str(date.today())
    meal_with_time = {**meal, "time": datetime.now().isoformat()}
    sb("POST", "meals", data={"recorded_date": today, "data": meal_with_time})


def get_recent_meals(days=7):
    cutoff = str(date.today() - timedelta(days=days))
    records = sb("GET", "meals", filters={
        "recorded_date": f"gte.{cutoff}",
        "order": "recorded_date",
        "select": "recorded_date,data"
    })
    if not records:
        return {}
    result = {}
    for r in records:
        d = r["recorded_date"]
        if d not in result:
            result[d] = []
        result[d].append(r["data"])
    return result


def get_today_meals():
    records = sb("GET", "meals", filters={
        "recorded_date": f"eq.{date.today()}",
        "order": "created_at",
        "select": "data"
    })
    return [r["data"] for r in records] if records else []


# ── Claude AI ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are DrWise, a personal health coach and friend.
Talk casually, like a smart friend who knows a lot about health, nutrition, fitness and recovery.
No corporate speak, no excessive disclaimers. Be direct, warm, and practical.
User's main goal: LOSE WEIGHT while building healthy habits.
You have access to meal logs, sleep data, activity, heart rate, and body metrics.
The health data comes from Apple Health / Oura / Withings via Health Auto Export.
Metric names follow Apple Health conventions e.g. weight_body_mass, body_fat_percentage, sleep_analysis, heart_rate, step_count.
Each metric has a 'data' array of readings and a 'units' field.
Always personalize advice based on the actual data. Keep responses concise — this is Telegram.
Use emojis naturally. Max 200 words unless doing a weekly report.

IMPORTANT USER PREFERENCES:
- All weight is in KILOGRAMS (kg). Never use lbs.
- Distances in KILOMETERS (km). Never miles.
- User is in Tokyo, Japan (JST = UTC+9).
- Weight and body composition from Withings scale.
- Sleep and recovery from Oura Ring."""


def ask_claude(user_message, context_data=None):
    context = ""
    if context_data:
        raw = json.dumps(context_data, default=str)
        if len(raw) > 6000:
            raw = raw[:6000] + "... [truncated]"
        context = f"\n\nUser's health data:\n{raw}"
    response = anthropic_client.messages.create(
        model="claude-opus-4-5", max_tokens=1000, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message + context}]
    )
    return response.content[0].text


def build_daily_briefing():
    return ask_claude(
        "Give me my morning health briefing. Look at last night's sleep, yesterday's meals and activity. "
        "How recovered am I? What to focus on today? One specific nutrition tip. Punchy, not an essay.",
        {"today_health": get_today_health(), "yesterday_meals": get_recent_meals(1), "goal": "lose weight"}
    )


def build_weekly_report():
    return ask_claude(
        "Give me my weekly health report. Analyze sleep patterns, nutrition trends, activity levels, "
        "body metric changes. What went well? What needs work? 3 specific goals for next week.",
        {"week_health": get_recent_health(7), "week_meals": get_recent_meals(7), "goal": "lose weight"}
    )


def build_meal_reaction(meal):
    today_meals = get_today_meals()
    return ask_claude(
        "I just logged a meal. React to it in context of my whole day. "
        "Am I on track for weight loss? Should I adjust my next meal? 2-3 sentences.",
        {
            "just_logged": meal,
            "today_meals_so_far": today_meals,
            "today_total_calories": sum(m.get("calories", 0) for m in today_meals),
            "today_total_protein": sum(m.get("protein", 0) for m in today_meals),
            "today_health": get_today_health(),
            "goal": "lose weight"
        }
    )


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def start(update, context):
    uid = update.effective_user.id
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"Hey {name}! 👋 I'm DrWise, your personal health coach.\n\n"
        f"Your Telegram ID is: {uid}\n\n"
        f"/briefing — morning summary\n"
        f"/weekly — full week report\n"
        f"/today — today's stats\n"
        f"/status — data status\n\n"
        f"Or just chat with me! 💬"
    )


async def briefing_cmd(update, context):
    await update.message.reply_text("Pulling your data... 🔍")
    await update.message.reply_text(build_daily_briefing())


async def weekly_cmd(update, context):
    await update.message.reply_text("Crunching your whole week... 📊")
    await update.message.reply_text(build_weekly_report())


async def today_cmd(update, context):
    meals = get_today_meals()
    health = get_today_health()
    lines = ["📅 Today so far\n"]
    if health:
        lines.append(f"📊 {len(health)} health metrics synced from Apple Health")
        lines.append("")
    if meals:
        lines += [
            f"🍽 Meals: {len(meals)}",
            f"🔥 Calories: {sum(m.get('calories', 0) for m in meals)} kcal",
            f"💪 Protein: {sum(m.get('protein', 0) for m in meals):.0f}g",
            f"🍞 Carbs: {sum(m.get('carbs', 0) for m in meals):.0f}g",
            f"🥑 Fat: {sum(m.get('fat', 0) for m in meals):.0f}g",
        ]
    else:
        lines.append("🍽 No meals logged yet\n(Send a photo to FatMaster!)")
    await update.message.reply_text("\n".join(lines))


async def status_cmd(update, context):
    health = get_recent_health(90)
    meals = get_recent_meals(90)
    last_health = max(health.keys()) if health else "never"
    last_meal = max(meals.keys()) if meals else "never"
    await update.message.reply_text(
        f"📡 DrWise Status\n\n"
        f"Health data: {len(health)} days in Supabase\n"
        f"Last sync: {last_health}\n\n"
        f"Meal data: {len(meals)} days in Supabase\n"
        f"Last meal: {last_meal}\n\n"
        f"Storage: Supabase ✅ (persistent, never resets)"
    )


async def handle_text(update, context):
    today_health = get_today_health()
    health_str = json.dumps(today_health, default=str)[:4000]
    ctx = {
        "today_health": health_str,
        "recent_meals": get_recent_meals(3),
        "goal": "lose weight"
    }
    await update.message.reply_text(ask_claude(update.message.text, ctx))


async def drain_message_queue(context):
    while not message_queue.empty():
        try:
            chat_id, text = message_queue.get_nowait()
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.error(f"Queue drain error: {e}")


# ── Meal webhook (from FatMaster) ─────────────────────────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"DrWise is alive!")

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
        except:
            self.send_response(400)
            self.end_headers()
            return

        if path == "/meal":
            save_meal(payload)
            logger.info(f"Meal from FatMaster: {payload.get('meal', '?')}")
            if MY_CHAT_ID:
                try:
                    reaction = build_meal_reaction(payload)
                    message_queue.put((int(MY_CHAT_ID), reaction))
                    logger.info("Meal reaction queued ✓")
                except Exception as e:
                    logger.error(f"Meal reaction error: {e}")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


def run_webhook_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), WebhookHandler).serve_forever()


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def send_daily_briefing(context):
    if MY_CHAT_ID:
        await context.bot.send_message(
            chat_id=int(MY_CHAT_ID),
            text=f"☀️ Morning Briefing\n\n{build_daily_briefing()}"
        )


async def send_weekly_report(context):
    if MY_CHAT_ID:
        await context.bot.send_message(
            chat_id=int(MY_CHAT_ID),
            text=f"📊 Weekly Report\n\n{build_weekly_report()}"
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    threading.Thread(target=run_webhook_server, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("briefing", briefing_cmd))
    app.add_handler(CommandHandler("weekly", weekly_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    jq = app.job_queue
    jq.run_repeating(drain_message_queue, interval=5, first=5)
    jq.run_daily(send_daily_briefing, time=datetime.strptime("08:00", "%H:%M").time())
    jq.run_daily(send_weekly_report, time=datetime.strptime("09:00", "%H:%M").time(), days=(6,))
    logger.info("DrWise is running! 🧠")
    app.run_polling()


if __name__ == "__main__":
    main()
