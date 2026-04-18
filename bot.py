import os
import asyncio
import logging
import aiohttp
import json
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

watched: dict[int, dict[str, dict]] = {}

# ─── AI CALL ─────────────────────────────────────────────────────────────────

async def ask_ai(system: str, user: str) -> str:
    if not OPENROUTER_API_KEY:
        raise Exception("OPENROUTER_API_KEY not set in environment variables!")
    
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-4.1-nano",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "max_tokens": 2000,
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                text = await r.text()
                raise Exception(f"OpenRouter error {r.status}: {text}")
            data = await r.json()
    
    content = data["choices"][0]["message"]["content"].strip()
    content = content.replace("```json", "").replace("```", "").strip()
    return content


# ─── FETCH LIVE MATCHES ──────────────────────────────────────────────────────

LIVE_SYSTEM = """You are a cricket live score assistant with up-to-date knowledge.
Return ONLY a valid JSON array of currently live/in-progress cricket matches.
Format:
[
  {
    "id": "ind_vs_aus_1odi",
    "name": "India vs Australia, 1st ODI",
    "t1": "IND",
    "t2": "AUS",
    "format": "ODI",
    "series": "Australia tour of India 2025",
    "status": "India need 45 runs from 30 balls",
    "score": [
      {"inning": "Australia Innings", "r": 287, "w": 6, "o": 50.0},
      {"inning": "India Innings",     "r": 243, "w": 3, "o": 44.2}
    ]
  }
]
Rules:
- Only include matches that are LIVE right now.
- If no matches are live, return [].
- Return ONLY the JSON array. No markdown, no explanation."""


async def ai_fetch_live_matches() -> list[dict]:
    content = await ask_ai(LIVE_SYSTEM, "What cricket matches are live right now? Give current scores.")
    return json.loads(content)


# ─── FETCH SCORECARD ─────────────────────────────────────────────────────────

SCORECARD_SYSTEM = """You are a cricket scorecard assistant.
Given a match name, return a detailed current scorecard as JSON:
{
  "match": "India vs Australia, 1st ODI",
  "series": "Australia tour of India 2025",
  "status": "India need 45 runs from 30 balls",
  "innings": [
    {
      "team": "Australia",
      "runs": 287,
      "wickets": 6,
      "overs": 50.0,
      "batsmen": [
        {"name": "Steve Smith", "runs": 89, "balls": 95, "fours": 8, "sixes": 1, "out": "c Kohli b Bumrah"}
      ],
      "bowlers": [
        {"name": "Jasprit Bumrah", "overs": 10.0, "runs": 45, "wickets": 3, "economy": 4.5}
      ]
    }
  ]
}
Return ONLY the JSON. No markdown, no explanation."""


async def ai_fetch_scorecard(match_name: str) -> str:
    try:
        content = await ask_ai(SCORECARD_SYSTEM, f"Give me the current scorecard for: {match_name}")
        sc = json.loads(content)
        return format_scorecard(sc)
    except Exception as e:
        logger.error(f"Scorecard error: {e}")
        return "❌ Scorecard fetch nahi hua."


# ─── FORMATTERS ──────────────────────────────────────────────────────────────

def format_live_card(m: dict) -> str:
    name   = m.get("name", "Match")
    status = m.get("status", "")
    series = m.get("series", "")
    inn_lines = [
        f"  {s.get('inning','')}: `{s.get('r',0)}/{s.get('w',0)} ({s.get('o',0)} ov)`"
        for s in m.get("score", [])
    ]
    inn_text = "\n".join(inn_lines) if inn_lines else "  Scores loading..."
    return (
        f"🏏 *{name}*\n"
        f"_{series}_\n"
        f"{inn_text}\n"
        f"📊 _{status}_"
    )


def format_scorecard(sc: dict) -> str:
    lines = [
        f"🏏 *{sc.get('match', 'Match')}*",
        f"_{sc.get('series', '')}_",
        f"📊 _{sc.get('status', '')}_",
        "",
    ]
    for inn in sc.get("innings", []):
        team  = inn.get("team", "?")
        runs  = inn.get("runs", 0)
        wkts  = inn.get("wickets", 0)
        overs = inn.get("overs", 0)
        lines += ["━━━━━━━━━━━━━━━━━━━━━━━━", f"🏏 *{team}*  `{runs}/{wkts}` ({overs} ov)", ""]

        for b in inn.get("batsmen", []):
            name  = b.get("name", "?")
            r     = b.get("runs", "-")
            balls = b.get("balls", "-")
            fours = b.get("fours", 0)
            sixes = b.get("sixes", 0)
            out   = b.get("out", "")
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

    lines.append("_⚠️ AI-generated — for reference only_")
    return "\n".join(lines)


def score_snapshot(m: dict) -> dict:
    return {
        "name": m.get("name", ""),
        "status": m.get("status", ""),
        "innings": [
            {"r": s.get("r"), "w": s.get("w"), "o": str(s.get("o", "0"))}
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
        or_, ow = o.get("r"), o.get("w")
        nr, nw  = n.get("r"), n.get("w")
        oo, no  = o.get("o", "0"), n.get("o", "0")
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
    await update.message.reply_text(
        "👋 *Cricket Live Bot* 🤖\n\n"
        "🏏 /live — Live matches dekho\n"
        "👁 /watching — Tracked matches\n"
        "⛔ /stopall — Sab tracking band karo\n\n"
        "_Powered by OpenRouter GPT-4.1-nano_",
        parse_mode="Markdown",
    )


async def cmd_live(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 AI se live matches fetch ho rahe hain...")
    try:
        matches = await ai_fetch_live_matches()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return

    if not matches:
        await update.message.reply_text("😴 AI ke according koi live match nahi abhi.")
        return

    keyboard = []
    for m in matches[:8]:
        t1  = m.get("t1", "T1")
        t2  = m.get("t2", "T2")
        fmt = m.get("format", "")
        mid = m.get("id", m.get("name", "").replace(" ", "_"))
        keyboard.append([InlineKeyboardButton(f"🏏 {t1} vs {t2} [{fmt}]", callback_data=f"watch:{mid}")])

    await update.message.reply_text(
        f"🔴 *{len(matches)} live match(es)* — 🤖 AI\nSelect karo 👇",
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
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data    = query.data

    if data.startswith("watch:"):
        mid = data.split(":", 1)[1]
        try:
            matches = await ai_fetch_live_matches()
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
            return

        m = next((x for x in matches if x.get("id", x.get("name","").replace(" ","_")) == mid), None)
        if not m:
            await query.edit_message_text("❌ Match nahi mila. /live dobara try karo.")
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
            f"✅ *Tracking shuru!*\n\n{card}\n\n_Score change hone par alert aayega._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("scorecard:"):
        mid = data.split(":", 1)[1]
        await query.edit_message_text("🤖 AI scorecard bana raha hai...")
        match_name = mid.replace("_", " ")
        text = await ai_fetch_scorecard(match_name)
        if len(text) > 4000:
            text = text[:4000] + "\n_...truncated_"
        kb = [[InlineKeyboardButton("🔄 Refresh", callback_data=f"scorecard:{mid}")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("unwatch:"):
        mid = data.split(":", 1)[1]
        if chat_id in watched:
            watched[chat_id].pop(mid, None)
        await query.edit_message_text("⛔ Tracking band!")


# ─── POLLER ──────────────────────────────────────────────────────────────────

async def poll_scores(app: Application):
    await asyncio.sleep(30)
    while True:
        try:
            if not any(watched.values()):
                await asyncio.sleep(60)
                continue

            matches = await ai_fetch_live_matches()
            match_map = {m.get("id", m.get("name","").replace(" ","_")): m for m in matches}

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
                        full_msg = "\n".join(changes) + "\n\n" + format_live_card(m)
                        if len(full_msg) > 4000:
                            full_msg = full_msg[:4000] + "\n_...truncated_"
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
        await asyncio.sleep(60)  # 60s — AI rate limit ka dhyan rakhte hue


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
    logger.info("🤖 Cricket AI Bot chal raha hai! (OpenRouter GPT-4.1-nano)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
