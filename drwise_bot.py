"""
DrWise — Personal Health Coach Telegram Bot
Fixed version with reliable meal reaction using message queue.
"""

import os
import json
import logging
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
from http.server import HTTPServer, BaseHTTPRequestHandler
from queue import Queue

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import anthropic

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MY_CHAT_ID = os.environ.get("MY_CHAT_ID")

message_queue: Queue = Queue()

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
HEALTH_FILE = DATA_DIR / "health_data.json"
MEALS_FILE  = DATA_DIR / "meals.json"

def load_json(path):
    if path.exists():
        try: return json.loads(path.read_text())
        except: return {}
    return {}

def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str))

def save_health_snapshot(payload):
    data = load_json(HEALTH_FILE)
    today = str(date.today())
    if today not in data: data[today] = {}
    data[today].update(payload)
    cutoff = str(date.today() - timedelta(days=90))
    save_json(HEALTH_FILE, {k: v for k, v in data.items() if k >= cutoff})

def get_recent_health(days=7):
    data = load_json(HEALTH_FILE)
    cutoff = str(date.today() - timedelta(days=days))
    return {k: v for k, v in data.items() if k >= cutoff}

def save_meal(meal):
    data = load_json(MEALS_FILE)
    today = str(date.today())
    if today not in data: data[today] = []
    data[today].append({**meal, "time": datetime.now().isoformat()})
    cutoff = str(date.today() - timedelta(days=90))
    save_json(MEALS_FILE, {k: v for k, v in data.items() if k >= cutoff})

def get_recent_meals(days=7):
    data = load_json(MEALS_FILE)
    cutoff = str(date.today() - timedelta(days=days))
    return {k: v for k, v in data.items() if k >= cutoff}

def get_today_meals():
    return load_json(MEALS_FILE).get(str(date.today()), [])

def get_today_health():
    return load_json(HEALTH_FILE).get(str(date.today()), {})

SYSTEM_PROMPT = """You are DrWise, a personal health coach and friend.
Talk casually, like a smart friend who knows a lot about health, nutrition, fitness and recovery.
No corporate speak, no excessive disclaimers. Be direct, warm, and practical.
User's main goal: LOSE WEIGHT while building healthy habits.
You have access to meal logs, sleep data, activity, heart rate, and body metrics.
Always personalize advice based on the actual data. Keep responses concise — this is Telegram.
Use emojis naturally. Max 200 words unless doing a weekly report."""

def ask_claude(user_message, context_data=None):
    context = f"\n\nUser's health data:\n{json.dumps(context_data, indent=2, default=str)}" if context_data else ""
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

async def start(update, context):
    uid = update.effective_user.id
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"Hey {name}! 👋 I'm DrWise, your personal health coach.\n\n"
        f"Your Telegram ID is: `{uid}`\n"
        f"_(Add as MY\\_CHAT\\_ID in Railway Variables if not set)_\n\n"
        f"/briefing — morning summary\n/weekly — full week report\n"
        f"/today — today's stats\n/status — data status\n\nOr just chat! 💬",
        parse_mode="Markdown"
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
    lines = ["📅 *Today so far*\n"]
    if health:
        if "sleep_hours" in health: lines.append(f"😴 Sleep: {health['sleep_hours']}h")
        if "hrv" in health:         lines.append(f"💓 HRV: {health['hrv']}")
        if "steps" in health:       lines.append(f"👟 Steps: {int(health['steps']):,}")
        if "weight_kg" in health:   lines.append(f"⚖️ Weight: {health['weight_kg']} kg")
        lines.append("")
    if meals:
        lines += [
            f"🍽 Meals: {len(meals)}",
            f"🔥 Calories: {sum(m.get('calories',0) for m in meals)} kcal",
            f"💪 Protein: {sum(m.get('protein',0) for m in meals):.0f}g",
        ]
    else:
        lines.append("🍽 No meals logged yet\n_(Send a photo to FatMaster!)_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def status_cmd(update, context):
    hd = load_json(HEALTH_FILE)
    md = load_json(MEALS_FILE)
    await update.message.reply_text(
        f"📡 *DrWise Status*\n\n"
        f"Health data: {len(hd)} days\nLast sync: {max(hd.keys()) if hd else 'never'}\n\n"
        f"Meal data: {len(md)} days\nLast meal: {max(md.keys()) if md else 'never'}\n\n"
        f"{'✅ Health Auto Export connected' if hd else '⚠️ No health data yet'}",
        parse_mode="Markdown"
    )

async def handle_text(update, context):
    ctx = {"recent_health": get_recent_health(3), "recent_meals": get_recent_meals(3), "goal": "lose weight"}
    await update.message.reply_text(ask_claude(update.message.text, ctx))

async def drain_message_queue(context):
    while not message_queue.empty():
        try:
            chat_id, text = message_queue.get_nowait()
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.error(f"Queue drain error: {e}")

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"DrWise is alive!")

    def do_POST(self):
        save_health_snapshot(payload)
        logger.info(f"Health saved — keys: {list(payload.keys())}")
        
        try:
            payload = json.loads(self.rfile.read(length))
        except:
            self.send_response(400); self.end_headers(); return


           save_health_snapshot(payload)
        logger.info(f"Health saved — keys: {list(payload.keys())}")

        elif path == "/meal":
            save_meal(payload)
            logger.info(f"Meal from FatMaster: {payload.get('meal', '?')}")
            if MY_CHAT_ID:
                try:
                    reaction = build_meal_reaction(payload)
                    message_queue.put((int(MY_CHAT_ID), reaction))
                    logger.info("Meal reaction queued ✓")
                except Exception as e:
                    logger.error(f"Meal reaction error: {e}")

        self.send_response(200); self.end_headers()
        self.wfile.write(b"ok")

def run_webhook_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), WebhookHandler).serve_forever()

async def send_daily_briefing(context):
    if MY_CHAT_ID:
        await context.bot.send_message(chat_id=int(MY_CHAT_ID), text=f"☀️ *Morning Briefing*\n\n{build_daily_briefing()}", parse_mode="Markdown")

async def send_weekly_report(context):
    if MY_CHAT_ID:
        await context.bot.send_message(chat_id=int(MY_CHAT_ID), text=f"📊 *Weekly Report*\n\n{build_weekly_report()}", parse_mode="Markdown")

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
    jq.run_daily(send_weekly_report,  time=datetime.strptime("09:00", "%H:%M").time(), days=(6,))
    logger.info("DrWise is running! 🧠")
    app.run_polling()

if __name__ == "__main__":
    main()
