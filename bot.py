import os
import asyncio
import logging
import aiohttp
import cloudscraper
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CRICAPI_KEY    = os.getenv("CRICAPI_KEY", "")  # optional fallback

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

watched: dict[int, dict[str, dict]] = {}

# ─── API LAYER ───────────────────────────────────────────────────────────────

scraper = cloudscraper.create_scraper()

ESPN_BASE = "https://hs-consumer-api.espncricinfo.com"
ESPN_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "accept": "application/json",
    "referer": "https://www.espncricinfo.com/",
}

CRICAPI_BASE = "https://api.cricapi.com/v1"


def espn_get(path: str, params: dict = None) -> dict:
    url = f"{ESPN_BASE}{path}"
    r = scraper.get(url, headers=ESPN_HEADERS, params=params, timeout=15)
    if r.status_code != 200:
        raise Exception(f"ESPN API error {r.status_code}")
    return r.json()


async def espn_get_async(path: str, params: dict = None) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: espn_get(path, params))


async def cricapi_get(path: str, params: dict = None) -> dict:
    if not CRICAPI_KEY:
        raise Exception("CRICAPI_KEY not set")
    url = f"{CRICAPI_BASE}{path}"
    p = {"apikey": CRICAPI_KEY, **(params or {})}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=p, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                raise Exception(f"CricAPI error {r.status}")
            data = await r.json()
            if data.get("status") != "success":
                raise Exception(f"CricAPI: {data.get('reason', 'unknown error')}")
            return data


# ─── FETCH LIVE MATCHES ──────────────────────────────────────────────────────

async def fetch_live_matches_espn() -> list[dict]:
    data = await espn_get_async(
        "/v1/pages/matches/current",
        params={"lang": "en", "latest": "true"}
    )
    matches = []
    for grp in data.get("matchGroups", []):
        for m in grp.get("matches", []):
            ms = m.get("match", {})
            if ms:
                matches.append(ms)
    return matches


async def fetch_live_matches_cricapi() -> list[dict]:
    data = await cricapi_get("/currentMatches", {"offset": 0})
    return data.get("data", [])


async def fetch_live_matches() -> tuple[list[dict], str]:
    try:
        matches = await fetch_live_matches_espn()
        logger.info(f"ESPN: got {len(matches)} matches")
        return matches, "espn"
    except Exception as e:
        logger.warning(f"ESPN failed ({e}) — trying CricAPI fallback")
        matches = await fetch_live_matches_cricapi()
        logger.info(f"CricAPI: got {len(matches)} matches")
        return matches, "cricapi"


# ─── FETCH SCORECARD ─────────────────────────────────────────────────────────

async def fetch_scorecard_espn(key: str) -> dict | None:
    try:
        series_id, match_id = key.split(":")
        return await espn_get_async(
            "/v1/pages/match/scorecard",
            params={"lang": "en", "seriesId": series_id, "matchId": match_id}
        )
    except Exception:
        return None


async def fetch_scorecard_cricapi(match_id: str) -> dict | None:
    try:
        data = await cricapi_get("/match_scorecard", {"id": match_id})
        return data.get("data")
    except Exception:
        return None


# ─── KEY HELPERS ─────────────────────────────────────────────────────────────

def get_match_key_espn(m: dict) -> str:
    return f"{m.get('series', {}).get('objectId', '')}:{m.get('objectId', '')}"


def get_match_key_cricapi(m: dict) -> str:
    return f"cricapi:{m.get('id', '')}"


# ─── FORMATTERS ──────────────────────────────────────────────────────────────

def format_live_card_espn(m: dict) -> str:
    teams = m.get("teams", [])
    t1 = teams[0].get("team", {}).get("shortName", "T1") if len(teams) > 0 else "T1"
    t2 = teams[1].get("team", {}).get("shortName", "T2") if len(teams) > 1 else "T2"
    fmt = m.get("format", "")
    status = m.get("statusText", m.get("status", ""))
    series = m.get("series", {}).get("name", "")
    innings = m.get("innings", [])
    inn_lines = []
    for inn in innings:
        team = inn.get("team", {}).get("shortName", "?")
        runs = inn.get("runs", 0)
        wkts = inn.get("wickets", 10)
        overs = inn.get("overs", "0")
        score = f"{runs}/{wkts} ({overs} ov)" if wkts < 10 else f"{runs} ({overs} ov)"
        inn_lines.append(f"  {team}: `{score}`")
    inn_text = "\n".join(inn_lines) if inn_lines else "  Scores loading..."
    return f"🏏 *{t1} vs {t2}* — {fmt}\n_{series}_\n{inn_text}\n📊 _{status}_"


def format_live_card_cricapi(m: dict) -> str:
    name = m.get("name", "Match")
    status = m.get("status", "")
    inn_lines = [
        f"  {s.get('inning','')}: `{s.get('r',0)}/{s.get('w',0)} ({s.get('o',0)} ov)`"
        for s in m.get("score", [])
    ]
    inn_text = "\n".join(inn_lines) if inn_lines else "  Scores loading..."
    return f"🏏 *{name}*\n{inn_text}\n📊 _{status}_"


def format_scorecard_espn(sc: dict) -> str:
    if not sc:
        return "❌ Scorecard nahi mila."
    match = sc.get("match", {})
    lines = [
        f"🏏 *{match.get('series', {}).get('name', '')}*",
        f"_{match.get('title', '')}_",
        f"📊 {match.get('statusText', '')}",
        "",
    ]
    for inn in sc.get("scorecard", sc.get("innings", [])):
        team = inn.get("team", {}).get("name", "?")
        runs = inn.get("runs", 0)
        wkts = inn.get("wickets", 0)
        overs = inn.get("overs", 0)
        lines += ["━━━━━━━━━━━━━━━━━━━━━━━━", f"🏏 *{team}*  `{runs}/{wkts}` ({overs} ov)", ""]
        for b in inn.get("batsmen", inn.get("batsman", [])):
            name = b.get("player", {}).get("name", b.get("name", "?"))
            r = b.get("runs", "-")
            balls = b.get("balls", "-")
            fours = b.get("fours", 0)
            sixes = b.get("sixes", 0)
            out = b.get("dismissalText", {}).get("short", b.get("outDesc", ""))
            if out.lower() == "not out":
                icon, dis = "🟢", " *(not out)*"
            elif not out or out.lower() in ("batting", "yet to bat", "did not bat"):
                icon, dis = "🟡", ""
            else:
                icon, dis = "🔴", f"\n       _↳ {out}_"
            try:
                sr = f" SR:{round(int(r)/int(balls)*100,1)}" if int(balls) > 0 else ""
            except Exception:
                sr = ""
            lines.append(f"{icon} `{name:<22}` *{r}* ({balls}b)  4s:{fours} 6s:{sixes}{sr}{dis}")
        lines.append("")
        for bw in inn.get("bowlers", inn.get("bowler", [])):
            name = bw.get("player", {}).get("name", bw.get("name", "?"))
            lines.append(f"⚪️ `{name:<22}` {bw.get('overs',0)}ov  {bw.get('runs',0)}r  *{bw.get('wickets',0)}w*  Eco:{bw.get('economy','-')}")
        lines.append("")
    return "\n".join(lines)


def score_snapshot(m: dict, source: str) -> dict:
    if source == "espn":
        return {
            "source": "espn",
            "status": m.get("statusText", ""),
            "innings": [
                {"runs": i.get("runs"), "wickets": i.get("wickets"), "overs": str(i.get("overs", "0"))}
                for i in m.get("innings", [])
            ],
        }
    else:
        return {
            "source": "cricapi",
            "status": m.get("status", ""),
            "innings": [
                {"runs": s.get("r"), "wickets": s.get("w"), "overs": str(s.get("o", "0"))}
                for s in m.get("score", [])
            ],
        }


def detect_changes(old: dict, new: dict) -> list[str]:
    changes = []
    if old["status"] != new["status"]:
        changes.append(f"📢 *{new['status']}*")
    old_inns, new_inns = old.get("innings", []), new.get("innings", [])
    if len(new_inns) > len(old_inns):
        changes.append("🆕 *New innings shuru!*")
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
        "_Primary: ESPNCricinfo | Fallback: CricAPI_",
        parse_mode="Markdown",
    )


async def cmd_live(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Live matches fetch ho rahe hain...")
    try:
        matches, source = await fetch_live_matches()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return

    if not matches:
        await update.message.reply_text("😴 Koi live match nahi abhi.")
        return

    keyboard = []
    for m in matches[:8]:
        if source == "espn":
            teams = m.get("teams", [])
            t1 = teams[0].get("team", {}).get("shortName", "T1") if len(teams) > 0 else "T1"
            t2 = teams[1].get("team", {}).get("shortName", "T2") if len(teams) > 1 else "T2"
            fmt = m.get("format", "")
            key = get_match_key_espn(m)
        else:
            name = m.get("name", "Match")
            parts = name.split(" vs ")
            t1 = parts[0].strip() if len(parts) > 0 else "T1"
            t2 = parts[1].strip() if len(parts) > 1 else "T2"
            fmt = m.get("matchType", "")
            key = get_match_key_cricapi(m)
        keyboard.append([InlineKeyboardButton(f"🏏 {t1} vs {t2} [{fmt}]", callback_data=f"watch:{key}")])

    src_label = "📡 ESPNCricinfo" if source == "espn" else "📡 CricAPI"
    await update.message.reply_text(
        f"🔴 *{len(matches)} match(es)* — {src_label}\nSelect karo 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_watching(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    w = watched.get(chat_id, {})
    if not w:
        await update.message.reply_text("Koi match track nahi.\n/live use karo!")
        return
    keyboard = [[InlineKeyboardButton(f"⛔ Stop", callback_data=f"unwatch:{k}")] for k in w]
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
        try:
            matches, source = await fetch_live_matches()
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
            return

        if source == "espn":
            m = next((x for x in matches if get_match_key_espn(x) == key), None)
        else:
            m = next((x for x in matches if get_match_key_cricapi(x) == key), None)

        if not m:
            await query.edit_message_text("❌ Match nahi mila.")
            return
        if chat_id not in watched:
            watched[chat_id] = {}
        if key in watched[chat_id]:
            await query.edit_message_text("✅ Already tracked!")
            return

        snap = score_snapshot(m, source)
        watched[chat_id][key] = snap
        card = format_live_card_espn(m) if source == "espn" else format_live_card_cricapi(m)
        kb = [[InlineKeyboardButton("📋 Scorecard", callback_data=f"scorecard:{key}")]]
        await query.edit_message_text(
            f"✅ *Tracking shuru!*\n\n{card}\n\n_Alerts aayenge changes par._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("scorecard:"):
        key = data.split(":", 1)[1]
        await query.edit_message_text("⏳ Scorecard load ho raha hai...")
        if key.startswith("cricapi:"):
            match_id = key.replace("cricapi:", "")
            sc = await fetch_scorecard_cricapi(match_id)
            text = str(sc)[:4000] if sc else "❌ Scorecard nahi mila."
        else:
            sc = await fetch_scorecard_espn(key)
            text = format_scorecard_espn(sc)
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

            matches, source = await fetch_live_matches()
            if source == "espn":
                match_map = {get_match_key_espn(m): m for m in matches}
            else:
                match_map = {get_match_key_cricapi(m): m for m in matches}

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

                    new_snap = score_snapshot(m, source)
                    changes = detect_changes(old_snap, new_snap)
                    if changes:
                        watched[chat_id][key] = new_snap
                        card = format_live_card_espn(m) if source == "espn" else format_live_card_cricapi(m)
                        full_msg = "\n".join(changes) + "\n\n" + card
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
    logger.info("🏏 Bot chal raha hai (ESPN primary + CricAPI fallback)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
