import os
import asyncio
import logging
import aiohttp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Cricbuzz internal API endpoints
CB_BASE     = "https://www.cricbuzz.com/api/cricket-match"
CB_LIVE_URL = "https://www.cricbuzz.com/api/cricket-match/live-matches"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.cricbuzz.com/cricket-match/live-scores",
    "Origin": "https://www.cricbuzz.com",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "x-pitchvision-client": "cricbuzz-webapp",
}

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── State ──────────────────────────────────────────────────────────────────────
# watched[chat_id] = { match_id: last_snapshot }
watched: dict[int, dict[str, dict]] = {}


# ── Cricbuzz Fetchers ──────────────────────────────────────────────────────────

async def fetch_live_matches() -> list[dict]:
    import json
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        async with s.get(CB_LIVE_URL, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                raise Exception(f"Cricbuzz ne {r.status} diya. Thodi der baad try karo.")
            text = await r.text()
            if not text or not text.strip().startswith("{"):
                raise Exception("Cricbuzz blocked kar raha hai. 2-3 min baad /live try karo.")
            data = json.loads(text)
            matches = []
            for type_group in data.get("typeMatches", []):
                for series in type_group.get("seriesMatches", []):
                    for m in series.get("seriesAdWrapper", {}).get("matches", []):
                        mi = m.get("matchInfo", {})
                        ms = m.get("matchScore", {})
                        matches.append({"info": mi, "score": ms})
            return matches


async def fetch_scorecard(match_id: str) -> dict | None:
    import json
    url = f"{CB_BASE}/{match_id}/full-scorecard"
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            text = await r.text()
            if not text or not text.strip().startswith("{"):
                return None
            return json.loads(text)


# ── Formatters ─────────────────────────────────────────────────────────────────

def dismissal_text(how_out: str, bowler: str, fielder: str) -> str:
    how = (how_out or "").lower()
    if not how or how in ("batting", "yet to bat", "did not bat"):
        return ""
    if how == "not out":
        return "not out"
    if how == "bowled":
        return f"b {bowler}"
    if how == "lbw":
        return f"lbw b {bowler}"
    if how == "caught":
        if fielder and fielder != bowler:
            return f"c {fielder} b {bowler}"
        return f"c&b {bowler}"
    if how == "run out":
        return f"run out ({fielder})" if fielder else "run out"
    if how == "stumped":
        return f"st {fielder} b {bowler}"
    if how == "hit wicket":
        return f"hit wkt b {bowler}"
    if how == "retired hurt":
        return "retired hurt"
    return how


def format_scorecard(sc: dict) -> str:
    if not sc:
        return "❌ Scorecard nahi mila."

    match_header = sc.get("matchHeader", {})
    match_desc   = match_header.get("matchDescription", "")
    series_name  = match_header.get("seriesName", "")
    state        = match_header.get("state", "")
    status       = match_header.get("status", "")

    lines = []
    lines.append(f"🏏 *{series_name}*")
    lines.append(f"_{match_desc}_")
    if status:
        lines.append(f"📊 {status}")
    lines.append("")

    for inning in sc.get("scorecard", []):
        inn_title = inning.get("inningsId", "")
        team_name = inning.get("batTeamName", "")
        runs      = inning.get("score", 0)
        wickets   = inning.get("wickets", 0)
        overs     = inning.get("overs", 0)
        is_dec    = inning.get("isDeclared", False)
        is_fo     = inning.get("isFollowOn", False)

        dec_str = " (d)" if is_dec else ""
        fo_str  = " (f/o)" if is_fo else ""
        lines.append(f"🏏 *{team_name} Innings {inn_title}* — `{runs}/{wickets}{dec_str}{fo_str}` ({overs} ov)")
        lines.append("")

        # Batsmen
        batsmen = inning.get("batsman", [])
        if batsmen:
            lines.append("*Batting:*")
            for b in batsmen:
                name     = b.get("name", "")
                runs_b   = b.get("runs", "-")
                balls    = b.get("balls", "-")
                fours    = b.get("fours", 0)
                sixes    = b.get("sixes", 0)
                how_out  = b.get("outDesc", "")
                bowler   = b.get("bowlerName", "")
                fielder  = b.get("fielderName", "")

                dis = dismissal_text(how_out, bowler, fielder)

                if dis == "not out":
                    status_icon = "🟢"
                    dis_str = " *(not out)*"
                elif dis == "":
                    status_icon = "🟡"
                    dis_str = ""
                else:
                    status_icon = "🔴"
                    dis_str = f" _({dis})_"

                sr_str = ""
                try:
                    sr = round(int(runs_b) / int(balls) * 100, 1) if int(balls) > 0 else 0.0
                    sr_str = f" SR:{sr}"
                except Exception:
                    pass

                lines.append(f"{status_icon} `{name:<20}` *{runs_b}* ({balls}b) 4s:{fours} 6s:{sixes}{sr_str}{dis_str}")

        lines.append("")

        # Bowlers
        bowlers = inning.get("bowler", [])
        if bowlers:
            lines.append("*Bowling:*")
            for bw in bowlers:
                bname   = bw.get("name", "")
                ov      = bw.get("overs", 0)
                maiden  = bw.get("maidens", 0)
                runs_bw = bw.get("runs", 0)
                wkts    = bw.get("wickets", 0)
                econ    = bw.get("economy", 0)
                lines.append(f"⚪️ `{bname:<20}` {ov}ov  {runs_bw}r  *{wkts}w*  M:{maiden}  Econ:{econ}")

        lines.append("")
        lines.append("─────────────────────")
        lines.append("")

    return "\n".join(lines)


def format_live_card(info: dict, score: dict) -> str:
    t1  = info.get("team1", {}).get("teamName", "T1")
    t2  = info.get("team2", {}).get("teamName", "T2")
    fmt = info.get("matchFormat", "")
    state = info.get("state", "")

    inn1 = score.get("inngs1", {})
    inn2 = score.get("inngs2", {})

    def inn_str(inn):
        if not inn:
            return "Yet to bat"
        r, w, o = inn.get("runs", 0), inn.get("wickets", 0), inn.get("overs", "0")
        return f"{r}/{w} ({o} ov)"

    return (
        f"🏏 *{t1} vs {t2}* — {fmt}\n"
        f"  {t1}: `{inn_str(inn1)}`\n"
        f"  {t2}: `{inn_str(inn2)}`\n"
        f"📊 _{state}_"
    )


def score_snapshot(info: dict, score: dict) -> dict:
    inn1 = score.get("inngs1", {})
    inn2 = score.get("inngs2", {})
    return {
        "state": info.get("state", ""),
        "inn1": (inn1.get("runs"), inn1.get("wickets"), inn1.get("overs")),
        "inn2": (inn2.get("runs"), inn2.get("wickets"), inn2.get("overs")),
    }


def detect_changes(old: dict, new: dict) -> list[str]:
    changes = []
    if old["state"] != new["state"]:
        changes.append(f"📢 *{new['state']}*")

    for key, label in [("inn1", "Inn 1"), ("inn2", "Inn 2")]:
        o, n = old[key], new[key]
        if o == (None, None, None) and n != (None, None, None):
            changes.append(f"🆕 *{label} shuru!*")
            continue
        if None in o or None in n:
            continue
        or_, ow, oo = o
        nr, nw, no = n
        if nw > ow:
            changes.append(f"🔴 *Wicket!* {label}: {nr}/{nw} ({no} ov)")
        if int(float(no)) > int(float(oo)) and nw == ow:
            changes.append(f"⚪️ Over complete — {label}: {nr}/{nw} ({no} ov)")
        elif nr - or_ >= 6 and nw == ow:
            changes.append(f"🟢 *SIX!* +{nr - or_} runs — {label}: {nr}/{nw}")
        elif nr - or_ >= 4 and nw == ow:
            changes.append(f"🔵 *FOUR!* — {label}: {nr}/{nw}")

    return changes


# ── Commands ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Cricket Live Bot*\n\n"
        "🏏 /live — Live matches dekho\n"
        "👁 /watching — Tracked matches\n"
        "⛔ /stopall — Sab band karo",
        parse_mode="Markdown",
    )


async def cmd_live(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching live matches...")
    try:
        matches = await fetch_live_matches()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return

    live = [m for m in matches if m["info"].get("state", "").lower() == "in progress"]
    if not live:
        await update.message.reply_text("😴 Koi live match nahi abhi.")
        return

    keyboard = []
    for m in live[:8]:
        info = m["info"]
        t1   = info.get("team1", {}).get("teamShortName", "T1")
        t2   = info.get("team2", {}).get("teamShortName", "T2")
        mid  = str(info.get("matchId", ""))
        fmt  = info.get("matchFormat", "")
        keyboard.append([InlineKeyboardButton(
            f"🏏 {t1} vs {t2} [{fmt}]",
            callback_data=f"watch:{mid}"
        )])

    await update.message.reply_text(
        f"🔴 *{len(live)} live match(es):*\nSelect karo track karne ke liye 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_watching(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    w = watched.get(chat_id, {})
    if not w:
        await update.message.reply_text("Koi match track nahi ho raha. /live use karo!")
        return

    keyboard = []
    for mid in w:
        keyboard.append([InlineKeyboardButton(f"⛔ Stop match {mid}", callback_data=f"unwatch:{mid}")])

    await update.message.reply_text(
        f"👁 *{len(w)} match(es) tracked.*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_stopall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    watched.pop(update.effective_chat.id, None)
    await update.message.reply_text("⛔ Sab tracking band!")


# ── Button Callbacks ───────────────────────────────────────────────────────────

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data    = query.data

    if data.startswith("watch:"):
        mid = data.split(":", 1)[1]
        try:
            matches = await fetch_live_matches()
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
            return

        m = next((x for x in matches if str(x["info"].get("matchId")) == mid), None)
        if not m:
            await query.edit_message_text("❌ Match nahi mila.")
            return

        if chat_id not in watched:
            watched[chat_id] = {}

        if mid in watched[chat_id]:
            await query.edit_message_text("✅ Already tracked hai!")
            return

        watched[chat_id][mid] = score_snapshot(m["info"], m["score"])

        card = format_live_card(m["info"], m["score"])
        await query.edit_message_text(
            f"✅ *Tracking shuru!*\n\n{card}\n\n_Score/wicket/over change hone par alert milega._",
            parse_mode="Markdown",
        )

    elif data.startswith("scorecard:"):
        mid = data.split(":", 1)[1]
        await query.edit_message_text("⏳ Scorecard load ho raha hai...")
        sc = await fetch_scorecard(mid)
        text = format_scorecard(sc)
        # Telegram 4096 char limit
        if len(text) > 4000:
            text = text[:4000] + "\n\n_...scorecard truncated_"
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data.startswith("unwatch:"):
        mid = data.split(":", 1)[1]
        if chat_id in watched:
            watched[chat_id].pop(mid, None)
        await query.edit_message_text("⛔ Tracking band kar di!")


# ── Background Poller ──────────────────────────────────────────────────────────

async def poll_scores(app: Application):
    await asyncio.sleep(15)

    while True:
        try:
            if not any(watched.values()):
                await asyncio.sleep(30)
                continue

            matches = await fetch_live_matches()
            match_map = {str(m["info"].get("matchId")): m for m in matches}

            for chat_id, tracking in list(watched.items()):
                for mid, old_snap in list(tracking.items()):
                    m = match_map.get(mid)

                    if not m or m["info"].get("state", "").lower() not in ("in progress",):
                        del watched[chat_id][mid]
                        try:
                            await app.bot.send_message(chat_id, "🏁 *Match khatam!*", parse_mode="Markdown")
                        except Exception:
                            pass
                        continue

                    new_snap = score_snapshot(m["info"], m["score"])
                    changes  = detect_changes(old_snap, new_snap)

                    if changes:
                        watched[chat_id][mid] = new_snap

                        # Fetch full scorecard on wicket
                        sc_text = ""
                        if any("Wicket" in c for c in changes):
                            try:
                                sc = await fetch_scorecard(mid)
                                sc_text = "\n\n" + format_scorecard(sc)
                            except Exception:
                                sc_text = ""

                        change_str = "\n".join(changes)
                        live_card  = format_live_card(m["info"], m["score"])
                        full_msg   = f"{change_str}\n\n{live_card}{sc_text}"

                        if len(full_msg) > 4000:
                            full_msg = full_msg[:4000] + "\n\n_...truncated_"

                        kb = [[InlineKeyboardButton("📋 Full Scorecard", callback_data=f"scorecard:{mid}")]]
                        try:
                            await app.bot.send_message(
                                chat_id, full_msg,
                                parse_mode="Markdown",
                                reply_markup=InlineKeyboardMarkup(kb),
                            )
                        except Exception as e:
                            logger.warning(f"Send failed: {e}")

        except Exception as e:
            logger.error(f"Poller error: {e}")

        await asyncio.sleep(30)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("live",    cmd_live))
    app.add_handler(CommandHandler("watching", cmd_watching))
    app.add_handler(CommandHandler("stopall", cmd_stopall))
    app.add_handler(CallbackQueryHandler(on_button))

    loop = asyncio.get_event_loop()
    loop.create_task(poll_scores(app))

    logger.info("🏏 Bot chal raha hai...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
