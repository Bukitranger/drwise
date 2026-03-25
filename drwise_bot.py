"""
DrWise — Personal Health Coach Telegram Bot
Combines Apple Health, Oura, Withings + FatMaster meal data
to give personalized daily/weekly health advice.

Environment variables needed:
  TELEGRAM_BOT_TOKEN   — from @BotFather
  ANTHROPIC_API_KEY    — from console.anthropic.com
  FATMASTER_CHAT_ID    — your Telegram user ID (bot will DM you)
"""

import os
import json
import logging
import asyncio
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
import anthropic

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Clients ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Your Telegram user ID — DrWise will send proactive messages here
# Set this after first /start (bot will tell you your ID)
MY_CHAT_ID = os.environ.get("MY_CHAT_ID")

# ── Data storage (JSON files on Railway volume) ───────────────────────────────
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

HEALTH_FILE  = DATA_DIR / "health_data.json"
MEALS_FILE   = DATA_DIR / "meals.json"
PROFILE_FILE = DATA_DIR / "profile.json"


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2, default=str))


# ── Health data helpers ───────────────────────────────────────────────────────

def save_health_snapshot(payload: dict):
    """Store incoming Health Auto Export webhook data."""
    data = load_json(HEALTH_FILE)
    today = str(date.today())
    if today not in data:
        data[today] = {}
    data[today].update(payload)
    # Keep only last 90 days
    cutoff = str(date.today() - timedelta(days=90))
    data = {k: v for k, v in data.items() if k >= cutoff}
    save_json(HEALTH_FILE, data)


def get_recent_health(days: int = 7) -> dict:
    """Return health data for the last N days."""
    data = load_json(HEALTH_FILE)
    cutoff = str(date.today() - timedelta(days=days))
    return {k: v for k, v in data.items() if k >= cutoff}


def save_meal(meal: dict):
    """Log a meal entry (can be called from fatmaster webhook or manually)."""
    data = load_json(MEALS_FILE)
    today = str(date.today())
    if today not in data:
        data[today] = []
    data[today].append({**meal, "time": datetime.now().isoformat()})
    # Keep last 90 days
    cutoff = str(date.today() - timedelta(days=90))
    data = {k: v for k, v in data.items() if k >= cutoff}
    save_json(MEALS_FILE, data)


def get_recent_meals(days: int = 7) -> dict:
    data = load_json(MEALS_FILE)
    cutoff = str(date.today() - timedelta(days=days))
    return {k: v for k, v in data.items() if k >= cutoff}


def get_today_meals() -> list:
    data = load_json(MEALS_FILE)
    return data.get(str(date.today()), [])


def get_today_health() -> dict:
    data = load_json(HEALTH_FILE)
    return data.get(str(date.today()), {})


# ── Claude AI helpers ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are DrWise, a personal health coach and friend. 
You talk casually, like a smart friend who happens to know a lot about health, nutrition, fitness and recovery.
No corporate speak, no excessive disclaimers. Be direct, warm, and practical.
Your user's main goal is to LOSE WEIGHT while building healthy habits.
You have access to their meal logs, sleep data, activity, heart rate, and body metrics.
Always personalize advice based on the actual data you're given.
Keep responses concise — this is Telegram, not a medical report.
Use emojis naturally. Max 200 words unless doing a weekly report."""


def ask_claude(user_message: str, context_data: Optional[dict] = None) -> str:
    context = ""
    if context_data:
        context = f"\n\nHere's the user's current health data:\n{json.dumps(context_data, indent=2, default=str)}"

    response = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message + context}]
    )
    return response.content[0].text


# ── Report generators ─────────────────────────────────────────────────────────

def build_daily_briefing() -> str:
    health = get_today_health()
    meals_yesterday = get_recent_meals(1)
    
    context = {
        "today_health": health,
        "yesterday_meals": meals_yesterday,
        "goal": "lose weight"
    }
    
    prompt = (
        "Give me my morning health briefing. "
        "Look at last night's sleep, yesterday's meals and activity. "
        "Tell me how recovered I am, what to focus on today, "
        "and one specific nutrition tip for today based on my data. "
        "Keep it punchy — morning energy, not an essay."
    )
    return ask_claude(prompt, context)


def build_weekly_report() -> str:
    health = get_recent_health(7)
    meals = get_recent_meals(7)
    
    context = {
        "week_health": health,
        "week_meals": meals,
        "goal": "lose weight"
    }
    
    prompt = (
        "Give me my weekly health report. "
        "Analyze my sleep patterns, nutrition trends, activity levels, and any body metric changes. "
        "What went well? What needs work? "
        "Give me 3 specific actionable goals for next week. "
        "This is the weekly deep-dive so you can be a bit more detailed — but still keep it friendly."
    )
    return ask_claude(prompt, context)


def build_meal_reaction(meal: dict) -> str:
    today_meals = get_today_meals()
    today_health = get_today_health()
    
    # Calculate today's running totals
    total_cal = sum(m.get("calories", 0) for m in today_meals)
    total_protein = sum(m.get("protein", 0) for m in today_meals)
    
    context = {
        "just_logged": meal,
        "today_meals_so_far": today_meals,
        "today_total_calories": total_cal,
        "today_total_protein": total_protein,
        "today_health": today_health,
        "goal": "lose weight"
    }
    
    prompt = (
        "I just logged a meal. React to it in the context of my whole day so far. "
        "Am I on track for my weight loss goal? "
        "Should I adjust my next meal? Any quick tip? Keep it to 2-3 sentences."
    )
    return ask_claude(prompt, context)


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"Hey {name}! 👋 I'm DrWise, your personal health coach.\n\n"
        f"Your Telegram ID is: `{uid}`\n"
        f"_(Save this — you'll need it for setup)_\n\n"
        f"Here's what I can do:\n"
        f"• Analyze your sleep, activity & nutrition together\n"
        f"• Send you a morning briefing every day\n"
        f"• React to your meals from FatMaster\n"
        f"• Give you a deep weekly report every Sunday\n"
        f"• Coach your gym routine based on your recovery\n\n"
        f"Commands:\n"
        f"/briefing — get your morning summary now\n"
        f"/weekly — full week report\n"
        f"/today — today's stats\n"
        f"/status — check what data I have\n"
        f"/help — show this message",
        parse_mode="Markdown"
    )


async def briefing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Pulling your data... 🔍")
    report = build_daily_briefing()
    await update.message.reply_text(report)


async def weekly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Crunching your whole week... this'll take a sec 📊")
    report = build_weekly_report()
    await update.message.reply_text(report)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    meals = get_today_meals()
    health = get_today_health()

    total_cal = sum(m.get("calories", 0) for m in meals)
    total_protein = sum(m.get("protein", 0) for m in meals)
    total_carbs = sum(m.get("carbs", 0) for m in meals)
    total_fat = sum(m.get("fat", 0) for m in meals)

    lines = ["📅 *Today so far*\n"]

    # Health metrics
    if health:
        if "sleep_hours" in health:
            lines.append(f"😴 Sleep: {health['sleep_hours']}h")
        if "hrv" in health:
            lines.append(f"💓 HRV: {health['hrv']}")
        if "readiness" in health:
            lines.append(f"⚡️ Readiness: {health['readiness']}/100")
        if "steps" in health:
            lines.append(f"👟 Steps: {health['steps']:,}")
        if "weight_kg" in health:
            lines.append(f"⚖️ Weight: {health['weight_kg']} kg")
        lines.append("")

    # Meals
    if meals:
        lines.append(f"🍽 Meals logged: {len(meals)}")
        lines.append(f"🔥 Calories: {total_cal} kcal")
        lines.append(f"💪 Protein: {total_protein:.0f}g")
        lines.append(f"🍞 Carbs: {total_carbs:.0f}g")
        lines.append(f"🥑 Fat: {total_fat:.0f}g")
    else:
        lines.append("🍽 No meals logged yet today")
        lines.append("_(Send a photo to FatMaster to log meals)_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    health_data = load_json(HEALTH_FILE)
    meal_data = load_json(MEALS_FILE)

    health_days = len(health_data)
    meal_days = len(meal_data)
    last_health = max(health_data.keys()) if health_data else "never"
    last_meal = max(meal_data.keys()) if meal_data else "never"

    await update.message.reply_text(
        f"📡 *DrWise Status*\n\n"
        f"Health data: {health_days} days stored\n"
        f"Last health sync: {last_health}\n\n"
        f"Meal data: {meal_days} days stored\n"
        f"Last meal logged: {last_meal}\n\n"
        f"{'✅ Health Auto Export connected' if health_days > 0 else '⚠️ No health data yet — set up Health Auto Export'}",
        parse_mode="Markdown"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Let users chat naturally with DrWise."""
    user_msg = update.message.text
    health = get_recent_health(3)
    meals = get_recent_meals(3)

    ctx = {"recent_health": health, "recent_meals": meals, "goal": "lose weight"}
    reply = ask_claude(user_msg, ctx)
    await update.message.reply_text(reply)


# ── Webhook endpoint for Health Auto Export ───────────────────────────────────
# Railway will expose this via the web process
# We use a simple HTTP server alongside the Telegram bot

from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import urllib.parse


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default HTTP logs

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            payload = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        # ── /health — from Health Auto Export ──
        if path == "/health":
            self._handle_health(payload)

        # ── /meal — from FatMaster bot ──
        elif path == "/meal":
            self._handle_meal(payload)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def _handle_health(self, payload: dict):
        """Process incoming Apple Health / Oura / Withings data."""
        # Health Auto Export sends nested metrics — flatten what we need
        normalized = {}

        # Sleep
        if "sleep_analysis" in payload:
            s = payload["sleep_analysis"]
            normalized["sleep_hours"] = s.get("asleep_hours") or s.get("inBed_hours")
            normalized["sleep_start"] = s.get("start")
            normalized["sleep_end"] = s.get("end")

        # Heart rate & HRV
        if "heart_rate_variability" in payload:
            normalized["hrv"] = payload["heart_rate_variability"].get("avg")
        if "resting_heart_rate" in payload:
            normalized["resting_hr"] = payload["resting_heart_rate"].get("avg")

        # Activity
        if "step_count" in payload:
            normalized["steps"] = payload["step_count"].get("sum") or payload["step_count"].get("qty")
        if "active_energy" in payload:
            normalized["active_calories"] = payload["active_energy"].get("sum")

        # Body metrics (Withings)
        if "body_mass" in payload:
            normalized["weight_kg"] = payload["body_mass"].get("avg") or payload["body_mass"].get("qty")
        if "body_fat_percentage" in payload:
            normalized["body_fat_pct"] = payload["body_fat_percentage"].get("avg")

        # Oura readiness (comes through Apple Health or direct)
        if "apple_exercise_time" in payload:
            normalized["exercise_minutes"] = payload["apple_exercise_time"].get("sum")

        # Store raw payload too so we don't lose anything
        normalized["_raw"] = payload

        save_health_snapshot(normalized)
        logger.info(f"Health snapshot saved: {list(normalized.keys())}")

    def _handle_meal(self, payload: dict):
        """Process meal logged by FatMaster."""
        save_meal(payload)
        logger.info(f"Meal received from FatMaster: {payload.get('meal', 'unknown')}")

        # Send real-time reaction via Telegram if we have a chat ID
        if MY_CHAT_ID:
            reaction = build_meal_reaction(payload)
            # Schedule async send
            asyncio.run_coroutine_threadsafe(
                _send_message(int(MY_CHAT_ID), reaction),
                bot_loop
            )

    def do_GET(self):
        """Health check endpoint."""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"DrWise is alive!")


bot_app = None
bot_loop = None


async def _send_message(chat_id: int, text: str):
    if bot_app:
        await bot_app.bot.send_message(chat_id=chat_id, text=text)


def run_webhook_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    logger.info(f"Webhook server listening on port {port}")
    server.serve_forever()


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def send_daily_briefing(context: ContextTypes.DEFAULT_TYPE):
    if not MY_CHAT_ID:
        return
    report = build_daily_briefing()
    await context.bot.send_message(chat_id=int(MY_CHAT_ID), text=f"☀️ *Morning Briefing*\n\n{report}", parse_mode="Markdown")


async def send_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    if not MY_CHAT_ID:
        return
    report = build_weekly_report()
    await context.bot.send_message(chat_id=int(MY_CHAT_ID), text=f"📊 *Weekly Report*\n\n{report}", parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global bot_app, bot_loop

    # Start webhook server in background thread
    webhook_thread = threading.Thread(target=run_webhook_server, daemon=True)
    webhook_thread.start()

    # Build Telegram bot
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    bot_app = app

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("briefing", briefing_cmd))
    app.add_handler(CommandHandler("weekly", weekly_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Schedule daily briefing at 8:00 AM and weekly report on Sundays
    job_queue = app.job_queue
    job_queue.run_daily(send_daily_briefing, time=datetime.strptime("08:00", "%H:%M").time())
    job_queue.run_daily(
        send_weekly_report,
        time=datetime.strptime("09:00", "%H:%M").time(),
        days=(6,)  # Sunday
    )

    logger.info("DrWise is running! 🧠")

    # Store event loop reference for cross-thread messaging
    bot_loop = asyncio.get_event_loop()
    app.run_polling()


if __name__ == "__main__":
    main()
