"""bot.py — Telegram mind-map bot (async, freeze-free, production-ready)."""
import asyncio
import io
import json
import logging
import os

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from parser import parse_mindmap_text
from renderer import build_xmind

try:
    from keep_alive import keep_alive
    keep_alive()
except ImportError:
    pass

logging.basicConfig(
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

SYSTEM_PROMPT = """You are a mind map architect. Given a topic, produce a structured mind map as a single valid JSON object (no markdown, no code fences, no extra text).

The JSON must follow this exact schema:
{
  "title": "<central topic text>",
  "branches": [
    {
      "title": "<main branch text>",
      "children": [
        {
          "title": "<sub-topic text>",
          "children": [
            {"title": "<leaf text>", "children": []}
          ]
        }
      ]
    }
  ]
}

Rules:
- 4 to 6 main branches maximum.
- Maximum 4 levels deep (branch → child → grandchild → leaf).
- All titles concise: 2–6 words.
- Output ONLY the JSON object. No explanation, no code block markers.
- structureClass must be org.xmind.ui.map.clockwise
- All line-class values must be org.xmind.branchConnection.elbow
- All shape-class values must be org.xmind.topicShape.roundedRect
"""


async def _call_openai(topic: str) -> dict:
    """Non-blocking OpenAI call; returns parsed JSON dict."""
    import openai

    client = openai.OpenAI(api_key=OPENAI_API_KEY)

    def _sync() -> dict:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": topic},
            ],
            temperature=0.4,
            max_tokens=1200,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        return json.loads(raw)

    return await asyncio.to_thread(_sync)


async def _generate_xmind(topic: str) -> bytes:
    data = await asyncio.wait_for(_call_openai(topic), timeout=50)
    title = data.get("title", topic)
    branches = data.get("branches", [])
    return await asyncio.to_thread(build_xmind, title, branches)


# ── handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🗺 ربات نقشه‌ذهنی

"
        "یک موضوع بنویس تا برات نقشه‌ذهنی XMind بسازم.
"
        "مثال: مدیریت زمان"
    )


async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "راهنما:
"
        "• هر موضوعی بنویس → فایل .xmind دریافت کن
"
        "• فایل را در نرم‌افزار XMind باز کن
"
        "• /start — شروع مجدد"
    )


async def handle_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    topic = (update.message.text or "").strip()
    if not topic:
        return

    status = await update.message.reply_text("⏳ در حال ساخت نقشه‌ذهنی…")

    try:
        xmind_bytes = await _generate_xmind(topic)
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in topic)
        filename = f"{safe_name[:40].strip()}.xmind"
        await update.message.reply_document(
            document=io.BytesIO(xmind_bytes),
            filename=filename,
            caption="✅ نقشه‌ذهنی آماده است.",
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("⚠️ درخواست بیش از حد طول کشید. دوباره امتحان کن.")
    except Exception as exc:
        logger.exception("Generation failed for topic %r: %s", topic, exc)
        await update.message.reply_text(f"❌ خطا رخ داد: {exc}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_topic))
    logger.info("Bot polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
