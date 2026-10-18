"""
bot.py - Telegram Mind Map Bot with Render Timeout Protection & Keep-Alive
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    _env_file = Path(".env")
    if _env_file.is_file():
        for _line in _env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from keep_alive import keep_alive
from parser import Node, parse_input
from renderer import FONT_CONFIG, BrowserManager, render_mindmap

BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or ""
).strip()
FONTS_DIR = Path(os.getenv("FONTS_DIR", "fonts")).resolve()
MAX_CONCURRENT_RENDERS = int(os.getenv("MAX_CONCURRENT_RENDERS", "1"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("mindmap_bot")

WAIT_INPUT = 0
WAIT_FONT = 1
_CB_PREFIX = "FONT:"
_TREES_STORE_KEY = "_mindmap_trees"
_MAX_STORED_TREES_PER_USER = 5
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_RENDER_TOTAL_TIMEOUT_SEC = 60.0

_RENDER_SEMAPHORE = asyncio.Semaphore(max(1, MAX_CONCURRENT_RENDERS))


def _esc(value: object) -> str:
    return html.escape(str(value))


def build_font_keyboard(token: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=str(cfg["button_label"]),
            callback_data=f"{_CB_PREFIX}{token}:{font_key}",
        )
        for font_key, cfg in FONT_CONFIG.items()
    ]
    return InlineKeyboardMarkup([buttons])


def _store_user_tree(
    user_data: Dict[str, object],
    tree: Node,
    source_name: str,
) -> str:
    store = user_data.get(_TREES_STORE_KEY)
    if not isinstance(store, dict):
        store = {}
        user_data[_TREES_STORE_KEY] = store

    while len(store) >= _MAX_STORED_TREES_PER_USER:
        oldest_token = next(iter(store))
        store.pop(oldest_token, None)

    token = uuid.uuid4().hex[:16]
    store[token] = {
        "tree": tree,
        "source_name": source_name,
    }
    return token


def _pop_user_tree(
    user_data: Dict[str, object],
    token: str,
) -> Optional[Tuple[Node, str]]:
    store = user_data.get(_TREES_STORE_KEY)
    if not isinstance(store, dict):
        return None
    entry = store.pop(token, None)
    if not isinstance(entry, dict):
        return None
    tree = entry.get("tree")
    source_name = str(entry.get("source_name", "mindmap"))
    if isinstance(tree, Node):
        return tree, source_name
    return None


async def _safe_send(coro_factory, max_retries: int = 2):
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except RetryAfter as exc:
            if attempt >= max_retries:
                raise
            wait_sec = float(exc.retry_after) + 1.0
            await asyncio.sleep(wait_sec)


async def _prompt_font_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tree: Node,
    source_label: str,
    filename_stem: str,
) -> int:
    token = _store_user_tree(context.user_data, tree, filename_stem)
    total_nodes = tree.count_nodes()

    msg_text = (
        f"✅ ورودی <b>{_esc(source_label)}</b> با موفقیت پردازش شد!\n\n"
        f"📌 <b>موضوع مرکزی:</b> <code>{_esc(tree.text)}</code>\n"
        f"🌿 <b>شاخه‌های اصلی:</b> <b>{len(tree.children)}</b> | "
        f"<b>مجموع گره‌ها:</b> <b>{total_nodes}</b>\n\n"
        "لطفاً <b>فونت دلخواه</b> خود را برای رندر نقشه ذهنی انتخاب کنید:"
    )
    await update.effective_message.reply_text(
        text=msg_text,
        parse_mode=ParseMode.HTML,
        reply_markup=build_font_keyboard(token),
    )
    return WAIT_FONT


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    welcome_lines = [
        "👋 سلام! به <b>ربات هوشمند ساخت نقشه ذهنی (Mind Map)</b> خوش آمدید.",
        "",
        "من فایل‌ها و متن‌های ساختاریافته شما را با استایل حرفه‌ای <b>XMind</b> به تصویر باکیفیت (<code>.png</code>) و سند برداری قابل زوم (<code>.pdf</code>) تبدیل می‌کنم.",
        "",
        "📥 <b>روش‌های ارسال ورودی:</b>",
        "1️⃣ ارسال فایل <code>.opml</code> یا <code>.md</code>",
        "2️⃣ ارسال مستقیم <b>متن Markdown</b> در همین چت",
        "",
        "برای لغو عملیات در هر زمان: /cancel",
    ]
    await update.effective_message.reply_text(
        text="\n".join(welcome_lines),
        parse_mode=ParseMode.HTML,
    )
    return WAIT_INPUT


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text(
        text="❌ عملیات لغو شد. هر زمان مایل بودید فایل یا متن جدیدی ارسال کنید.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    doc = message.document if message else None
    if not doc:
        return WAIT_INPUT

    filename = doc.file_name or "mindmap.md"
    ext = Path(filename).suffix.lower()

    if ext not in (".opml", ".md", ".markdown", ".txt", ".xml"):
        await message.reply_text(
            text="⚠️ لطفاً فقط فایل‌های با فرمت <code>.opml</code> یا <code>.md</code> ارسال کنید.",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_INPUT

    if doc.file_size and doc.file_size > _MAX_UPLOAD_BYTES:
        await message.reply_text(
            text="⚠️ حجم فایل ارسالی بیش از حد مجاز (۱۰ مگابایت) است.",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_INPUT

    status_msg = await message.reply_text(
        text="⏳ در حال دریافت و تحلیل ساختار فایل…",
        parse_mode=ParseMode.HTML,
    )

    try:
        tg_file = await doc.get_file()
        raw_bytes = bytes(await tg_file.download_as_bytearray())
        tree = parse_input(raw_bytes, filename=filename)
    except Exception as exc:
        logger.exception("Failed to parse document '%s'", filename)
        await status_msg.edit_text(
            text=f"❌ خطا در خواندن و پردازش فایل:\n<code>{_esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_INPUT

    try:
        await status_msg.delete()
    except TelegramError:
        pass

    clean_stem = Path(filename).stem or "mindmap"
    return await _prompt_font_selection(
        update=update,
        context=context,
        tree=tree,
        source_label=filename,
        filename_stem=clean_stem,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    raw_text = ((message.text if message else "") or "").strip()
    if not raw_text:
        return WAIT_INPUT

    try:
        tree = parse_input(raw_text, filename="mindmap.md")
    except Exception as exc:
        logger.warning("Text parse error: %s", exc)
        await message.reply_text(
            text=f"❌ خطا در پردازش متن ارسالی:\n<code>{_esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return WAIT_INPUT

    return await _prompt_font_selection(
        update=update,
        context=context,
        tree=tree,
        source_label="متن مستقیم",
        filename_stem="mindmap",
    )


async def handle_font_selection(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query:
        return WAIT_INPUT

    await query.answer()
    data_str = query.data or ""

    parts = data_str.split(":", 2)
    if len(parts) != 3 or parts[0] != "FONT":
        if query.message:
            await query.message.reply_text(
                text="⚠️ درخواست نامعتبر است.",
                parse_mode=ParseMode.HTML,
            )
        return WAIT_INPUT

    _, token, font_key = parts
    popped = _pop_user_tree(context.user_data, token)

    if popped is None:
        if query.message:
            await query.message.edit_text(
                text="⚠️ این نقشه قبلاً پردازش شده یا منقضی شده است. لطفاً فایل یا متن را مجدداً ارسال کنید.",
                parse_mode=ParseMode.HTML,
            )
        return WAIT_INPUT

    tree, source_stem = popped
    font_cfg = FONT_CONFIG.get(font_key, FONT_CONFIG["font_a"])
    font_label = str(font_cfg["button_label"])

    status_msg = query.message
    if status_msg:
        try:
            await status_msg.edit_text(
                text=(
                    f"🎨 فونت <b>{_esc(font_label)}</b> انتخاب شد.\n"
                    "⚙️ در حال ترسیم گراف برداری و رندر خروجی‌های PNG و PDF…"
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            status_msg = await query.message.reply_text(
                text="⚙️ در حال رندر نقشه ذهنی…",
                parse_mode=ParseMode.HTML,
            )

    try:
        async with _RENDER_SEMAPHORE:
            result = await asyncio.wait_for(
                render_mindmap(
                    root=tree,
                    font_key=font_key,
                    fonts_dir=FONTS_DIR,
                ),
                timeout=_RENDER_TOTAL_TIMEOUT_SEC,
            )
    except asyncio.TimeoutError:
        logger.error("Render timed out after %s seconds", _RENDER_TOTAL_TIMEOUT_SEC)
        await BrowserManager.shutdown()
        if status_msg:
            await status_msg.edit_text(
                text="⏱ زمان رندر بیش از حد مجاز طول کشید. مرورگر بازنشانی شد؛ لطفاً مجدداً تلاش کنید.",
                parse_mode=ParseMode.HTML,
            )
        return WAIT_INPUT
    except Exception as exc:
        logger.exception("Playwright rendering failed")
        if status_msg:
            await status_msg.edit_text(
                text=f"❌ خطا در هنگام رندر نقشه ذهنی:\n<code>{_esc(exc)}</code>",
                parse_mode=ParseMode.HTML,
            )
        return WAIT_INPUT

    if status_msg:
        try:
            await status_msg.edit_text(
                text="📤 در حال آپلود فایل‌های خروجی در تلگرام…",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

    target_msg = query.message
    if target_msg:
        if (result.width + result.height) < 9500 and len(result.png_bytes) < int(9.5 * 1024 * 1024):
            try:
                await _safe_send(
                    lambda: target_msg.reply_photo(
                        photo=InputFile(
                            io.BytesIO(result.png_bytes),
                            filename=f"{source_stem}_preview.png",
                        ),
                        caption=(
                            f"🖼 <b>پیش‌نمایش نقشه ذهنی</b>\n"
                            f"📌 موضوع: <b>{_esc(tree.text)}</b>\n"
                            f"🔤 فونت: <b>{_esc(result.font_label)}</b>"
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                )
            except Exception as photo_exc:
                logger.warning("Skipping photo preview: %s", photo_exc)

        await _safe_send(
            lambda: target_msg.reply_document(
                document=InputFile(
                    io.BytesIO(result.png_bytes),
                    filename=f"{source_stem}_HighRes.png",
                ),
                caption=(
                    f"📎 فایل تصویر اصلی بدون افت کیفیت "
                    f"(<code>PNG — {result.width}×{result.height}px</code>)"
                ),
                parse_mode=ParseMode.HTML,
            )
        )

        await _safe_send(
            lambda: target_msg.reply_document(
                document=InputFile(
                    io.BytesIO(result.pdf_bytes),
                    filename=f"{source_stem}_Vector.pdf",
                ),
                caption="📄 نسخه برداری قابل زوم و چاپ (<code>Vector PDF</code>)",
                parse_mode=ParseMode.HTML,
            )
        )

    if status_msg:
        try:
            await status_msg.delete()
        except TelegramError:
            pass

    return WAIT_INPUT


async def on_startup(app: Application) -> None:
    logger.info("Initializing Playwright BrowserManager...")
    await BrowserManager.initialize(fonts_dir=FONTS_DIR)
    logger.info("Playwright BrowserManager is ready.")


async def on_shutdown(app: Application) -> None:
    logger.info("Shutting down Playwright BrowserManager...")
    await BrowserManager.shutdown()


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        raise RuntimeError(
            "متغیر TELEGRAM_BOT_TOKEN تنظیم نشده است. لطفاً در بخش Environment سایت Render آن را وارد کنید."
        )

    keep_alive()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60.0)
        .write_timeout(60.0)
        .connect_timeout(30.0)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    doc_handler = MessageHandler(filters.Document.ALL, handle_document)
    text_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    font_handler = CallbackQueryHandler(
        handle_font_selection,
        pattern=rf"^{_CB_PREFIX}[0-9a-f]+:(?:{'|'.join(FONT_CONFIG.keys())})$",
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            doc_handler,
            text_handler,
        ],
        states={
            WAIT_INPUT: [
                doc_handler,
                text_handler,
            ],
            WAIT_FONT: [
                font_handler,
                doc_handler,
                text_handler,
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )

    app.add_handler(conv)
    logger.info("Starting Telegram Mind Map Bot polling...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
