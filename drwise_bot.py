"""
DrWise — Personal Health Coach Telegram Bot
- Reads health data from Supabase (written by HAE via hyper-processor Edge Function)
- Reads meals from Supabase (written by FatMaster)
- Persistent user profile memory via /remember command
- Smart health metric aggregation with dynamic unit conversion

CHANGELOG (2026-04-24):
  * METRIC_MAP key names switched from iOS HealthKit IDs to HAE export names
    (active_energy, heart_rate_variability, blood_oxygen_saturation, apple_stand_hour).
  * Sleep aggregation now handles BOTH HAE payload shapes (Apple segmented + Oura summary).
  * Unit conversion reads the `units` field from each metric at runtime
    (fixes miles→km for distance and inches→cm for step length).
  * "Today" now uses Tokyo time (Asia/Tokyo) instead of server UTC.
  * Added a few extra metrics (apple_stand_hour, environmental_audio_exposure,
    time_in_daylight, apple_walking_steadiness, physical_effort) so they flow
    through to Claude when present.
"""

import os
import json
import logging
import threading
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
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

TOKYO = ZoneInfo("Asia/Tokyo")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
message_queue: Queue = Queue()


def today_jst() -> date:
    """Current date in Tokyo — server runs in UTC so date.today() is wrong for the user."""
    return datetime.now(TOKYO).date()


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


# ── User profile (persistent memory) ─────────────────────────────────────────

def get_user_profile() -> str:
    records = sb("GET", "user_profile", filters={"select": "key,value", "order": "updated_at"})
    if not records:
        return ""
    return "\n".join(f"- {r['key']}: {r['value']}" for r in records)


def save_profile_note(key: str, value: str):
    existing = sb("GET", "user_profile", filters={"key": f"eq.{key}", "select": "id"})
    if existing and len(existing) > 0:
        sb("PATCH", f"user_profile?key=eq.{key}",
           data={"value": value, "updated_at": datetime.now().isoformat()})
    else:
        sb("POST", "user_profile", data={"key": key, "value": value})


def delete_profile_note(key: str):
    sb("DELETE", f"user_profile?key=eq.{key}", filters=None)


# ── Health data ───────────────────────────────────────────────────────────────

def get_health_records(days=30):
    cutoff = str(today_jst() - timedelta(days=days))
    records = sb("GET", "health_data", filters={
        "recorded_date": f"gte.{cutoff}",
        "order": "recorded_date",
        "select": "recorded_date,data"
    })
    return records or []


def get_today_health_raw():
    records = sb("GET", "health_data", filters={
        "recorded_date": f"eq.{today_jst()}",
        "select": "data"
    })
    if records and len(records) > 0:
        return records[0]["data"]
    return {}


# ── Meal data ─────────────────────────────────────────────────────────────────

def get_recent_meals(days=7):
    cutoff = str(today_jst() - timedelta(days=days))
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
        result.setdefault(d, []).append(r["data"])
    return result


def get_today_meals():
    records = sb("GET", "meals", filters={
        "recorded_date": f"eq.{today_jst()}",
        "order": "created_at",
        "select": "data"
    })
    return [r["data"] for r in records] if records else []


# ── Health summarizer ─────────────────────────────────────────────────────────
#
# Key names here MUST match what the hyper-processor Edge Function writes.
# HAE uses snake_case short names (e.g. "active_energy"), NOT the iOS
# HealthKit identifiers (e.g. "active_energy_burned"). Previous versions
# of this map used HealthKit names and silently dropped most data.

METRIC_MAP = {
    # Heart & cardiovascular
    "heart_rate":                       ("Heart Rate",          "bpm"),
    "resting_heart_rate":               ("Resting HR",          "bpm"),
    "heart_rate_variability":           ("HRV",                 "ms"),
    "walking_heart_rate_average":       ("Walking HR avg",      "bpm"),
    "heart_rate_recovery_one_minute":   ("HR Recovery",         "bpm"),

    # Activity
    "step_count":                       ("Steps",               "steps"),
    "walking_running_distance":         ("Distance",            "km"),
    "active_energy":                    ("Active Calories",     "kcal"),
    "basal_energy_burned":              ("Basal Calories",      "kcal"),
    "apple_exercise_time":              ("Exercise Time",       "min"),
    "apple_stand_time":                 ("Stand Time",          "min"),
    "apple_stand_hour":                 ("Stand Hours",         "hr"),
    "flights_climbed":                  ("Flights Climbed",     "floors"),
    "vo2_max":                          ("VO2 Max",             "ml/kg·min"),

    # Body composition
    "weight_body_mass":                 ("Weight",              "kg"),
    "body_mass_index":                  ("BMI",                 ""),
    "body_fat_percentage":              ("Body Fat",            "%"),
    "lean_body_mass":                   ("Lean Mass",           "kg"),

    # Recovery & respiration
    "sleep_analysis":                   ("Sleep",               "hr"),
    "respiratory_rate":                 ("Respiratory Rate",    "breaths/min"),
    "blood_oxygen_saturation":          ("Blood Oxygen",        "%"),

    # Gait quality
    "walking_speed":                    ("Walking Speed",       "km/h"),
    "walking_step_length":              ("Step Length",         "cm"),
    "walking_asymmetry_percentage":     ("Walking Asymmetry",   "%"),
    "walking_double_support_percentage":("Double Support",      "%"),
    "apple_walking_steadiness":         ("Walking Steadiness",  "%"),

    # Environment / lifestyle
    "time_in_daylight":                 ("Daylight Time",       "min"),
    "physical_effort":                  ("Physical Effort",     "MET"),
    "environmental_audio_exposure":     ("Env Audio",           "dB"),
    "headphone_audio_exposure":         ("Headphone Audio",     "dB"),
}

# How to roll up intra-day readings into a single number.
SUM_METRICS = {
    "Steps", "Active Calories", "Basal Calories",
    "Exercise Time", "Stand Time", "Stand Hours",
    "Flights Climbed", "Distance", "Daylight Time",
}
LATEST_METRICS = {
    "Weight", "BMI", "Body Fat", "Lean Mass",
    "VO2 Max", "Walking Steadiness",
}
# Everything else is averaged.


# ── Unit conversion ──────────────────────────────────────────────────────────
#
# HAE emits whatever units the iPhone's region is set to. A US-locale phone
# sends weight in lb, distance in mi, and step length in in. We canonicalize
# to the display units declared in METRIC_MAP.

# Pairs where the source and target mean the same thing but are spelled
# differently in HAE vs. what we want to show. Value is unchanged.
UNIT_ALIASES = {
    ("count/min", "bpm"),
    ("count", "steps"),
    ("count", "floors"),
    ("count", "hr"),            # e.g. apple_stand_hour units="count"
    ("count", ""),              # e.g. BMI units="count"
    ("count", "ml/kg·min"),     # guarded by metric label but safe
    ("dbaspl", "db"),
    ("ml/(kg·min)", "ml/kg·min"),
}


def convert_to_display_unit(value: float, source_unit: str | None, target_unit: str) -> float:
    """Convert `value` from HAE's source unit to the unit DrWise wants to show."""
    if not source_unit:
        return value
    s = source_unit.strip().lower()
    t = (target_unit or "").strip().lower()
    if s == t:
        return value
    if (s, t) in UNIT_ALIASES:
        return value

    # Weight / mass
    if s in ("lb", "lbs") and t == "kg":
        return value * 0.453592
    if s == "kg" and t in ("lb", "lbs"):
        return value / 0.453592

    # Distance
    if s in ("mi", "mile", "miles") and t == "km":
        return value * 1.609344
    if s == "km" and t in ("mi", "mile", "miles"):
        return value / 1.609344
    if s == "m" and t == "km":
        return value / 1000.0

    # Short length
    if s in ("in", "inch", "inches") and t == "cm":
        return value * 2.54
    if s == "m" and t == "cm":
        return value * 100.0
    if s == "mm" and t == "cm":
        return value / 10.0

    # Speed
    if s in ("mi/hr", "mph") and t == "km/h":
        return value * 1.609344
    if s == "m/s" and t == "km/h":
        return value * 3.6

    # No recognized conversion → return as-is; we'll show the raw source unit.
    return value


def should_convert(source_unit: str | None, target_unit: str) -> bool:
    """True if we can safely display this reading using the target unit label —
    either because a conversion exists, or because the units are equivalent."""
    if not source_unit:
        # No unit in payload → trust METRIC_MAP's label.
        return True
    s = source_unit.strip().lower()
    t = (target_unit or "").strip().lower()
    if s == t:
        return True
    if (s, t) in UNIT_ALIASES:
        return True
    pairs = {
        ("lb", "kg"), ("lbs", "kg"),
        ("kg", "lb"), ("kg", "lbs"),
        ("mi", "km"), ("mile", "km"), ("miles", "km"),
        ("km", "mi"), ("km", "mile"), ("km", "miles"),
        ("m", "km"),
        ("in", "cm"), ("inch", "cm"), ("inches", "cm"),
        ("m", "cm"), ("mm", "cm"),
        ("mi/hr", "km/h"), ("mph", "km/h"), ("m/s", "km/h"),
    }
    return (s, t) in pairs


# ── Sleep (special-cased because HAE sends it in two different shapes) ───────

def extract_sleep_hours(metric_data: dict) -> float | None:
    """HAE sleep comes in two formats:
       A) Apple segmented — many rows, each with `qty` = hours of that segment
          and `value` = stage ("Core", "Deep", "REM", "Awake", "In Bed").
          → sum the `qty` where stage is an actual sleep stage.
       B) Oura / Withings summary — one row with `totalSleep` / `asleep`
          / `inBed` pre-aggregated in hours.
          → prefer `totalSleep`, fall back to `asleep`, then `inBed`.
    """
    if not isinstance(metric_data, dict):
        return None
    rows = metric_data.get("data") or []
    if not isinstance(rows, list) or not rows:
        return None

    # Shape B: single summary row
    if len(rows) == 1 and isinstance(rows[0], dict):
        r = rows[0]
        for key in ("totalSleep", "asleep", "inBed"):
            v = r.get(key)
            if v is not None:
                try:
                    fv = float(v)
                    if fv > 0:
                        return round(fv, 2)
                except (TypeError, ValueError):
                    pass
        # Fall through to shape-A handling in case it's segmented despite length 1.

    # Shape A: segmented rows
    asleep_stages = {"core", "deep", "rem", "asleep"}
    total = 0.0
    saw_any = False
    for r in rows:
        if not isinstance(r, dict):
            continue
        stage = str(r.get("value", "")).strip().lower()
        qty = r.get("qty")
        if qty is None:
            continue
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            continue
        saw_any = True
        if stage in asleep_stages:
            total += qty
    if not saw_any:
        return None
    return round(total, 2)


# ── Generic metric extraction ────────────────────────────────────────────────

def extract_summary_value(metric_data: dict, label: str) -> float | None:
    """Aggregate per-metric readings into a single number.
       - SUM_METRICS: total for the day
       - LATEST_METRICS: latest reading
       - else: average
    """
    if not isinstance(metric_data, dict):
        return None
    data_arr = metric_data.get("data") or []
    if not isinstance(data_arr, list) or not data_arr:
        return None

    values = []
    for entry in data_arr:
        if not isinstance(entry, dict):
            continue
        val = (entry.get("qty")
               or entry.get("Avg")
               or entry.get("value"))
        if val is None:
            continue
        try:
            values.append(float(val))
        except (TypeError, ValueError):
            pass

    if not values:
        return None

    if label in SUM_METRICS:
        return round(sum(values), 2)
    if label in LATEST_METRICS:
        return round(values[-1], 2)
    return round(sum(values) / len(values), 2)


def summarize_health(raw: dict) -> dict:
    """Pull a compact, Claude-ready summary out of a day's raw HAE payload."""
    if not isinstance(raw, dict):
        return {}
    summary = {}
    for key, (label, display_unit) in METRIC_MAP.items():
        if key not in raw:
            continue
        metric_payload = raw[key]
        source_unit = metric_payload.get("units") if isinstance(metric_payload, dict) else None

        # Sleep needs a custom aggregator.
        if key == "sleep_analysis":
            hours = extract_sleep_hours(metric_payload)
            if hours is not None and hours > 0:
                summary[label] = f"{hours} hr"
            continue

        val = extract_summary_value(metric_payload, label)
        if val is None:
            continue

        # Convert to display unit if we recognize the source unit.
        if should_convert(source_unit, display_unit):
            val = convert_to_display_unit(val, source_unit, display_unit)
            shown_unit = display_unit
        else:
            shown_unit = source_unit or display_unit or ""

        val = round(float(val), 2)
        summary[label] = f"{val} {shown_unit}".strip()

    return summary


def summarize_health_week(records) -> dict:
    return {r["recorded_date"]: summarize_health(r.get("data", {})) for r in records}


# ── Claude AI ─────────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are DrWise, a personal health coach and friend.
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


def get_system_prompt() -> str:
    profile = get_user_profile()
    if profile:
        return BASE_SYSTEM_PROMPT + f"\n\nPERSONAL NOTES ABOUT THIS USER (always respect these):\n{profile}"
    return BASE_SYSTEM_PROMPT


def ask_claude(user_message, context_data=None):
    context = ""
    if context_data:
        context = f"\n\nUser's health data:\n{json.dumps(context_data, default=str)}"
    response = anthropic_client.messages.create(
        model="claude-opus-4-5", max_tokens=1000,
        system=get_system_prompt(),
        messages=[{"role": "user", "content": user_message + context}]
    )
    return response.content[0].text


def build_daily_briefing():
    records = get_health_records(2)
    health_summary = summarize_health_week(records)
    meals = get_recent_meals(1)
    health_str = json.dumps(health_summary, default=str)[:4000]
    meals_str = json.dumps(meals, default=str)[:2000]
    return ask_claude(
        "Give me my morning health briefing. Look at my recent health metrics and yesterday's meals. "
        "How recovered am I? What to focus on today? One specific nutrition tip. Punchy, not an essay.",
        {"health": health_str, "yesterday_meals": meals_str, "goal": "lose weight"}
    )


def build_weekly_report():
    records = get_health_records(30)
    health_summary = summarize_health_week(records)
    meals = get_recent_meals(30)
    health_str = json.dumps(health_summary, default=str)[:5000]
    meals_str = json.dumps(meals, default=str)[:3000]
    return ask_claude(
        "Give me my monthly health report. Analyze sleep patterns, nutrition trends, activity levels, "
        "body metric changes over the past 30 days. What are the key trends? What needs work? "
        "3 specific goals for the coming weeks.",
        {"month_health": health_str, "month_meals": meals_str, "goal": "lose weight"}
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
        f"/weekly — monthly report\n"
        f"/today — today's stats\n"
        f"/status — data status\n"
        f"/remember [note] — save something important\n"
        f"/memories — see what I remember\n"
        f"/forget [key] — remove a memory\n\n"
        f"Or just chat with me! 💬"
    )


async def briefing_cmd(update, context):
    await update.message.reply_text("Pulling your data... 🔍")
    await update.message.reply_text(build_daily_briefing())


async def weekly_cmd(update, context):
    await update.message.reply_text("Crunching your last 30 days... 📊")
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
    profile = get_user_profile()

    # Also report which metrics are coming through today — makes data issues obvious.
    today_raw = get_today_health_raw()
    today_metrics = sorted(today_raw.keys()) if isinstance(today_raw, dict) else []

    await update.message.reply_text(
        f"📡 DrWise Status\n\n"
        f"Health data: {len(records)} days in Supabase\n"
        f"Last sync: {last_health}\n"
        f"Today metrics ({len(today_metrics)}): {', '.join(today_metrics) if today_metrics else 'none yet'}\n\n"
        f"Meal data: {len(meals)} days in Supabase\n"
        f"Last meal: {last_meal}\n\n"
        f"Memories: {len(profile.splitlines()) if profile else 0} notes\n\n"
        f"Storage: Supabase ✅ (persistent)"
    )


async def remember_cmd(update, context):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text(
            "Usage: /remember [something important]\n"
            "Example: /remember I had surgery and can't do high step counts yet"
        )
        return
    key = f"note_{datetime.now(TOKYO).strftime('%Y%m%d_%H%M%S')}"
    save_profile_note(key, text)
    await update.message.reply_text(f"✅ Got it, I'll remember that:\n\"{text}\"")


async def memories_cmd(update, context):
    records = sb("GET", "user_profile", filters={"select": "key,value,updated_at", "order": "updated_at"})
    if not records:
        await update.message.reply_text("No memories saved yet.\nUse /remember to add notes!")
        return
    lines = ["🧠 What I remember about you:\n"]
    for r in records:
        lines.append(f"• [{r['key']}] {r['value']}")
    await update.message.reply_text("\n".join(lines))


async def forget_cmd(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /forget [key]\nUse /memories to see keys.")
        return
    key = context.args[0]
    delete_profile_note(key)
    await update.message.reply_text(f"🗑 Removed memory: {key}")


async def handle_text(update, context):
    records = get_health_records(30)
    health_summary = summarize_health_week(records)
    today_meals = get_today_meals()
    recent_meals = get_recent_meals(7)

    health_str = json.dumps(health_summary, default=str)
    if len(health_str) > 4000:
        health_str = health_str[:4000] + "... [truncated]"

    recent_meals_str = json.dumps(recent_meals, default=str)
    if len(recent_meals_str) > 2000:
        recent_meals_str = recent_meals_str[:2000] + "... [truncated]"

    ctx = {
        "health_last_30_days": health_str,
        "todays_meals": today_meals,  # always full, never truncated
        "recent_meals_history": recent_meals_str,
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
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        if path == "/meal":
            # FatMaster already saved to Supabase — just react
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
            text=f"📊 Monthly Report\n\n{build_weekly_report()}"
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
    app.add_handler(CommandHandler("remember", remember_cmd))
    app.add_handler(CommandHandler("memories", memories_cmd))
    app.add_handler(CommandHandler("forget", forget_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    jq = app.job_queue
    jq.run_repeating(drain_message_queue, interval=5, first=5)
    # Schedule in Tokyo time so 08:00 means 08:00 JST, not 08:00 UTC.
    jq.run_daily(send_daily_briefing,
                 time=datetime.strptime("08:00", "%H:%M").time().replace(tzinfo=TOKYO))
    jq.run_daily(send_weekly_report,
                 time=datetime.strptime("09:00", "%H:%M").time().replace(tzinfo=TOKYO),
                 days=(6,))
    logger.info("DrWise is running! 🧠")
    app.run_polling()


if __name__ == "__main__":
    main()
