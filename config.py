"""Configuration, loaded from environment / .env file.

Values are read defensively: a malformed setting logs a warning and falls back
to its default rather than crash-looping the whole bot. Pasting environment
variables into a hosting dashboard mangles them more often than you'd think.
"""
import logging
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional
    pass

log = logging.getLogger("wordlebot.config")


def _first_token(raw: str | None) -> str:
    """Take the first whitespace-delimited chunk of a value.

    If two variables get pasted onto one line, the value arrives looking like
    "2 SHARED_WINS=true". Everything we read is a single token with no spaces,
    so keeping the first one recovers the intended value.
    """
    if not raw:
        return ""
    cleaned = raw.strip().split()[0] if raw.strip() else ""
    if cleaned != raw.strip():
        log.warning(
            "Value %r looks like it has another variable stuck to it; using %r. "
            "Check that each variable is on its own line in your host's settings.",
            raw, cleaned,
        )
    return cleaned


def _str(name: str, default: str = "") -> str:
    return _first_token(os.getenv(name)) or default


def _int(name: str, default: int) -> int:
    raw = _first_token(os.getenv(name))
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not a whole number; using %s instead.", name, raw, default)
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _first_token(os.getenv(name)).lower()
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    if raw:
        log.warning("%s=%r is not true/false; using %s instead.", name, raw, default)
    return default


# --- Required ---------------------------------------------------------------
TELEGRAM_TOKEN = _str("TELEGRAM_TOKEN")

# --- Optional: screenshot reading via Claude vision --------------------------
# Without a key the bot still works: it will ask you to tap the number of
# guesses on an inline keyboard instead of reading the picture.
ANTHROPIC_API_KEY = _str("ANTHROPIC_API_KEY")
VISION_MODEL = _str("VISION_MODEL", "claude-sonnet-5")

# --- Storage ----------------------------------------------------------------
# If DATABASE_URL is set (e.g. a Supabase Postgres connection string) the bot
# uses it. Otherwise it keeps everything in a local SQLite file.
DATABASE_URL = _str("DATABASE_URL")
DB_PATH = _str("DB_PATH", "wordle.sqlite3")

# --- Puzzle numbering -------------------------------------------------------
# Wordle #0 was 2021-06-19 (so #1000 == 2024-03-15).
# If your bot is ever off by one vs. the real game, set PUZZLE_OFFSET=1 or -1.
PUZZLE_OFFSET = _int("PUZZLE_OFFSET", 0)

# Timezone used for players who never ran /tz.
DEFAULT_TZ = _str("DEFAULT_TZ", "UTC")

# --- League rules -----------------------------------------------------------
# A failed puzzle (X/6) is ranked as if it were this many guesses.
FAIL_SCORE = 7
# A day only counts toward "wins" if at least this many people submitted.
MIN_PLAYERS_FOR_WIN = _int("MIN_PLAYERS_FOR_WIN", 2)
# On a tie, everyone on the best score gets a win. Set to false to award none.
SHARED_WINS = _bool("SHARED_WINS", True)
# Allow a player to overwrite an already-recorded result for the same puzzle.
ALLOW_OVERWRITE = _bool("ALLOW_OVERWRITE", False)

# --- Daily recap --------------------------------------------------------------
# Once everyone's submitted, forward each player's screenshot to the group
# (works regardless of the API key — it's plain Telegram forwarding).
REVEAL_SCREENSHOTS = _bool("REVEAL_SCREENSHOTS", True)
# ...and post a short Claude-written sports-commentary recap. Needs
# ANTHROPIC_API_KEY; silently skipped without one.
DAILY_COMMENTARY = _bool("DAILY_COMMENTARY", True)

# --- Comments -----------------------------------------------------------------
# After a day is revealed, plain text sent to the bot is relayed to the other
# players as a comment.
COMMENTS = _bool("COMMENTS", True)
# Safety valves: longest relayed comment, and comments per player per day.
COMMENT_MAX_CHARS = _int("COMMENT_MAX_CHARS", 1000)
COMMENT_MAX_PER_DAY = _int("COMMENT_MAX_PER_DAY", 30)

# --- Scheduling -------------------------------------------------------------
# How often (seconds) to check whether a day can be closed and announced.
CHECK_INTERVAL = _int("CHECK_INTERVAL", 300)
