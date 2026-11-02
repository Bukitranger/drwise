"""Storage layer. Talks to Supabase/Postgres when DATABASE_URL is set,
otherwise falls back to a local SQLite file. The rest of the bot doesn't
care which one is in use.

Tables are prefixed `wordle_` so this can live safely alongside other
apps in a shared Supabase project.
"""
import json
import logging
import sqlite3
import threading
import time
from urllib.parse import urlsplit

import config

log = logging.getLogger("wordlebot.db")

BACKEND = "postgres" if config.DATABASE_URL else "sqlite"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_pool = None

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS wordle_players (
    user_id      INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    tz           TEXT NOT NULL,
    chat_id      INTEGER,
    created_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wordle_results (
    puzzle          INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    attempts        INTEGER,
    solved          INTEGER NOT NULL,
    source          TEXT NOT NULL,
    media_hash      TEXT,
    origin_chat_id  INTEGER,
    origin_msg_id   INTEGER,
    guesses         TEXT,
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (puzzle, user_id)
);
CREATE TABLE IF NOT EXISTS wordle_media (
    media_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    puzzle     INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wordle_announcements (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS wordle_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS wordle_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    puzzle     INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS wordle_comments_puzzle_idx ON wordle_comments (puzzle);
"""

SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS wordle_players (
    user_id      BIGINT PRIMARY KEY,
    display_name TEXT   NOT NULL,
    tz           TEXT   NOT NULL,
    chat_id      BIGINT,
    created_at   BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS wordle_results (
    puzzle          INTEGER NOT NULL,
    user_id         BIGINT  NOT NULL,
    attempts        INTEGER,
    solved          INTEGER NOT NULL,
    source          TEXT    NOT NULL,
    media_hash      TEXT,
    origin_chat_id  BIGINT,
    origin_msg_id   BIGINT,
    guesses         TEXT,
    created_at      BIGINT  NOT NULL,
    PRIMARY KEY (puzzle, user_id)
);
CREATE TABLE IF NOT EXISTS wordle_media (
    media_hash TEXT PRIMARY KEY,
    user_id    BIGINT  NOT NULL,
    puzzle     INTEGER NOT NULL,
    created_at BIGINT  NOT NULL
);
CREATE TABLE IF NOT EXISTS wordle_announcements (
    kind       TEXT   NOT NULL,
    key        TEXT   NOT NULL,
    created_at BIGINT NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS wordle_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS wordle_results_puzzle_idx ON wordle_results (puzzle);
CREATE TABLE IF NOT EXISTS wordle_comments (
    id         BIGSERIAL PRIMARY KEY,
    puzzle     INTEGER NOT NULL,
    user_id    BIGINT  NOT NULL,
    body       TEXT    NOT NULL,
    created_at BIGINT  NOT NULL
);
CREATE INDEX IF NOT EXISTS wordle_comments_puzzle_idx ON wordle_comments (puzzle);
"""


def init() -> None:
    global _conn, _pool
    if BACKEND == "postgres":
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        url = config.DATABASE_URL

        # The Supabase dashboard shows two very similar URLs. The API one
        # (https://<ref>.supabase.co) is for REST calls and is NOT a database
        # connection string, so catch that mix-up with a readable message.
        if not url.startswith(("postgresql://", "postgres://")):
            raise SystemExit(
                "\nDATABASE_URL is not a Postgres connection string.\n"
                f"  got: {url[:60]}\n\n"
                "If it starts with https:// you've copied the project API URL.\n"
                "The one you want is in Supabase under:\n"
                "  Settings -> Database -> Connection string -> Session pooler\n"
                "and looks like:\n"
                "  postgresql://postgres.<ref>:<password>"
                "@aws-0-<region>.pooler.supabase.com:5432/postgres\n"
            )

        if "sslmode=" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"

        # A very common paste error: leaving the [YOUR-PASSWORD] placeholder in.
        # Square brackets are reserved in URLs (they denote IPv6 literals), so
        # this otherwise surfaces as a baffling parser traceback.
        if "[" in url or "]" in url:
            raise SystemExit(
                "\nDATABASE_URL still contains square brackets.\n"
                "You've left the password placeholder in the connection string.\n\n"
                "  replace:  :[YOUR-PASSWORD]@\n"
                "  with:     :youractualpassword@\n\n"
                "Delete the brackets too, not just the words inside them.\n"
            )

        # Log where we're actually connecting. The password is never printed.
        try:
            parts = urlsplit(url)
            host, port, user = parts.hostname, parts.port or 5432, parts.username
        except ValueError as exc:
            raise SystemExit(
                f"\nDATABASE_URL could not be parsed: {exc}\n"
                "Check it matches:\n"
                "  postgresql://postgres.<ref>:<password>"
                "@aws-0-<region>.pooler.supabase.com:5432/postgres\n"
            )

        log.info("Connecting to Postgres at %s:%s as %s", host, port, user)
        if host and host.startswith("db."):
            log.warning(
                "That host is Supabase's DIRECT connection, which is IPv6-only "
                "and unreachable from most hosts. Use the Session pooler string "
                "(username postgres.<ref>, host aws-N-<region>.pooler.supabase.com)."
            )

        kwargs = {"row_factory": dict_row, "autocommit": True}
        if ":6543" in url:
            # Supabase's transaction pooler can't handle prepared statements.
            kwargs["prepare_threshold"] = None

        _pool = ConnectionPool(url, min_size=1, max_size=3, kwargs=kwargs, open=True)
        with _pool.connection() as conn:
            conn.execute(SCHEMA_POSTGRES)
    else:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA_SQLITE)
        _conn.commit()

    _migrate()


def _migrate() -> None:
    """Add columns introduced after the initial deploy. Safe to run every
    startup: each ALTER is wrapped so an existing column is just skipped
    rather than crashing the bot.
    """
    for stmt in (
        "ALTER TABLE wordle_results ADD COLUMN origin_chat_id BIGINT" if BACKEND == "postgres"
        else "ALTER TABLE wordle_results ADD COLUMN origin_chat_id INTEGER",
        "ALTER TABLE wordle_results ADD COLUMN origin_msg_id BIGINT" if BACKEND == "postgres"
        else "ALTER TABLE wordle_results ADD COLUMN origin_msg_id INTEGER",
        "ALTER TABLE wordle_results ADD COLUMN guesses TEXT",
    ):
        try:
            _q(stmt)
        except Exception:
            pass  # column already exists — nothing to do


def _q(sql: str, args=(), *, fetch: str | None = None):
    if BACKEND == "postgres":
        # psycopg wants %s placeholders; our SQL is written with ?
        pg_sql = sql.replace("?", "%s")
        with _pool.connection() as conn:
            cur = conn.execute(pg_sql, args)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur.rowcount

    with _lock:
        cur = _conn.execute(sql, args)
        if fetch == "one":
            row = cur.fetchone()
            _conn.commit()
            return row
        if fetch == "all":
            rows = cur.fetchall()
            _conn.commit()
            return rows
        _conn.commit()
        return cur.rowcount


# --- players ----------------------------------------------------------------

def upsert_player(user_id: int, display_name: str, tz: str, chat_id: int | None) -> None:
    _q(
        """INSERT INTO wordle_players (user_id, display_name, tz, chat_id, created_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             display_name=EXCLUDED.display_name,
             tz=EXCLUDED.tz,
             chat_id=COALESCE(EXCLUDED.chat_id, wordle_players.chat_id)""",
        (user_id, display_name, tz, chat_id, int(time.time())),
    )


def get_player(user_id: int):
    return _q("SELECT * FROM wordle_players WHERE user_id=?", (user_id,), fetch="one")


def list_players():
    return _q("SELECT * FROM wordle_players ORDER BY created_at", fetch="all")


def set_tz(user_id: int, tz: str) -> None:
    _q("UPDATE wordle_players SET tz=? WHERE user_id=?", (tz, user_id))


def remove_player(user_id: int) -> None:
    _q("DELETE FROM wordle_players WHERE user_id=?", (user_id,))


# --- results ----------------------------------------------------------------

def save_result(
    puzzle, user_id, attempts, solved, source,
    media_hash=None, origin_chat_id=None, origin_msg_id=None, guesses=None,
) -> None:
    guesses_json = json.dumps(guesses) if guesses else None
    _q(
        """INSERT INTO wordle_results
             (puzzle,user_id,attempts,solved,source,media_hash,
              origin_chat_id,origin_msg_id,guesses,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(puzzle,user_id) DO UPDATE SET
             attempts=EXCLUDED.attempts, solved=EXCLUDED.solved,
             source=EXCLUDED.source,
             media_hash=COALESCE(EXCLUDED.media_hash, wordle_results.media_hash),
             origin_chat_id=COALESCE(EXCLUDED.origin_chat_id, wordle_results.origin_chat_id),
             origin_msg_id=COALESCE(EXCLUDED.origin_msg_id, wordle_results.origin_msg_id),
             guesses=COALESCE(EXCLUDED.guesses, wordle_results.guesses)""",
        (puzzle, user_id, attempts, int(solved), source, media_hash,
         origin_chat_id, origin_msg_id, guesses_json, int(time.time())),
    )


def get_result(puzzle: int, user_id: int):
    return _q(
        "SELECT * FROM wordle_results WHERE puzzle=? AND user_id=?",
        (puzzle, user_id),
        fetch="one",
    )


def delete_result(puzzle: int, user_id: int) -> int:
    return _q("DELETE FROM wordle_results WHERE puzzle=? AND user_id=?", (puzzle, user_id))


def results_for_puzzle(puzzle: int):
    return _q(
        """SELECT r.*, p.display_name FROM wordle_results r
           JOIN wordle_players p USING(user_id) WHERE r.puzzle=?""",
        (puzzle,),
        fetch="all",
    )


def results_range(first: int, last: int):
    return _q(
        """SELECT r.*, p.display_name FROM wordle_results r
           JOIN wordle_players p USING(user_id)
           WHERE r.puzzle BETWEEN ? AND ? ORDER BY r.puzzle""",
        (first, last),
        fetch="all",
    )


def all_results():
    return _q(
        """SELECT r.*, p.display_name FROM wordle_results r
           JOIN wordle_players p USING(user_id) ORDER BY r.puzzle""",
        fetch="all",
    )


def puzzles_pending_daily():
    return [
        r["puzzle"]
        for r in _q(
            """SELECT DISTINCT puzzle FROM wordle_results
               WHERE CAST(puzzle AS TEXT) NOT IN
                     (SELECT key FROM wordle_announcements WHERE kind='daily')
               ORDER BY puzzle""",
            fetch="all",
        )
    ]


# --- media dedupe -----------------------------------------------------------

def media_owner(media_hash: str):
    return _q("SELECT * FROM wordle_media WHERE media_hash=?", (media_hash,), fetch="one")


def remember_media(media_hash: str, user_id: int, puzzle: int) -> None:
    _q(
        """INSERT INTO wordle_media (media_hash,user_id,puzzle,created_at)
           VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
        (media_hash, user_id, puzzle, int(time.time())),
    )


# --- announcements ----------------------------------------------------------

def was_announced(kind: str, key: str) -> bool:
    return _q(
        "SELECT 1 AS hit FROM wordle_announcements WHERE kind=? AND key=?",
        (kind, str(key)),
        fetch="one",
    ) is not None


def mark_announced(kind: str, key: str) -> None:
    _q(
        """INSERT INTO wordle_announcements (kind,key,created_at)
           VALUES (?,?,?) ON CONFLICT DO NOTHING""",
        (kind, str(key), int(time.time())),
    )


# --- comments ---------------------------------------------------------------
# Players can comment on a revealed day. Comments live in their own table and
# never touch results/players/announcements, so game history is unaffected.

def save_comment(puzzle: int, user_id: int, body: str) -> None:
    _q(
        "INSERT INTO wordle_comments (puzzle,user_id,body,created_at) VALUES (?,?,?,?)",
        (puzzle, user_id, body, int(time.time())),
    )


def count_comments(puzzle: int, user_id: int) -> int:
    row = _q(
        "SELECT COUNT(*) AS n FROM wordle_comments WHERE puzzle=? AND user_id=?",
        (puzzle, user_id),
        fetch="one",
    )
    return int(row["n"]) if row else 0


def comments_for_puzzle(puzzle: int):
    # LEFT JOIN so a comment survives (as "Someone") even if its author /leave'd.
    return _q(
        """SELECT c.*, COALESCE(p.display_name, 'Someone') AS display_name
           FROM wordle_comments c
           LEFT JOIN wordle_players p USING(user_id)
           WHERE c.puzzle=? ORDER BY c.id""",
        (puzzle,),
        fetch="all",
    )


def unannounce(kind: str, key: str) -> None:
    """Reopen a day (or period) so it can be properly re-closed once the
    data that made it 'complete' changes — e.g. after /undo removes a
    result, the day is no longer actually finished, even though it was
    marked as such."""
    _q("DELETE FROM wordle_announcements WHERE kind=? AND key=?", (kind, str(key)))


# --- settings ---------------------------------------------------------------

def set_setting(key: str, value: str) -> None:
    _q(
        """INSERT INTO wordle_settings (key,value) VALUES (?,?)
           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""",
        (key, value),
    )


def get_setting(key: str, default=None):
    row = _q("SELECT value FROM wordle_settings WHERE key=?", (key,), fetch="one")
    return row["value"] if row else default
