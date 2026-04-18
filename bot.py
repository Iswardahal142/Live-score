import os
import asyncio
import logging
import aiohttp
import json
import cloudscraper
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
CRICAPI_KEY       = os.getenv("CRICAPI_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

watched: dict[int, dict[str, dict]] = {}

# ─── SOURCE TRACKER ──────────────────────────────────────────────────────────
# Tracks which source is currently working so poller uses same source
current_source = {"name": "espn"}  # espn | cricapi | ai

# ─── ESPN (cloudscraper) ─────────────────────────────────────────────────────

scraper = cloudscraper.create_scraper()

ESPN_BASE = "https://hs-consumer-api.espncricinfo.com"
ESPN_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "accept": "application/json",
    "referer": "https://www.espncricinfo.com/",
}


def espn_get_sync(path: str, params: dict = None) -> dict:
    r = scraper.get(f"{ESPN_BASE}{path}", headers=ESPN_HEADERS, params=params, timeout=15)
    if r.status_code != 200:
        raise Exception(f"ESPN {r.status_code}")
    return r.json()


async def espn_get(path: str, params: dict = None) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: espn_get_sync(path, params))


async def fetch_espn_matches() -> list[dict]:
    data = await espn_get("/v1/pages/matches/current", params={"lang": "en", "latest": "true"})
    matches = []
    for grp in data.get("matchGroups", []):
        for m in grp.get("matches", []):
            ms = m.get("match", {})
            if ms:
                matches.append(ms)
    return matches


# ─── CRICAPI ─────────────────────────────────────────────────────────────────

async def fetch_cricapi_matches() -> list[dict]:
    if not CRICAPI_KEY:
        raise Exception("No CRICAPI_KEY")
    url = f"https://api.cricapi.com/v1/currentMatches"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params={"apikey": CRICAPI_KEY, "offset": 0},
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json()
            if data.get("status") != "success":
                raise Exception(f"CricAPI: {data.get('reason','error')}")
            return data.get("data", [])


# ─── OPENROUTER AI ───────────────────────────────────────────────────────────

AI_SYSTEM_PROMPT = """You are a cricket live score assistant. 
When asked for live cricket matches, respond ONLY with a valid JSON array like this:
[
  {
    "id": "unique_match_id",
    "name": "India vs Australia, 1st ODI",
    "t1": "IND",
    "t2": "AUS",
    "format": "ODI",
    "status": "India need 45 runs from 30 balls",
    "score": [
      {"inning": "Australia Innings", "r": 287, "w": 6, "o": 50},
      {"inning": "India Innings", "r": 243, "w": 3, "o": 44.2}
    ]
  }
]
Only include matches that are currently live or in progress. 
If no matches are live, return an empty array [].
Return ONLY the JSON, no markdown, no explanation."""

AI_SCORE_PROMPT = """You are a cricket live score assistant.
Given a match name/id, return the current detailed scorecard as JSON:
{
  "match": "India vs Australia, 1st ODI",
  "status": "India won by 5 wickets",
  "innings": [
    {
      "team": "Australia",
      "runs": 287,
      "wickets": 6,
      "overs": 50,
      "batsmen": [
        {"name": "Steve Smith", "runs": 89, "balls": 95, "fours": 8, "sixes": 1, "out": "c Kohli b Bumrah"}
      ],
      "bowlers": [
        {"name": "Jasprit Bumrah", "overs": 10, "runs": 45, "wickets": 3, "economy": 4.5}
      ]
    }
  ]
}
Return ONLY valid JSON, no markdown, no explanation."""


async def ai_fetch_live_matches() -> list[dict]:
    if not OPENROUTER_API_KEY:
        raise Exception("No OPENROUTER_API_KEY")
    
    payload = {
        "model": "openai/gpt-4.1-nano",
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": "What cricket matches are live right now? Give me current live scores."}
        ],
        "max_tokens": 2000,
    }
    
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://cricket-bot.railway.app",
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                raise Exception(f"OpenRouter error {r.status}")
            data = await r.json()
    
    content = data["choices"][0]["message"]["content"].strip()
    # Strip markdown code blocks if present
    content = content.replace("```json", "").replace("```", "").strip()
    matches = json.loads(content)
    return matches


async def ai_fetch_scorecard(match_name: str) -> str:
    if not OPENROUTER_API_KEY:
        return "❌ AI not configured."
    
    payload = {
        "model": "openai/gpt-4.1-nano",
        "messages": [
            {"role": "system", "content": AI_SCORE_PROMPT},
            {"role": "user", "content": f"Give me the current scorecard for: {match_name}"}
        ],
        "max_tokens": 2000,
    }
    
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://cricket-bot.railway.app",
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                return "❌ AI scorecard fetch failed."
            data = await r.json()
    
    content = data["choices"][0]["message"]["content"].strip()
    content = content.replace("```json", "").replace("```", "").strip()
    
    try:
        sc = json.loads(content)
        return format_scorecard_ai(sc)
    except Exception:
        return content[:4000]


def format_scorecard_ai(sc: dict) -> str:
    lines = [
        f"🏏 *{sc.get('match', 'Match')}*",
        f"📊 _{sc.get('status', '')}_",
        "",
    ]
    for inn in sc.get("innings", []):
        team = inn.get("team", "?")
        runs = inn.get("runs", 0)
        wkts = inn.get("wickets", 0)
        overs = inn.get("overs", 0)
        lines += ["━━━━━━━━━━━━━━━━━━━━━━━━", f"🏏 *{team}*  `{runs}/{wkts}` ({overs} ov)", ""]
        for b in inn.get("batsmen", []):
            name = b.get("name", "?")
            r = b.get("runs", "-")
            balls = b.get("balls", "-")
            fours = b.get("fours", 0)
            sixes = b.get("sixes", 0)
            out = b.get("out", "")
            if out.lower() == "not out":
                icon, dis = "🟢", " *(not out)*"
            elif not out:
                icon, dis = "🟡", ""
            else:
                icon, dis = "🔴", f"\n       _↳ {out}_"
            try:
                sr = f" SR:{round(int(r)/int(balls)*100,1)}" if int(balls) > 0 else ""
            except Exception:
                sr = ""
            lines.append(f"{icon} `{name:<22}` *{r}* ({balls}b)  4s:{fours} 6s:{sixes}{sr}{dis}")
        lines.append("")
        for bw in inn.get("bowlers", []):
            name = bw.get("name", "?")
            lines.append(
                f"⚪️ `{name:<22}` {bw.get('overs',0)}ov  "
                f"{bw.get('runs',0)}r  *{bw.get('wickets',0)}w*  Eco:{bw.get('economy','-')}"
            )
        lines.append("")
    lines.append("_⚠️ AI-generated scores — verify for accuracy_")
    return "\n".join(lines)


# ─── UNIFIED FETCH ───────────────────────────────────────────────────────────

async def fetch_live_matches() -> tuple[list[dict], str]:
    # 1. Try ESPN
    try:
        matches = await fetch_espn_matches()
        logger.info(f"✅ ESPN: {len(matches)} matches")
        current_source["name"] = "espn"
        return matches, "espn"
    except Exception as e:
        logger.warning(f"ESPN failed: {e}")

    # 2. Try CricAPI
    try:
        matches = await fetch_cricapi_matches()
        logger.info(f"✅ CricAPI: {len(matches)} matches")
        current_source["name"] = "cricapi"
        return matches, "cricapi"
    except Exception as e:
        logger.warning(f"CricAPI failed: {e}")

    # 3. AI fallback
    logger.info("Using AI fallback (OpenRouter GPT-4.1-nano)...")
    matches = await ai_fetch_live_matches()
    logger.info(f"✅ AI: {len(matches)} matches")
    current_source["name"] = "ai"
    return matches, "ai"


# ─── KEY HELPERS ─────────────────────────────────────────────────────────────

def get_key(m: dict, source: str) -> str:
    if source == "espn":
        return f"espn:{m.get('series',{}).get('objectId','')}:{m.get('objectId','')}"
    elif source == "cricapi":
        return f"cricapi:{m.get('id','')}"
    else:  # ai
        return f"ai:{m.get('id', m.get('name','match').replace(' ','_'))}"


# ─── FORMATTERS ──────────────────────────────────────────────────────────────

def format_live_card(m: dict, source: str) -> str:
    if source == "espn":
        teams = m.get("teams", [])
        t1 = teams[0].get("team", {}).get("shortName", "T1") if len(teams) > 0 else "T1"
        t2 = teams[1].get("team", {}).get("shortName", "T2") if len(teams) > 1 else "T2"
        fmt = m.get("format", "")
        status = m.get("statusText", "")
        series = m.get("series", {}).get("name", "")
        inn_lines = []
        for inn in m.get("innings", []):
            team = inn.get("team", {}).get("shortName", "?")
            r = inn.get("runs", 0)
            w = inn.get("wickets", 10)
            ov = inn.get("overs", "0")
            inn_lines.append(f"  {team}: `{r}/{w} ({ov} ov)`")
        inn_text = "\n".join(inn_lines) or "  Scores loading..."
        return f"🏏 *{t1} vs {t2}* — {fmt}\n_{series}_\n{inn_text}\n📊 _{status}_"

    elif source == "cricapi":
        name = m.get("name", "Match")
        status = m.get("status", "")
        inn_lines = [
            f"  {s.get('inning','')}: `{s.get('r',0)}/{s.get('w',0)} ({s.get('o',0)} ov)`"
            for s in m.get("score", [])
        ]
        inn_text = "\n".join(inn_lines) or "  Scores loading..."
        return f"🏏 *{name}*\n{inn_text}\n📊 _{status}_"

    else:  # ai
        name = m.get("name", "Match")
        status = m.get("status", "")
        inn_lines = [
            f"  {s.get('inning','')}: `{s.get('r',0)}/{s.get('w',0)} ({s.get('o',0)} ov)`"
            for s in m.get("score", [])
        ]
        inn_text = "\n".join(inn_lines) or "  Scores loading..."
        return f"🏏 *{name}*\n{inn_text}\n📊 _{status}_\n_🤖 via AI_"


def score_snapshot(m: dict, source: str) -> dict:
    if source == "espn":
        innings = [
            {"runs": i.get("runs"), "wickets": i.get("wickets"), "overs": str(i.get("overs", "0"))}
            for i in m.get("innings", [])
        ]
        return {"source": source, "status": m.get("statusText", ""), "innings": innings}
    elif source == "cricapi":
        innings = [
            {"runs": s.get("r"), "wickets": s.get("w"), "overs": str(s.get("o", "0"))}
            for s in m.get("score", [])
        ]
        return {"source": source, "status": m.get("status", ""), "innings": innings}
    else:  # ai
        innings = [
            {"runs": s.get("r"), "wickets": s.get("w"), "overs": str(s.get("o", "0"))}
            for s in m.get("score", [])
        ]
        return {"source": source, "status": m.get("status", ""), "innings": innings}


def detect_changes(old: dict, new: dict) -> list[str]:
    changes = []
    if old["status"] != new["status"]:
        changes.append(f"📢 *{new['status']}*")
    old_inns, new_inns = old.get("innings", []), new.get("innings", [])
    if len(new_inns) > len(old_inns):
        changes.append("🆕 *New innings shuru!*")
    for i, (o, n) in enumerate(zip(old_inns, new_inns)):
        or_, ow = o.get("runs"), o.get("wickets")
        nr, nw = n.get("runs"), n.get("wickets")
        oo, no = o.get("overs", "0"), n.get("overs", "0")
        if None in (or_, ow, nr, nw):
            continue
        label = f"Inn {i+1}"
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
    sources = "ESPN → CricAPI → 🤖 AI (GPT-4.1-nano)"
    await update.message.reply_text(
        "👋 *Cricket Live Bot*\n\n"
        "🏏 /live — Live matches dekho\n"
        "👁 /watching — Tracked matches\n"
        "⛔ /stopall — Sab tracking band karo\n\n"
        f"_Sources: {sources}_",
        parse_mode="Markdown",
    )


async def cmd_live(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Live matches fetch ho rahe hain...")
    try:
        matches, source = await fetch_live_matches()
    except Exception as e:
        await update.message.reply_text(f"❌ Sab sources fail ho gaye:\n{e}")
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
        else:
            name = m.get("name", "Match")
            parts = name.split(" vs ")
            t1 = parts[0].strip()[:10] if parts else "T1"
            t2 = parts[1].strip()[:10] if len(parts) > 1 else "T2"
            fmt = m.get("format", m.get("matchType", ""))
        key = get_key(m, source)
        keyboard.append([InlineKeyboardButton(f"🏏 {t1} vs {t2} [{fmt}]", callback_data=f"watch:{key}")])

    src_icons = {"espn": "📡 ESPNCricinfo", "cricapi": "📡 CricAPI", "ai": "🤖 AI (GPT-4.1-nano)"}
    await update.message.reply_text(
        f"🔴 *{len(matches)} match(es)* — {src_icons.get(source, source)}\nSelect karo 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_watching(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    w = watched.get(chat_id, {})
    if not w:
        await update.message.reply_text("Koi match track nahi.\n/live use karo!")
        return
    keyboard = [[InlineKeyboardButton("⛔ Stop", callback_data=f"unwatch:{k}")] for k in w]
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
        source_prefix = key.split(":")[0]  # espn | cricapi | ai

        try:
            matches, source = await fetch_live_matches()
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
            return

        m = next((x for x in matches if get_key(x, source) == key), None)
        if not m:
            await query.edit_message_text("❌ Match nahi mila. /live dobara try karo.")
            return
        if chat_id not in watched:
            watched[chat_id] = {}
        if key in watched[chat_id]:
            await query.edit_message_text("✅ Already tracked!")
            return

        watched[chat_id][key] = score_snapshot(m, source)
        card = format_live_card(m, source)
        kb = [[InlineKeyboardButton("📋 Scorecard", callback_data=f"scorecard:{key}")]]
        await query.edit_message_text(
            f"✅ *Tracking shuru!*\n\n{card}\n\n_Alerts aayenge changes par._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("scorecard:"):
        key = data.split(":", 1)[1]
        source_prefix = key.split(":")[0]
        await query.edit_message_text("⏳ Scorecard load ho raha hai...")

        if source_prefix == "cricapi":
            match_id = key.replace("cricapi:", "")
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        "https://api.cricapi.com/v1/match_scorecard",
                        params={"apikey": CRICAPI_KEY, "id": match_id},
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as r:
                        d = await r.json()
                        text = str(d.get("data", "No data"))[:4000]
            except Exception as e:
                text = f"❌ Scorecard error: {e}"

        elif source_prefix == "espn":
            parts = key.split(":")  # espn:seriesId:matchId
            try:
                sc = await espn_get(
                    "/v1/pages/match/scorecard",
                    params={"lang": "en", "seriesId": parts[1], "matchId": parts[2]}
                )
                # Basic format
                match = sc.get("match", {})
                lines = [f"🏏 *{match.get('title','')}*", f"📊 {match.get('statusText','')}", ""]
                for inn in sc.get("scorecard", []):
                    team = inn.get("team", {}).get("name", "?")
                    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━")
                    lines.append(f"*{team}*  `{inn.get('runs',0)}/{inn.get('wickets',0)}` ({inn.get('overs',0)} ov)")
                text = "\n".join(lines)[:4000]
            except Exception as e:
                text = f"❌ Scorecard error: {e}"

        else:  # ai
            match_name = key.replace("ai:", "").replace("_", " ")
            text = await ai_fetch_scorecard(match_name)

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
            match_map = {get_key(m, source): m for m in matches}

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
                        card = format_live_card(m, source)
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
    logger.info("🏏 Bot chal raha hai! Sources: ESPN → CricAPI → AI")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
