import os
import asyncio
import logging
import aiohttp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# ESPNCricinfo free endpoints — no API key needed!
BASE_URL = "https://hs-consumer-api.espncricinfo.com"
HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "accept": "application/json",
}

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

watched: dict[int, dict[str, dict]] = {}


async def api_get(path: str, params: dict = None) -> dict:
    url = f"{BASE_URL}{path}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=HEADERS, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                raise Exception(f"API error {r.status}")
            return await r.json()


async def fetch_live_matches() -> list[dict]:
    data = await api_get("/v1/pages/matches/current", params={"lang": "en", "latest": "true"})
    matches = []
    for grp in data.get("matchGroups", []):
        for m in grp.get("matches", []):
            if m.get("match", {}).get("state") in ("live", "Live", "LIVE", "inprogress"):
                matches.append(m.get("match", {}))
    # Fallback: return all matches if no live found (some may be "innings break" etc.)
    if not matches:
        for grp in data.get("matchGroups", []):
            for m in grp.get("matches", []):
                ms = m.get("match", {})
                if ms:
                    matches.append(ms)
    return matches


async def fetch_match_details(series_id: str, match_id: str) -> dict | None:
    try:
        return await api_get(
            "/v1/pages/match/home",
            params={"lang": "en", "seriesId": series_id, "matchId": match_id}
        )
    except Exception:
        return None


async def fetch_scorecard(series_id: str, match_id: str) -> dict | None:
    try:
        return await api_get(
            "/v1/pages/match/scorecard",
            params={"lang": "en", "seriesId": series_id, "matchId": match_id}
        )
    except Exception:
        return None


def get_match_id_key(m: dict) -> str:
    """Returns combined key: seriesId:matchId"""
    return f"{m.get('series', {}).get('objectId', '')}:{m.get('objectId', '')}"


def format_innings_score(inn: dict) -> str:
    if not inn:
        return "Yet to bat"
    runs = inn.get("runs", 0)
    wkts = inn.get("wickets", 10)
    overs = inn.get("overs", "0")
    if wkts == 10:
        return f"{runs} ({overs} ov)"
    return f"{runs}/{wkts} ({overs} ov)"


def format_live_card(m: dict) -> str:
    t1 = m.get("teams", [{}])[0].get("team", {}).get("shortName", "T1") if len(m.get("teams", [])) > 0 else "T1"
    t2 = m.get("teams", [{}])[1].get("team", {}).get("shortName", "T2") if len(m.get("teams", [])) > 1 else "T2"
    fmt = m.get("format", "")
    status = m.get("statusText", m.get("status", ""))
    series = m.get("series", {}).get("name", "")

    innings = m.get("innings", [])
    inn_lines = []
    for inn in innings:
        team = inn.get("team", {}).get("shortName", "?")
        score = format_innings_score(inn)
        inn_lines.append(f"  {team}: `{score}`")

    inn_text = "\n".join(inn_lines) if inn_lines else "  Scores not available yet"

    return (
        f"🏏 *{t1} vs {t2}* — {fmt}\n"
        f"_{series}_\n"
        f"{inn_text}\n"
        f"📊 _{status}_"
    )


def format_scorecard_msg(sc_data: dict) -> str:
    if not sc_data:
        return "❌ Scorecard nahi mila."

    match = sc_data.get("match", {})
    innings_list = sc_data.get("scorecard", sc_data.get("innings", []))

    lines = [
        f"🏏 *{match.get('series', {}).get('name', '')}*",
        f"_{match.get('title', '')}_",
    ]
    status = match.get("statusText", "")
    if status:
        lines.append(f"📊 {status}")
    lines.append("")

    for inn in innings_list:
        team = inn.get("team", {}).get("name", inn.get("batTeamName", "?"))
        runs = inn.get("runs", inn.get("score", 0))
        wkts = inn.get("wickets", 0)
        overs = inn.get("overs", 0)
        inn_id = inn.get("inningNumber", inn.get("inningsId", ""))
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"🏏 *{team}* Inn {inn_id}  `{runs}/{wkts}` ({overs} ov)",
            "",
        ]

        batsmen = inn.get("batsmen", inn.get("batsman", []))
        if batsmen:
            lines.append("*Batting:*")
            for b in batsmen:
                name = b.get("player", {}).get("name", b.get("name", "?"))
                r = b.get("runs", "-")
                balls = b.get("balls", "-")
                fours = b.get("fours", 0)
                sixes = b.get("sixes", 0)
                out_desc = b.get("dismissalText", {}).get("short", b.get("outDesc", ""))
                if not out_desc or out_desc.lower() in ("batting", "yet to bat", "did not bat"):
                    icon = "🟡"
                    dis_txt = ""
                elif out_desc.lower() == "not out":
                    icon = "🟢"
                    dis_txt = " *(not out)*"
                else:
                    icon = "🔴"
                    dis_txt = f"\n       _↳ {out_desc}_"
                try:
                    sr = round(int(r) / int(balls) * 100, 1) if int(balls) > 0 else 0.0
                    sr_txt = f" SR:{sr}"
                except Exception:
                    sr_txt = ""
                lines.append(f"{icon} `{name:<22}` *{r}* ({balls}b)  4s:{fours} 6s:{sixes}{sr_txt}{dis_txt}")
            lines.append("")

        bowlers = inn.get("bowlers", inn.get("bowler", []))
        if bowlers:
            lines.append("*Bowling:*")
            for bw in bowlers:
                name = bw.get("player", {}).get("name", bw.get("name", "?"))
                ov = bw.get("overs", 0)
                maiden = bw.get("maidens", 0)
                r = bw.get("runs", 0)
                wkts = bw.get("wickets", 0)
                econ = bw.get("economy", "-")
                lines.append(f"⚪️ `{name:<22}` {ov}ov  {r}r  *{wkts}w*  M:{maiden}  Eco:{econ}")
            lines.append("")

    return "\n".join(lines)


def score_snapshot(m: dict) -> dict:
    innings = m.get("innings", [])
    snap = {"status": m.get("statusText", m.get("status", "")), "innings": []}
    for inn in innings:
        snap["innings"].append({
            "runs": inn.get("runs"),
            "wickets": inn.get("wickets"),
            "overs": str(inn.get("overs", "0")),
        })
    return snap


def detect_changes(old: dict, new: dict) -> list[str]:
    changes = []
    if old["status"] != new["status"]:
        changes.append(f"📢 *{new['status']}*")

    old_inns = old.get("innings", [])
    new_inns = new.get("innings", [])

    # New innings started
    if len(new_inns) > len(old_inns):
        changes.append(f"🆕 *New innings shuru!*")

    for i, (o, n) in enumerate(zip(old_inns, new_inns)):
        label = f"Inn {i+1}"
        or_, ow = o.get("runs"), o.get("wickets")
        nr, nw = n.get("runs"), n.get("wickets")
        oo, no = o.get("overs", "0"), n.get("overs", "0")
        if None in (or_, ow, nr, nw):
            continue
        if nw > ow:
            changes.append(f"🔴 *Wicket!* {label}: {nr}/{nw} ({no} ov)")
        elif int(float(no)) > int(float(oo)) and nw == ow:
            changes.append(f"⚪️ Over complete — {label}: {nr}/{nw} ({no} ov)")
        elif nr - or_ >= 6 and nw == ow:
            changes.append(f"🟢 *SIX!* +{nr - or_} — {label}: {nr}/{nw}")
        elif nr - or_ >= 4 and nw == ow:
            changes.append(f"🔵 *FOUR!* — {label}: {nr}/{nw}")
    return changes


# ─── COMMANDS ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Cricket Live Bot*\n\n"
        "🏏 /live — Live matches dekho\n"
        "👁 /watching — Tracked matches\n"
        "⛔ /stopall — Sab tracking band karo\n\n"
        "_Powered by ESPNCricinfo — Free & No API key!_",
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
        t1 = m.get("teams", [{}])[0].get("team", {}).get("shortName", "T1") if len(m.get("teams", [])) > 0 else "T1"
        t2 = m.get("teams", [{}])[1].get("team", {}).get("shortName", "T2") if len(m.get("teams", [])) > 1 else "T2"
        fmt = m.get("format", "")
        key = get_match_id_key(m)
        keyboard.append([InlineKeyboardButton(f"🏏 {t1} vs {t2} [{fmt}]", callback_data=f"watch:{key}")])

    await update.message.reply_text(
        f"🔴 *{len(matches)} match(es) available:*\nSelect karo 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_watching(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    w = watched.get(chat_id, {})
    if not w:
        await update.message.reply_text("Koi match track nahi.\n/live use karo!")
        return
    keyboard = [[InlineKeyboardButton(f"⛔ Stop {key}", callback_data=f"unwatch:{key}")] for key in w]
    await update.message.reply_text(
        f"👁 *{len(w)} match(es) tracked.*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_stopall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    watched.pop(update.effective_chat.id, None)
    await update.message.reply_text("⛔ Sab tracking band!")


# ─── BUTTON HANDLER ──────────────────────────────────────────────────────────

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data.startswith("watch:"):
        key = data.split(":", 1)[1]
        series_id, match_id = key.split(":")
        try:
            matches = await fetch_live_matches()
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
            return

        m = next((x for x in matches if get_match_id_key(x) == key), None)
        if not m:
            await query.edit_message_text("❌ Match nahi mila.")
            return
        if chat_id not in watched:
            watched[chat_id] = {}
        if key in watched[chat_id]:
            await query.edit_message_text("✅ Already tracked!")
            return
        watched[chat_id][key] = score_snapshot(m)
        card = format_live_card(m)
        kb = [[InlineKeyboardButton("📋 Scorecard", callback_data=f"scorecard:{key}")]]
        await query.edit_message_text(
            f"✅ *Tracking shuru!*\n\n{card}\n\n_Score/wicket/over change hone par alert aayega._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("scorecard:"):
        key = data.split(":", 1)[1]
        series_id, match_id = key.split(":")
        await query.edit_message_text("⏳ Scorecard load ho raha hai...")
        sc = await fetch_scorecard(series_id, match_id)
        text = format_scorecard_msg(sc)
        if len(text) > 4000:
            text = text[:4000] + "\n_...truncated_"
        kb = [[InlineKeyboardButton("🔄 Refresh", callback_data=f"scorecard:{key}")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("unwatch:"):
        key = data.split(":", 1)[1]
        if chat_id in watched:
            watched[chat_id].pop(key, None)
        await query.edit_message_text("⛔ Tracking band!")


# ─── POLLER ──────────────────────────────────────────────────────────────────

async def poll_scores(app: Application):
    await asyncio.sleep(20)
    while True:
        try:
            if not any(watched.values()):
                await asyncio.sleep(30)
                continue

            matches = await fetch_live_matches()
            match_map = {get_match_id_key(m): m for m in matches}

            for chat_id, tracking in list(watched.items()):
                for key, old_snap in list(tracking.items()):
                    m = match_map.get(key)
                    if not m:
                        del watched[chat_id][key]
                        try:
                            await app.bot.send_message(chat_id, "🏁 *Match khatam!*", parse_mode="Markdown")
                        except Exception:
                            pass
                        continue

                    new_snap = score_snapshot(m)
                    changes = detect_changes(old_snap, new_snap)
                    if changes:
                        watched[chat_id][key] = new_snap
                        full_msg = "\n".join(changes) + "\n\n" + format_live_card(m)
                        if len(full_msg) > 4000:
                            full_msg = full_msg[:4000] + "\n_...truncated_"
                        kb = [[InlineKeyboardButton("📋 Full Scorecard", callback_data=f"scorecard:{key}")]]
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


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("live",     cmd_live))
    app.add_handler(CommandHandler("watching", cmd_watching))
    app.add_handler(CommandHandler("stopall",  cmd_stopall))
    app.add_handler(CallbackQueryHandler(on_button))
    loop = asyncio.get_event_loop()
    loop.create_task(poll_scores(app))
    logger.info("🏏 Cricket Bot chal raha hai (ESPNCricinfo)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
