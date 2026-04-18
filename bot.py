import os
import asyncio
import logging
import aiohttp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
RAPIDAPI_KEY    = os.getenv("RAPIDAPI_KEY")

BASE_URL = "https://cricbuzz-cricket.p.rapidapi.com"
HEADERS  = {
    "x-rapidapi-host": "cricbuzz-cricket.p.rapidapi.com",
    "x-rapidapi-key":  RAPIDAPI_KEY,
}

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

watched: dict[int, dict[str, dict]] = {}


async def api_get(path: str) -> dict:
    url = f"{BASE_URL}{path}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 429:
                raise Exception("RapidAPI rate limit. Thodi der baad try karo.")
            if r.status != 200:
                raise Exception(f"API error {r.status}")
            return await r.json()


async def fetch_live_matches() -> list[dict]:
    data    = await api_get("/matches/v1/live")
    matches = []
    for type_group in data.get("typeMatches", []):
        for series in type_group.get("seriesMatches", []):
            for m in series.get("seriesAdWrapper", {}).get("matches", []):
                matches.append(m)
    return matches


async def fetch_scorecard(match_id: str) -> dict | None:
    try:
        return await api_get(f"/mcenter/v1/{match_id}/scard")
    except Exception:
        return None


async def fetch_leanback(match_id: str) -> dict | None:
    try:
        return await api_get(f"/mcenter/v1/{match_id}/leanback")
    except Exception:
        return None


def dismissal_str(bat: dict) -> str:
    out_desc = bat.get("outDesc", "").strip()
    if not out_desc or out_desc.lower() in ("batting", "yet to bat", "did not bat", ""):
        return ""
    if out_desc.lower() == "not out":
        return "not out"
    return out_desc


def format_batting_card(batsmen: list[dict]) -> str:
    lines = []
    for b in batsmen:
        name  = b.get("name", "?")
        runs  = b.get("runs", "-")
        balls = b.get("balls", "-")
        fours = b.get("fours", 0)
        sixes = b.get("sixes", 0)
        dis   = dismissal_str(b)

        if dis == "not out":
            icon    = "🟢"
            dis_txt = " *(not out)*"
        elif dis == "":
            icon    = "🟡"
            dis_txt = ""
        else:
            icon    = "🔴"
            dis_txt = f"\n       _↳ {dis}_"

        try:
            sr     = round(int(runs) / int(balls) * 100, 1) if int(balls) > 0 else 0.0
            sr_txt = f" SR:{sr}"
        except Exception:
            sr_txt = ""

        lines.append(f"{icon} `{name:<22}` *{runs}* ({balls}b)  4s:{fours} 6s:{sixes}{sr_txt}{dis_txt}")
    return "\n".join(lines)


def format_bowling_card(bowlers: list[dict]) -> str:
    lines = []
    for bw in bowlers:
        name = bw.get("name", "?")
        ov   = bw.get("overs", 0)
        m    = bw.get("maidens", 0)
        runs = bw.get("runs", 0)
        wkts = bw.get("wickets", 0)
        econ = bw.get("economy", "-")
        lines.append(f"⚪️ `{name:<22}` {ov}ov  {runs}r  *{wkts}w*  M:{m}  Eco:{econ}")
    return "\n".join(lines)


def format_scorecard_msg(sc: dict) -> str:
    if not sc:
        return "❌ Scorecard nahi mila."
    header = sc.get("matchHeader", {})
    lines  = [
        f"🏏 *{header.get('seriesName', '')}*",
        f"_{header.get('matchDescription', '')}_",
    ]
    if header.get("status"):
        lines.append(f"📊 {header['status']}")
    lines.append("")

    for inn in sc.get("scorecard", []):
        team  = inn.get("batTeamName", "")
        runs  = inn.get("score", 0)
        wkts  = inn.get("wickets", 0)
        overs = inn.get("overs", 0)
        inn_id= inn.get("inningsId", "")
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"🏏 *{team}* Inn {inn_id}  `{runs}/{wkts}` ({overs} ov)",
            "",
        ]
        if inn.get("batsman"):
            lines += ["*Batting:*", format_batting_card(inn["batsman"]), ""]
        if inn.get("bowler"):
            lines += ["*Bowling:*", format_bowling_card(inn["bowler"]), ""]

    return "\n".join(lines)


def format_live_card(m: dict) -> str:
    info  = m.get("matchInfo", {})
    score = m.get("matchScore", {})
    t1    = info.get("team1", {}).get("teamName", "T1")
    t2    = info.get("team2", {}).get("teamName", "T2")
    fmt   = info.get("matchFormat", "")
    state = info.get("status", "")

    def inn_str(key):
        inn = score.get(key, {})
        if not inn:
            return "Yet to bat"
        return f"{inn.get('runs',0)}/{inn.get('wickets',0)} ({inn.get('overs','0')} ov)"

    return (
        f"🏏 *{t1} vs {t2}* — {fmt}\n"
        f"  {t1}: `{inn_str('inngs1')}`\n"
        f"  {t2}: `{inn_str('inngs2')}`\n"
        f"📊 _{state}_"
    )


def format_leanback_alert(lb: dict) -> str:
    if not lb:
        return ""
    lines = []
    batsmen = lb.get("batTeam", {}).get("batsman", [])
    if batsmen:
        lines.append("🏏 *Batting now:*")
        for b in batsmen:
            lines.append(f"  `{b.get('name','?')}` — *{b.get('runs',0)}* ({b.get('balls',0)}b)")
    bowler = lb.get("bowlTeam", {}).get("bowler", {})
    if bowler:
        lines.append(
            f"⚪️ *Bowling:* `{bowler.get('name','?')}` — "
            f"{bowler.get('overs',0)}ov {bowler.get('runs',0)}r *{bowler.get('wickets',0)}w*"
        )
    last_wkt = lb.get("lastWicket", "")
    if last_wkt:
        lines.append(f"🔴 *Last Wicket:* _{last_wkt}_")
    return "\n".join(lines)


def score_snapshot(m: dict) -> dict:
    score = m.get("matchScore", {})
    info  = m.get("matchInfo", {})
    inn1  = score.get("inngs1", {})
    inn2  = score.get("inngs2", {})
    return {
        "status": info.get("status", ""),
        "inn1": (inn1.get("runs"), inn1.get("wickets"), str(inn1.get("overs", "0"))),
        "inn2": (inn2.get("runs"), inn2.get("wickets"), str(inn2.get("overs", "0"))),
    }


def detect_changes(old: dict, new: dict) -> list[str]:
    changes = []
    if old["status"] != new["status"]:
        changes.append(f"📢 *{new['status']}*")
    for key, label in [("inn1", "Inn 1"), ("inn2", "Inn 2")]:
        o, n = old[key], new[key]
        if o == (None, None, "0") and n[0] is not None:
            changes.append(f"🆕 *{label} shuru!*")
            continue
        if None in o or None in n:
            continue
        or_, ow, oo = o
        nr, nw, no  = n
        if nw > ow:
            changes.append(f"🔴 *Wicket!* {label}: {nr}/{nw} ({no} ov)")
        elif int(float(no)) > int(float(oo)) and nw == ow:
            changes.append(f"⚪️ Over complete — {label}: {nr}/{nw} ({no} ov)")
        elif nr - or_ >= 6 and nw == ow:
            changes.append(f"🟢 *SIX!* +{nr - or_} — {label}: {nr}/{nw}")
        elif nr - or_ >= 4 and nw == ow:
            changes.append(f"🔵 *FOUR!* — {label}: {nr}/{nw}")
    return changes


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Cricket Live Bot*\n\n"
        "🏏 /live — Live matches dekho\n"
        "👁 /watching — Tracked matches\n"
        "⛔ /stopall — Sab tracking band karo",
        parse_mode="Markdown",
    )


async def cmd_live(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Live matches fetch ho rahe hain...")
    try:
        matches = await fetch_live_matches()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return

    if not matches:
        await update.message.reply_text("😴 Koi live match nahi abhi.")
        return

    keyboard = []
    for m in matches[:8]:
        info = m.get("matchInfo", {})
        t1   = info.get("team1", {}).get("teamShortName", "T1")
        t2   = info.get("team2", {}).get("teamShortName", "T2")
        fmt  = info.get("matchFormat", "")
        mid  = str(info.get("matchId", ""))
        keyboard.append([InlineKeyboardButton(f"🏏 {t1} vs {t2} [{fmt}]", callback_data=f"watch:{mid}")])

    await update.message.reply_text(
        f"🔴 *{len(matches)} live match(es):*\nSelect karo 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_watching(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    w = watched.get(chat_id, {})
    if not w:
        await update.message.reply_text("Koi match track nahi.\n/live use karo!")
        return
    keyboard = [[InlineKeyboardButton(f"⛔ Stop {mid}", callback_data=f"unwatch:{mid}")] for mid in w]
    await update.message.reply_text(f"👁 *{len(w)} match(es) tracked.*", parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_stopall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    watched.pop(update.effective_chat.id, None)
    await update.message.reply_text("⛔ Sab tracking band!")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
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
        m = next((x for x in matches if str(x.get("matchInfo", {}).get("matchId")) == mid), None)
        if not m:
            await query.edit_message_text("❌ Match nahi mila.")
            return
        if chat_id not in watched:
            watched[chat_id] = {}
        if mid in watched[chat_id]:
            await query.edit_message_text("✅ Already tracked!")
            return
        watched[chat_id][mid] = score_snapshot(m)
        card = format_live_card(m)
        kb   = [[InlineKeyboardButton("📋 Scorecard", callback_data=f"scorecard:{mid}")]]
        await query.edit_message_text(
            f"✅ *Tracking shuru!*\n\n{card}\n\n_Score/wicket/over change hone par alert aayega._",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("scorecard:"):
        mid = data.split(":", 1)[1]
        await query.edit_message_text("⏳ Scorecard load ho raha hai...")
        sc   = await fetch_scorecard(mid)
        text = format_scorecard_msg(sc)
        if len(text) > 4000:
            text = text[:4000] + "\n_...truncated_"
        kb = [[InlineKeyboardButton("🔄 Refresh", callback_data=f"scorecard:{mid}")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("unwatch:"):
        mid = data.split(":", 1)[1]
        if chat_id in watched:
            watched[chat_id].pop(mid, None)
        await query.edit_message_text("⛔ Tracking band!")


async def poll_scores(app: Application):
    await asyncio.sleep(20)
    while True:
        try:
            if not any(watched.values()):
                await asyncio.sleep(30)
                continue
            matches   = await fetch_live_matches()
            match_map = {str(m.get("matchInfo", {}).get("matchId")): m for m in matches}

            for chat_id, tracking in list(watched.items()):
                for mid, old_snap in list(tracking.items()):
                    m = match_map.get(mid)
                    if not m:
                        del watched[chat_id][mid]
                        try:
                            await app.bot.send_message(chat_id, "🏁 *Match khatam!*", parse_mode="Markdown")
                        except Exception:
                            pass
                        continue
                    new_snap = score_snapshot(m)
                    changes  = detect_changes(old_snap, new_snap)
                    if changes:
                        watched[chat_id][mid] = new_snap
                        extra = ""
                        if any("Wicket" in c for c in changes):
                            lb = await fetch_leanback(mid)
                            if lb:
                                extra = "\n\n" + format_leanback_alert(lb)
                        full_msg = "\n".join(changes) + "\n\n" + format_live_card(m) + extra
                        if len(full_msg) > 4000:
                            full_msg = full_msg[:4000] + "\n_...truncated_"
                        kb = [[InlineKeyboardButton("📋 Full Scorecard", callback_data=f"scorecard:{mid}")]]
                        try:
                            await app.bot.send_message(chat_id, full_msg, parse_mode="Markdown",
                                                       reply_markup=InlineKeyboardMarkup(kb))
                        except Exception as e:
                            logger.warning(f"Send failed: {e}")
        except Exception as e:
            logger.error(f"Poller error: {e}")
        await asyncio.sleep(30)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("live",     cmd_live))
    app.add_handler(CommandHandler("watching", cmd_watching))
    app.add_handler(CommandHandler("stopall",  cmd_stopall))
    app.add_handler(CallbackQueryHandler(on_button))
    loop = asyncio.get_event_loop()
    loop.create_task(poll_scores(app))
    logger.info("🏏 Cricket Bot chal raha hai...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
