"""
DrWise — Personal Health Coach Telegram Bot
Reads health data from Supabase (written by HAE via Edge Function).
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


def get_health_records(days=7):
    cutoff = str(date.today() - timedelta(days=days))
    records = sb("GET", "health_data", filters={
        "recorded_date": f"gte.{cutoff}",
        "order": "recorded_date",
        "select": "recorded_date,data"
    })
    return records or []


def get_today_health_raw():
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


# ── Health summarizer ─────────────────────────────────────────────────────────
# Extracts clean latest values from HAE's nested metric structure
# HAE format: { "metric_name": { "units": "kg", "data": [{"qty": 82.1, "date": "..."}, ...] } }

# Map HAE metric names → friendly labels and units
METRIC_MAP = {
    "heart_rate":                    ("Heart Rate", "bpm"),
    "resting_heart_rate":            ("Resting HR", "bpm"),
    "heart_rate_variability_sdnn":   ("HRV", "ms"),
    "walking_heart_rate_average":    ("Walking HR avg", "bpm"),
    "heart_rate_recovery_one_minute":("HR Recovery", "bpm"),
    "step_count":                    ("Steps", "steps"),
    "distance_walking_running":      ("Distance", "km"),
    "active_energy_burned":          ("Active Calories", "kcal"),
    "basal_energy_burned":           ("Basal Calories", "kcal"),
    "apple_exercise_time":           ("Exercise Time", "min"),
    "apple_stand_time":              ("Stand Time", "min"),
    "flights_climbed":               ("Flights Climbed", "floors"),
    "vo2_max":                       ("VO2 Max", "ml/kg/min"),
    "weight_body_mass":              ("Weight", "kg"),
    "body_mass_index":               ("BMI", ""),
    "body_fat_percentage":           ("Body Fat", "%"),
    "lean_body_mass":                ("Lean Mass", "kg"),
    "sleep_analysis":                ("Sleep", "hr"),
    "respiratory_rate":              ("Respiratory Rate", "breaths/min"),
    "oxygen_saturation":             ("Blood Oxygen", "%"),
    "walking_speed":                 ("Walking Speed", "km/h"),
    "walking_step_length":           ("Step Length", "cm"),
    "walking_asymmetry_percentage":  ("Walking Asymmetry", "%"),
    "walking_double_support_percentage": ("Double Support", "%"),
    "apple_walking_steadiness":      ("Walking Steadiness", "%"),
    "time_in_daylight":              ("Daylight Time", "min"),
    "physical_effort":               ("Physical Effort", "MET"),
    "environmental_audio_exposure":  ("Env Audio", "dB"),
    "headphone_audio_exposure":      ("Headphone Audio", "dB"),
}


def extract_latest_value(metric_data):
    """Get the most recent value from a HAE metric object."""
    if not isinstance(metric_data, dict):
        return None
    data_arr = metric_data.get("data", [])
    if not data_arr or not isinstance(data_arr, list):
        return None
    # Get last entry
    last = data_arr[-1]
    if isinstance(last, dict):
        return last.get("qty") or last.get("value") or last.get("inBed") or last.get("asleep")
    return None


# Metrics Apple Health stores in lbs — convert to kg
LBS_TO_KG = {"Weight", "Lean Mass"}
# Metrics stored in meters — convert to cm
M_TO_CM = {"Step Length"}

def summarize_health(raw: dict) -> dict:
    """Convert raw HAE metric dict into clean summary dict."""
    summary = {}
    for key, (label, unit) in METRIC_MAP.items():
        if key in raw:
            val = extract_latest_value(raw[key])
            if val is not None:
                try:
                    val = round(float(val), 2)
                    if label in LBS_TO_KG:
                        val = round(val * 0.453592, 1)
                    elif label in M_TO_CM:
                        val = round(val * 100, 1)
                    summary[label] = f"{val} {unit}".strip()
                except:
                    summary[label] = str(val)
    return summary


def summarize_health_week(records) -> dict:
    """Summarize a week of health records into daily snapshots."""
    result = {}
    for r in records:
        day = r["recorded_date"]
        raw = r.get("data", {})
        result[day] = summarize_health(raw)
    return result


# ── Claude AI ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are DrWise, a personal health coach and friend.
Talk casually, like a smart friend who knows a lot about health, nutrition, fitness and recovery.
No corporate speak, no excessive disclaimers. Be direct, warm, and practical.
User's main goal: LOSE WEIGHT while building healthy habits.
You have access to meal logs and summarized health metrics from Apple Health, Oura Ring and Withings scale.
Always personalize advice based on the actual data. Keep responses concise — this is Telegram.
Use emojis naturally. Max 200 words unless doing a weekly report.

IMPORTANT USER PREFERENCES:
- All weight in KILOGRAMS (kg). Never lbs.
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
    today_raw = get_today_health_raw()
    today_summary = summarize_health(today_raw)
    meals = get_recent_meals(1)
    return ask_claude(
        "Give me my morning health briefing. Look at my health metrics and yesterday's meals. "
        "How recovered am I? What to focus on today? One specific nutrition tip. Punchy, not an essay.",
        {"today_health": today_summary, "yesterday_meals": meals, "goal": "lose weight"}
    )


def build_weekly_report():
    records = get_health_records(7)
    week_summary = summarize_health_week(records)
    meals = get_recent_meals(7)
    return ask_claude(
        "Give me my weekly health report. Analyze sleep patterns, nutrition trends, activity levels, "
        "body metric changes. What went well? What needs work? 3 specific goals for next week.",
        {"week_health": week_summary, "week_meals": meals, "goal": "lose weight"}
    )


def build_meal_reaction(meal):
    today_meals = get_today_meals()
    today_summary = summarize_health(get_today_health_raw())
    return ask_claude(
        "I just logged a meal. React to it in context of my whole day. "
        "Am I on track for weight loss? Should I adjust my next meal? 2-3 sentences.",
        {
            "just_logged": meal,
            "today_meals_so_far": today_meals,
            "today_total_calories": sum(m.get("calories", 0) for m in today_meals),
            "today_total_protein": sum(m.get("protein", 0) for m in today_meals),
            "today_health": today_summary,
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
    today_raw = get_today_health_raw()
    summary = summarize_health(today_raw)
    lines = ["📅 Today so far\n"]
    if summary:
        for label, val in summary.items():
            lines.append(f"• {label}: {val}")
        lines.append("")
    else:
        lines.append("📊 No health data yet today\n")
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
    records = get_health_records(90)
    meals = get_recent_meals(90)
    last_health = records[-1]["recorded_date"] if records else "never"
    last_meal = max(meals.keys()) if meals else "never"
    today_raw = get_today_health_raw()
    metric_count = len([k for k in today_raw if k in METRIC_MAP])
    await update.message.reply_text(
        f"📡 DrWise Status\n\n"
        f"Health data: {len(records)} days in Supabase\n"
        f"Last sync: {last_health}\n"
        f"Metrics today: {metric_count}/{len(METRIC_MAP)} tracked\n\n"
        f"Meal data: {len(meals)} days in Supabase\n"
        f"Last meal: {last_meal}\n\n"
        f"Storage: Supabase ✅ (persistent)"
    )


async def handle_text(update, context):
    today_summary = summarize_health(get_today_health_raw())
    ctx = {
        "today_health": today_summary,
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
