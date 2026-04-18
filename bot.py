import os
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import aiohttp

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CRICKET_API_KEY = os.getenv("CRICKET_API_KEY")
CRICKET_API_BASE = "https://api.cricapi.com/v1"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── State ──────────────────────────────────────────────────────────────────────
# watched_matches[chat_id] = { match_id: { last_score_snapshot } }
watched_matches: dict[int, dict[str, dict]] = {}


# ── Helpers ────────────────────────────────────────────────────────────────────

async def fetch_live_matches() -> list[dict]:
    url = f"{CRICKET_API_BASE}/currentMatches?apikey={CRICKET_API_KEY}&offset=0"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if data.get("status") == "success":
                return data.get("data", [])
            return []


async def fetch_match_info(match_id: str) -> dict | None:
    url = f"{CRICKET_API_BASE}/match_info?apikey={CRICKET_API_KEY}&id={match_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if data.get("status") == "success":
                return data.get("data")
            return None


def format_match_card(m: dict) -> str:
    name     = m.get("name", "Unknown Match")
    status   = m.get("status", "—")
    mtype    = m.get("matchType", "").upper()
    venue    = m.get("venue", "")
    teams    = m.get("teams", [])
    scores   = m.get("score", [])

    lines = [f"🏏 *{name}*", f"📍 {venue}" if venue else "", f"🏷 {mtype}"]
    lines = [l for l in lines if l]

    if scores:
        lines.append("")
        for s in scores:
            inning = s.get("inning", "")
            r, w, o = s.get("r", 0), s.get("w", 0), s.get("o", 0)
            lines.append(f"  `{inning}`: *{r}/{w}* ({o} ov)")
    elif teams:
        lines.append(f"\n  {teams[0]} vs {teams[1]}")

    lines.append(f"\n📊 _{status}_")
    return "\n".join(lines)


def score_snapshot(m: dict) -> dict:
    """Extract a minimal snapshot to detect changes."""
    return {
        "status": m.get("status"),
        "scores": [
            (s.get("inning"), s.get("r"), s.get("w"), s.get("o"))
            for s in m.get("score", [])
        ],
    }


def detect_changes(old: dict, new: dict) -> list[str]:
    """Return human-readable change descriptions."""
    changes = []

    # Status change (e.g. match ended, innings break)
    if old["status"] != new["status"]:
        changes.append(f"📢 Status: _{new['status']}_")

    old_scores = {item[0]: item for item in old["scores"]}
    new_scores = {item[0]: item for item in new["scores"]}

    for inning, (ing, r, w, o) in new_scores.items():
        if inning not in old_scores:
            changes.append(f"🆕 New inning started: *{inning}*")
            continue
        _, old_r, old_w, old_o = old_scores[inning]

        # Wicket fell
        if w > old_w:
            diff = w - old_w
            changes.append(f"🔴 *{diff} wicket{'s' if diff > 1 else ''} fell!*  ({inning}: {r}/{w})")

        # Over completed
        if int(o) > int(old_o):
            changes.append(f"⚪️ Over {int(o)} complete  ({inning}: {r}/{w})")

        # Boundary / big run jump (≥4 between polls) without wicket
        elif r - old_r >= 4 and w == old_w:
            diff = r - old_r
            emoji = "🟢" if diff >= 6 else "🔵"
            changes.append(f"{emoji} *+{diff} runs*  ({inning}: {r}/{w})")

    return changes


# ── Commands ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Cricket Live Bot* mein aapka swagat hai!\n\n"
        "Available commands:\n"
        "🏏 /live — Abhi chal rahe matches dekho\n"
        "👁 /watching — Kaunse matches track ho rahe hain\n"
        "⛔️ /stopall — Sab tracking band karo\n\n"
        "_/live command use karo aur match select karke tracking shuru karo!_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_live(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Live matches fetch ho rahe hain...", parse_mode="Markdown")

    matches = await fetch_live_matches()
    live = [m for m in matches if not m.get("matchEnded", False)]

    if not live:
        await update.message.reply_text("😴 Abhi koi live match nahi chal raha.")
        return

    # Show up to 8 live matches as inline buttons
    keyboard = []
    for m in live[:8]:
        name = m.get("name", "Unknown")[:40]
        mid  = m.get("id", "")
        keyboard.append([InlineKeyboardButton(f"👁 {name}", callback_data=f"watch:{mid}")])

    await update.message.reply_text(
        f"🏏 *{len(live)} live match(es) chal rahe hain:*\nSelect karke track karo 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_watching(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    watching = watched_matches.get(chat_id, {})

    if not watching:
        await update.message.reply_text("👁 Abhi koi match track nahi ho raha.\n/live use karo!")
        return

    matches = await fetch_live_matches()
    match_map = {m["id"]: m for m in matches}

    lines = ["👁 *Tracked Matches:*\n"]
    keyboard = []
    for mid in watching:
        m = match_map.get(mid)
        name = m.get("name", mid) if m else mid
        lines.append(f"• {name}")
        keyboard.append([InlineKeyboardButton(f"⛔ Stop: {name[:30]}", callback_data=f"unwatch:{mid}")])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


async def cmd_stopall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    watched_matches.pop(chat_id, None)
    await update.message.reply_text("⛔️ Sab match tracking band kar di!")


# ── Callbacks ──────────────────────────────────────────────────────────────────

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data    = query.data

    if data.startswith("watch:"):
        mid = data.split(":", 1)[1]

        # Fetch initial snapshot
        matches = await fetch_live_matches()
        m = next((x for x in matches if x["id"] == mid), None)
        if not m:
            await query.edit_message_text("❌ Match nahi mila. Shayad khatam ho gaya.")
            return

        if chat_id not in watched_matches:
            watched_matches[chat_id] = {}

        if mid in watched_matches[chat_id]:
            await query.edit_message_text("✅ Yeh match already track ho raha hai!")
            return

        watched_matches[chat_id][mid] = score_snapshot(m)

        card = format_match_card(m)
        await query.edit_message_text(
            f"✅ *Tracking shuru!*\n\n{card}\n\n_Score/wicket/over change hone par alert milega._",
            parse_mode="Markdown",
        )

    elif data.startswith("unwatch:"):
        mid = data.split(":", 1)[1]
        if chat_id in watched_matches:
            watched_matches[chat_id].pop(mid, None)
        await query.edit_message_text("⛔️ Match tracking band kar di!")


# ── Background Poller ──────────────────────────────────────────────────────────

async def poll_scores(app: Application):
    """Runs every 30 seconds, checks for score changes."""
    await asyncio.sleep(10)  # initial delay

    while True:
        try:
            if not any(watched_matches.values()):
                await asyncio.sleep(30)
                continue

            matches = await fetch_live_matches()
            match_map = {m["id"]: m for m in matches}

            for chat_id, tracking in list(watched_matches.items()):
                for mid, old_snap in list(tracking.items()):
                    m = match_map.get(mid)

                    # Match ended or not found
                    if not m or m.get("matchEnded", False):
                        del watched_matches[chat_id][mid]
                        try:
                            await app.bot.send_message(
                                chat_id,
                                f"🏁 Match khatam ho gaya!\n_{old_snap.get('status', '')}_",
                                parse_mode="Markdown",
                            )
                        except Exception:
                            pass
                        continue

                    new_snap = score_snapshot(m)
                    changes  = detect_changes(old_snap, new_snap)

                    if changes:
                        watched_matches[chat_id][mid] = new_snap
                        card    = format_match_card(m)
                        change_text = "\n".join(changes)
                        msg = f"{change_text}\n\n{card}"
                        try:
                            await app.bot.send_message(chat_id, msg, parse_mode="Markdown")
                        except Exception as e:
                            logger.warning(f"Send failed: {e}")

        except Exception as e:
            logger.error(f"Poller error: {e}")

        await asyncio.sleep(30)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("live", cmd_live))
    app.add_handler(CommandHandler("watching", cmd_watching))
    app.add_handler(CommandHandler("stopall", cmd_stopall))
    app.add_handler(CallbackQueryHandler(on_button))

    loop = asyncio.get_event_loop()
    loop.create_task(poll_scores(app))

    logger.info("🏏 Cricket Bot chal raha hai...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
