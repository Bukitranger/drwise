"""Wordle league bot for Telegram.

Send the bot your Wordle result — as pasted share text, a screenshot, or a GIF —
and it records it against today's puzzle number *in your own timezone*. When
everyone has submitted (or when the day has ended everywhere), it announces the
winner and keeps weekly / monthly / all-time tables.
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import commentary
import config
import db
import leaderboards as lb
import vision
import wordle

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("wordlebot")
logging.getLogger("httpx").setLevel(logging.WARNING)

TZ_SET = available_timezones()


# --- helpers ----------------------------------------------------------------

def display_name(update: Update) -> str:
    u = update.effective_user
    return (u.first_name or u.username or str(u.id))[:12]


async def reply(update: Update, text: str, **kw):
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, **kw
    )


def score_keyboard(puzzle: int) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(str(n), callback_data=f"set:{puzzle}:{n}") for n in range(1, 4)]
    row2 = [InlineKeyboardButton(str(n), callback_data=f"set:{puzzle}:{n}") for n in range(4, 7)]
    row3 = [InlineKeyboardButton("X (failed)", callback_data=f"set:{puzzle}:X")]
    return InlineKeyboardMarkup([row1, row2, row3])


def announce_chats() -> set[int]:
    return {p["chat_id"] for p in db.list_players() if p["chat_id"]}


async def broadcast(bot, text: str) -> None:
    for chat_id in announce_chats():
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            log.warning("could not post to %s: %s", chat_id, exc)


# --- commands ---------------------------------------------------------------

HELP = """*Wordle league bot*

Send me your result and I'll log it: paste the share text, or send a
screenshot or GIF of the finished board. Your day is worked out from
*your own* timezone, so we all end up on the same puzzle number.

Submit privately (DM me) if you don't want to see each other's boards
before everyone's done — once the whole group is in, I'll reveal
everyone's screenshots and post the results together.

*Setup*
/join `Europe/Kyiv` — join the league with your timezone
/tz `Asia/Tokyo` — change your timezone
/who — who's playing, and what day it is for them

*Scores*
/today — today's board
/week — this week's table
/month — this month's table
/alltime — all-time table
/me — your own stats
/fix `4` — correct today's entry (or `/fix 1885 4`)
/undo — delete today's entry

*Comments*
Once everyone's results are revealed, just type a message to me and I'll
pass it on to the other players. /comments shows the day's thread.
"""


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, HELP)


async def cmd_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tz = (ctx.args[0] if ctx.args else config.DEFAULT_TZ).strip()
    if tz not in TZ_SET:
        await reply(
            update,
            f"`{tz}` isn't a timezone I know. Use an IANA name like "
            "`Europe/Kyiv`, `America/New_York`, `Asia/Tokyo`.",
        )
        return
    uid = update.effective_user.id
    db.upsert_player(uid, display_name(update), tz, update.effective_chat.id)
    p = wordle.current_puzzle(tz)
    await reply(
        update,
        f"You're in, *{display_name(update)}* ({tz}). "
        f"It's Wordle {wordle.fmt_puzzle(p)} where you are.\n"
        "Send me a result whenever you're done.",
    )


async def cmd_tz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await reply(update, "Usage: `/tz Europe/Berlin`")
        return
    tz = ctx.args[0].strip()
    if tz not in TZ_SET:
        await reply(update, f"Unknown timezone `{tz}`.")
        return
    if not db.get_player(update.effective_user.id):
        db.upsert_player(update.effective_user.id, display_name(update), tz, update.effective_chat.id)
    else:
        db.set_tz(update.effective_user.id, tz)
    await reply(update, f"Timezone set to `{tz}` — that's Wordle {wordle.fmt_puzzle(wordle.current_puzzle(tz))} for you.")


async def cmd_who(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    players = db.list_players()
    if not players:
        await reply(update, "Nobody has joined yet. Try `/join Europe/Kyiv`.")
        return
    lines = []
    for p in players:
        now = datetime.now(ZoneInfo(p["tz"]))
        lines.append(
            f"{p['display_name']:<12} {p['tz']:<20} {now:%H:%M}  "
            f"#{wordle.fmt_puzzle(wordle.current_puzzle(p['tz']))}"
        )
    await reply(update, "*Players*\n```\n" + "\n".join(lines) + "\n```")


async def cmd_leave(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.remove_player(update.effective_user.id)
    await reply(update, "Removed you from the league. Your past results are kept.")


def _requester_puzzle(update: Update) -> int:
    p = db.get_player(update.effective_user.id)
    return wordle.current_puzzle(p["tz"] if p else config.DEFAULT_TZ)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    puzzle = _requester_puzzle(update)
    rows = db.results_for_puzzle(puzzle)
    if not rows:
        await reply(update, f"No results yet for Wordle {wordle.fmt_puzzle(puzzle)}.")
        return
    final = db.was_announced("daily", str(puzzle))
    await reply(update, lb.format_day(puzzle, rows, db.list_players(), final))


async def _range_table(update, first_date, last_date, title):
    first, last = wordle.puzzle_for_date(first_date), wordle.puzzle_for_date(last_date)
    rows = db.results_range(first, last)
    table = lb.standings(rows, db.list_players())
    sub = f"{first_date:%d %b} – {last_date:%d %b %Y}"
    await reply(update, lb.format_table(title, table, sub))


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = wordle.date_for_puzzle(_requester_puzzle(update))
    a, b = lb.week_bounds(d)
    await _range_table(update, a, b, "This week")


async def cmd_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = wordle.date_for_puzzle(_requester_puzzle(update))
    a, b = lb.month_bounds(d)
    await _range_table(update, a, b, f"{d:%B %Y}")


async def cmd_alltime(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db.all_results()
    table = lb.standings(rows, db.list_players())
    await reply(update, lb.format_table("All time", table))


async def cmd_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = db.all_results()
    table = lb.standings(rows, db.list_players())
    mine = next((s for s in table if s["user_id"] == uid), None)
    if not mine:
        await reply(update, "You haven't submitted anything yet.")
        return
    st = lb.streak(rows, uid, _requester_puzzle(update))
    await reply(update, lb.format_personal(mine["name"], mine, st))


async def cmd_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    puzzle = _requester_puzzle(update)
    score = None
    if len(args) == 1:
        score = args[0]
    elif len(args) >= 2:
        try:
            puzzle = int(args[0].replace(",", ""))
        except ValueError:
            await reply(update, "Usage: `/fix 4` or `/fix 1885 4`")
            return
        score = args[1]
    if score is None:
        await reply(update, "What was your score?", reply_markup=score_keyboard(puzzle))
        return
    await _record(update, ctx, puzzle, score, source="manual")


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    puzzle = _requester_puzzle(update)
    n = db.delete_result(puzzle, update.effective_user.id)
    if n:
        # The day was possibly already announced before this deletion — but
        # with one fewer result, it isn't actually complete anymore. Reopen
        # it so the next submission properly re-triggers the full reveal.
        db.unannounce("daily", str(puzzle))
    await reply(
        update,
        f"Deleted your entry for Wordle {wordle.fmt_puzzle(puzzle)}."
        if n
        else "Nothing to delete for today.",
    )


# --- recording --------------------------------------------------------------

async def _record(
    update, ctx, puzzle: int, score, source: str,
    media_hash=None, origin_chat_id=None, origin_msg_id=None, guesses=None,
):
    uid = update.effective_user.id
    if not db.get_player(uid):
        await reply(update, "Send `/join Europe/Kyiv` (your own timezone) first.")
        return

    if isinstance(score, str):
        s = score.strip().lower()
        solved = s not in ("x", "x/6")
        attempts = int(s[0]) if solved and s[0].isdigit() else None
        if solved and attempts not in (1, 2, 3, 4, 5, 6):
            await reply(update, "Score must be 1–6 or X.")
            return
    else:
        attempts, solved = score

    existing = db.get_result(puzzle, uid)
    if existing and source != "manual" and not config.ALLOW_OVERWRITE:
        await reply(
            update,
            f"You already have {wordle.fmt_score(existing['attempts'], bool(existing['solved']))} "
            f"logged for Wordle {wordle.fmt_puzzle(puzzle)}. Use `/fix` to change it.",
        )
        return
    is_new_submission = existing is None

    db.save_result(puzzle, uid, attempts, solved, source, media_hash, origin_chat_id, origin_msg_id, guesses)
    if media_hash:
        db.remember_media(media_hash, uid, puzzle)

    await reply(
        update,
        f"Logged *{wordle.fmt_score(attempts, solved)}* for Wordle "
        f"{wordle.fmt_puzzle(puzzle)} \u2705",
    )

    # Let the others know someone's in — without revealing the score, so it
    # doesn't undercut the whole point of submitting privately. Skip this if
    # that was the LAST submission: the full reveal is about to fire below,
    # and a ping immediately before it is just noise.
    if is_new_submission:
        players = db.list_players()
        count = len({r["user_id"] for r in db.results_for_puzzle(puzzle)})
        if count < len(players):
            player = db.get_player(uid)
            await notify_submission(ctx.bot, update.effective_chat.id, player["display_name"], count, len(players))

    await maybe_announce(ctx.bot, puzzle)


async def notify_submission(bot, submitter_chat_id: int, name: str, count: int, total: int) -> None:
    """Tell everyone else someone's in, without leaking the score."""
    text = f"\u2705 *{name}* submitted today's puzzle. ({count}/{total} in)"
    for chat_id in announce_chats():
        if chat_id == submitter_chat_id:
            continue
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            log.warning("could not notify %s of submission: %s", chat_id, exc)


# --- comments ---------------------------------------------------------------

def comment_window(user_id: int) -> int | None:
    """The puzzle this player can comment on right now, or None.

    A day opens for comments once it has been revealed (announced) and the
    player took part in it. We check their current puzzle and the one before,
    so a comment sent just after their local midnight still lands on the day
    everyone was just looking at.
    """
    player = db.get_player(user_id)
    if not player:
        return None
    cur = wordle.current_puzzle(player["tz"])
    for p in (cur, cur - 1):
        if db.was_announced("daily", str(p)) and db.get_result(p, user_id):
            return p
    return None


def _open_comment_window(update: Update) -> int | None:
    """Comments only work in a private chat with the bot, never in a group —
    otherwise ordinary group chatter would be relayed as 'comments'."""
    if not config.COMMENTS or update.effective_chat.type != "private":
        return None
    return comment_window(update.effective_user.id)


async def relay_comment(update: Update, ctx: ContextTypes.DEFAULT_TYPE, puzzle: int, text: str) -> None:
    """Store a comment and pass it on to every other player.

    Sent as plain text on purpose: a comment containing * or _ would break
    Markdown parsing and the whole message would fail to deliver.
    """
    msg = update.effective_message
    uid = update.effective_user.id
    sender = db.get_player(uid)
    body = text.strip()
    if not body:
        return
    if len(body) > config.COMMENT_MAX_CHARS:
        await msg.reply_text(
            f"That's a bit long ({len(body)} characters) — keep comments under {config.COMMENT_MAX_CHARS}."
        )
        return
    if db.count_comments(puzzle, uid) >= config.COMMENT_MAX_PER_DAY:
        await msg.reply_text("That's plenty of comments for one day — save some for tomorrow.")
        return

    db.save_comment(puzzle, uid, body)

    out = f"\U0001f4ac {sender['display_name']} on Wordle {wordle.fmt_puzzle(puzzle)}:\n{body}"
    delivered, failed = [], []
    for p in db.list_players():
        if p["user_id"] == uid or not p["chat_id"]:
            continue
        try:
            await ctx.bot.send_message(p["chat_id"], out)
            delivered.append(p["display_name"])
        except Exception as exc:
            log.warning("could not deliver comment to %s: %s", p["display_name"], exc)
            failed.append(p["display_name"])

    if delivered and not failed:
        await msg.reply_text(f"Sent to {', '.join(delivered)} ✅")
    elif delivered:
        await msg.reply_text(
            f"Sent to {', '.join(delivered)}, but couldn't reach {', '.join(failed)} "
            "(have they started the bot?)."
        )
    elif failed:
        await msg.reply_text(f"Saved, but I couldn't reach {', '.join(failed)} (have they started the bot?).")
    else:
        await msg.reply_text("Saved — there's nobody else in the league to send it to yet.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Plain text that isn't a result: a comment if a day is open, otherwise
    a short hint about when comments open."""
    if update.effective_chat.type != "private":
        return
    uid = update.effective_user.id
    player = db.get_player(uid)
    if not player:
        return

    window = comment_window(uid)
    if window is not None:
        await relay_comment(update, ctx, window, update.effective_message.text or "")
        return

    puzzle = wordle.current_puzzle(player["tz"])
    if not db.get_result(puzzle, uid):
        hint = "Send me today's result first — comments open once everyone's in."
    else:
        waiting = [
            p["display_name"] for p in db.list_players() if not db.get_result(puzzle, p["user_id"])
        ]
        hint = "Comments open once everyone's in"
        hint += f" — still waiting on {', '.join(waiting)}." if waiting else "."
    await update.effective_message.reply_text(hint)


async def cmd_comments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the comment thread for the day currently open to you."""
    uid = update.effective_user.id
    if not db.get_player(uid):
        await update.effective_message.reply_text("Send /join first.")
        return
    puzzle = comment_window(uid) or _requester_puzzle(update)
    rows = db.comments_for_puzzle(puzzle)
    label = f"Wordle {wordle.fmt_puzzle(puzzle)}"
    if not rows:
        await update.effective_message.reply_text(f"No comments yet for {label}.")
        return
    thread = "\n".join(f"{r['display_name']}: {r['body']}" for r in rows)
    await update.effective_message.reply_text(f"{label} comments\n\n{thread}"[:4000])


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, puzzle_s, score = q.data.split(":")
        puzzle = int(puzzle_s)
    except ValueError:
        return
    uid = q.from_user.id
    solved = score.lower() != "x"
    attempts = int(score) if solved else None
    db.save_result(puzzle, uid, attempts, solved, "manual")
    await q.edit_message_text(
        f"Logged {wordle.fmt_score(attempts, solved)} for Wordle {wordle.fmt_puzzle(puzzle)} \u2705"
    )
    await maybe_announce(ctx.bot, puzzle)


async def on_submission(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    player = db.get_player(uid)

    # Only registered players count. Otherwise anyone dropping a picture into
    # the group would silently become a fourth competitor, and the league
    # would then wait forever for them to submit.
    if not player:
        await reply(
            update,
            "You're not in the league yet — send `/join Europe/Kyiv` "
            "(your own timezone) first, then resend this.",
        )
        return

    tz = player["tz"]
    if update.effective_chat.type != "private" and not player["chat_id"]:
        db.upsert_player(uid, player["display_name"], tz, update.effective_chat.id)

    puzzle = wordle.current_puzzle(tz)

    # 1. Text / caption ------------------------------------------------------
    text = msg.text or msg.caption or ""
    parsed = wordle.parse_text(text)

    # Track where this message came from so a real screenshot can be
    # forward_message'd (not re-uploaded) to the group once everyone's in.
    media_hash = origin_chat_id = origin_msg_id = None

    if msg.photo or msg.animation or msg.document:
        origin_chat_id = update.effective_chat.id
        origin_msg_id = msg.message_id

        file_id = (
            msg.photo[-1].file_id if msg.photo
            else msg.animation.file_id if msg.animation
            else msg.document.file_id
        )
        f = await ctx.bot.get_file(file_id)
        data = bytes(await f.download_as_bytearray())
        media_hash = hashlib.sha256(data).hexdigest()

        owner = db.media_owner(media_hash)
        if owner and owner["user_id"] != uid:
            await reply(update, "\u26a0\ufe0f That exact image was already submitted by someone else.")
            return

        if not parsed.confident:
            mime = (
                (msg.animation.mime_type if msg.animation else msg.document.mime_type)
                if (msg.animation or msg.document) else ""
            ) or ""
            if msg.animation or mime.startswith("video") or mime == "image/gif":
                frame = vision.gif_to_png(data)
                if frame:
                    data = frame

            if vision.available():
                await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
                parsed = await vision.read_image(data)
            else:
                parsed = wordle.Parsed(note="vision disabled")

    # 2. Trust the puzzle number printed in the share text, if any -----------
    if parsed.puzzle and abs(parsed.puzzle - puzzle) <= 2:
        puzzle = parsed.puzzle

    # 3. Record, or ask -------------------------------------------------------
    if parsed.confident and (parsed.attempts or not parsed.solved):
        source = "text" if not media_hash else "image"
        await _record(
            update, ctx, puzzle, (parsed.attempts, parsed.solved), source,
            media_hash=media_hash, origin_chat_id=origin_chat_id,
            origin_msg_id=origin_msg_id, guesses=parsed.guesses,
        )
        return

    # A text-only message that merely mentions a score ("nice 3/6!") is a
    # comment, not a result, once the day has been revealed.
    if not media_hash:
        window = _open_comment_window(update)
        if window is not None:
            await relay_comment(update, ctx, window, text)
            return

    hint = f" ({parsed.note})" if parsed.note else ""
    await reply(
        update,
        f"I couldn't read that with confidence{hint}.\n"
        f"How many guesses for Wordle {wordle.fmt_puzzle(puzzle)}?",
        reply_markup=score_keyboard(puzzle),
    )


# --- announcements ----------------------------------------------------------

def puzzle_closed_everywhere(puzzle: int, players) -> bool:
    """True once every player's local date has moved past this puzzle."""
    return bool(players) and all(wordle.current_puzzle(p["tz"]) > puzzle for p in players)


async def reveal_screenshots(bot, rows: list[dict]) -> None:
    """Forward each player's original message (a real Telegram forward, not
    a re-upload) worst score first, so the winner lands last for effect.
    Only rows that actually included a photo/gif/image have anything to
    forward — text-only submissions are silently skipped.
    """
    revealers = [r for r in rows if r.get("origin_msg_id")]
    if not revealers:
        return
    revealers.sort(key=lambda r: -lb.effective(r))
    chats = announce_chats()
    for r in revealers:
        for chat_id in chats:
            if chat_id == r["origin_chat_id"]:
                continue  # don't forward someone's own screenshot back to them
            try:
                await bot.forward_message(
                    chat_id=chat_id,
                    from_chat_id=r["origin_chat_id"],
                    message_id=r["origin_msg_id"],
                )
            except Exception as exc:
                log.warning("could not forward %s's screenshot: %s", r["display_name"], exc)
        await asyncio.sleep(0.4)  # a beat between reveals reads better than a wall of images


async def maybe_announce(bot, puzzle: int) -> None:
    players = db.list_players()
    if not players or db.was_announced("daily", str(puzzle)):
        return
    rows = [dict(r) for r in db.results_for_puzzle(puzzle)]
    everyone_in = len({r["user_id"] for r in rows}) >= len(players)
    if not (everyone_in or puzzle_closed_everywhere(puzzle, players)):
        return
    if not rows:
        db.mark_announced("daily", str(puzzle))
        return

    db.mark_announced("daily", str(puzzle))
    log.info(
        "closing puzzle %s: %d/%d players submitted (everyone_in=%s)",
        puzzle, len(rows), len(players), everyone_in,
    )
    text = lb.format_day(puzzle, rows, players, final=True)

    d = wordle.date_for_puzzle(puzzle)
    a, b = lb.week_bounds(d)
    wrows = db.results_range(wordle.puzzle_for_date(a), wordle.puzzle_for_date(b))
    table = lb.standings(wrows, players)
    if table:
        text += "\nWeek so far: " + " · ".join(f"{s['name']} {s['wins']}" for s in table)

    await broadcast(bot, text)

    if config.REVEAL_SCREENSHOTS:
        n_media = sum(1 for r in rows if r.get("origin_msg_id"))
        log.info("reveal: %d/%d results have a screenshot to forward", n_media, len(rows))
        await reveal_screenshots(bot, rows)

    if config.DAILY_COMMENTARY:
        quip = await commentary.generate_daily_commentary(rows)
        if quip:
            log.info("commentary generated (%d chars)", len(quip))
            await broadcast(bot, f"\U0001f3a4 _{quip}_")
        else:
            log.info(
                "no commentary posted (ai_client available=%s) — "
                "check ANTHROPIC_API_KEY if this is unexpected",
                commentary.available(),
            )

    await maybe_period_announce(bot, puzzle, players)


async def maybe_period_announce(bot, puzzle: int, players) -> None:
    """After the last puzzle of a week/month is settled, post the summary."""
    d = wordle.date_for_puzzle(puzzle)

    _, wend = lb.week_bounds(d)
    if d == wend and not db.was_announced("weekly", lb.week_key(d)):
        wstart, _ = lb.week_bounds(d)
        rows = db.results_range(wordle.puzzle_for_date(wstart), puzzle)
        table = lb.standings(rows, players)
        if table:
            db.mark_announced("weekly", lb.week_key(d))
            await broadcast(
                bot,
                "\U0001f4c5 " + lb.format_table(
                    "Weekly champion", table, f"{wstart:%d %b} – {wend:%d %b %Y}"
                ),
            )

    _, mend = lb.month_bounds(d)
    if d == mend and not db.was_announced("monthly", lb.month_key(d)):
        mstart, _ = lb.month_bounds(d)
        rows = db.results_range(wordle.puzzle_for_date(mstart), puzzle)
        table = lb.standings(rows, players)
        if table:
            db.mark_announced("monthly", lb.month_key(d))
            await broadcast(
                bot,
                "\U0001f5d3\ufe0f " + lb.format_table(f"{d:%B %Y} champion", table),
            )


async def sweep(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: close out any day that has ended in every timezone."""
    players = db.list_players()
    if not players:
        return
    pending = set(db.puzzles_pending_daily())
    # Also consider yesterday, in case nobody submitted at all.
    pending.add(min(wordle.current_puzzle(p["tz"]) for p in players) - 1)
    for puzzle in sorted(pending):
        try:
            await maybe_announce(ctx.bot, puzzle)
        except Exception as exc:
            log.exception("sweep failed for %s: %s", puzzle, exc)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors as one readable line instead of a wall of traceback."""
    from telegram.error import Conflict, NetworkError

    err = ctx.error
    if isinstance(err, Conflict):
        log.error(
            "Telegram says another instance is polling with this token. "
            "Check for a second Railway service or replica running the same bot."
        )
        return
    if isinstance(err, NetworkError):
        log.warning("Network hiccup talking to Telegram: %s", err)
        return
    log.exception("Unhandled error while processing an update", exc_info=err)


# --- wiring -----------------------------------------------------------------

def main() -> None:
    if not config.TELEGRAM_TOKEN:
        raise SystemExit("Set TELEGRAM_TOKEN (see .env.example)")
    db.init()

    app = Application.builder().token(config.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler(["join", "register"], cmd_join))
    app.add_handler(CommandHandler("tz", cmd_tz))
    app.add_handler(CommandHandler("who", cmd_who))
    app.add_handler(CommandHandler("leave", cmd_leave))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler(["alltime", "all"], cmd_alltime))
    app.add_handler(CommandHandler(["me", "stats"], cmd_me))
    app.add_handler(CommandHandler("fix", cmd_fix))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("comments", cmd_comments))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^set:"))
    app.add_error_handler(on_error)

    media = filters.PHOTO | filters.ANIMATION | filters.Document.IMAGE
    app.add_handler(MessageHandler(media & ~filters.COMMAND, on_submission))

    # Only react to text that actually looks like a result: a score like 4/6,
    # or a row of five Wordle tiles. Deliberately NOT the bare word "wordle",
    # or the bot would interrupt every conversation about the game.
    looks_like_result = filters.Regex(
        r"[1-6xX]\s*/\s*6|[\u2b1b\u2b1c\U0001f7e8\U0001f7e9\U0001f7e6\U0001f7e7]{5}"
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & looks_like_result, on_submission)
    )

    # Everything else typed to the bot is a comment (once a day is revealed).
    # Registered AFTER the result handler: PTB uses the first match, so
    # result-looking text still goes to on_submission.
    if config.COMMENTS:
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if app.job_queue:
        app.job_queue.run_repeating(sweep, interval=config.CHECK_INTERVAL, first=20)
    else:
        log.warning("No job queue — install python-telegram-bot[job-queue] for auto-closing days")

    log.info("Wordle bot running. Vision: %s", "on" if vision.available() else "off")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
