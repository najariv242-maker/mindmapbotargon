"""bot.py — Telegram Mind-Map Bot (aiogram 3.13+, async polling).

Flow:
  /start          -> Persian guide
  .md/.opml file  -> bounded download -> pending store -> font inline keyboard
  raw text        -> same as markdown
  callback        -> parse + render (Playwright, in-memory PNG/PDF) -> send back

Safety: user & document byte limits, HTML escaping for all Telegram messages,
semaphore-bounded concurrency, RetryAfter/Forbidden/BadRequest handling,
no secrets in logs, startup/shutdown lifecycle, no temp files on disk.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import CommandStart
from aiogram import Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import parser as mdparser
from renderer import FONT_CONFIG, Renderer, RenderConfig, RenderError

# --------------------------------------------------------------------------- #
# Configuration (env, no secrets logged)
# --------------------------------------------------------------------------- #

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("BOT_TOKEN", "")).strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_DOCUMENT_BYTES = int(os.getenv("MAX_DOCUMENT_BYTES", str(512 * 1024)))
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "20000"))
MAX_PENDING_PER_USER = int(os.getenv("MAX_PENDING_PER_USER", "3"))
PENDING_TTL_SECONDS = int(os.getenv("PENDING_TTL_SECONDS", "1800"))
MAX_CONCURRENT_RENDERS = int(os.getenv("MAX_CONCURRENT_RENDERS", "2"))
PNG_SCALE = int(os.getenv("PNG_DEVICE_SCALE_FACTOR", "3"))

RENDER_CONFIG = RenderConfig(
    max_concurrent_renders=MAX_CONCURRENT_RENDERS,
    png_device_scale_factor=PNG_SCALE,
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")
logging.getLogger("aiogram").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Pending store (in-memory, TTL + quota)
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Pending:
    user_id: int
    content: bytes
    kind: str                      # "md" | "opml"
    title: str
    created_at: float = field(default_factory=time.monotonic)


class PendingStore:
    def __init__(self, ttl: int = PENDING_TTL_SECONDS,
                 per_user: int = MAX_PENDING_PER_USER) -> None:
        self._data: dict[str, Pending] = {}
        self._ttl = ttl
        self._per_user = per_user

    def add(self, user_id: int, content: bytes, kind: str, title: str) -> str:
        self.prune()
        owned = [k for k, v in self._data.items() if v.user_id == user_id]
        while len(owned) >= self._per_user:
            oldest = min(owned, key=lambda k: self._data[k].created_at)
            del self._data[oldest]
            owned.remove(oldest)
        pid = uuid.uuid4().hex[:12]
        self._data[pid] = Pending(user_id, content, kind, title)
        return pid

    def get(self, pid: str, user_id: int) -> Pending | None:
        item = self._data.get(pid)
        if item is None or item.user_id != user_id:
            return None
        if time.monotonic() - item.created_at > self._ttl:
            del self._data[pid]
            return None
        return item

    def drop(self, pid: str) -> None:
        self._data.pop(pid, None)

    def prune(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._data.items()
                  if now - v.created_at > self._ttl]:
            del self._data[k]


STORE = PendingStore()


# --------------------------------------------------------------------------- #
# Safe Telegram send helpers (RetryAfter / Forbidden / BadRequest)
# --------------------------------------------------------------------------- #

async def safe_send(bot: Bot, chat_id: int, **kwargs) -> Message | None:
    """Send with flood-wait retry, silently skip blocked/dead chats."""
    for attempt in range(3):
        try:
            return await bot.send_document(chat_id=chat_id, **kwargs)
        except TelegramRetryAfter as exc:
            logger.warning("FloodWait %ss; sleeping.", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramForbiddenError:
            logger.info("Chat %s blocked the bot; skipping.", chat_id)
            return None
        except TelegramBadRequest as exc:
            logger.warning("BadRequest while sending: %s", type(exc).__name__)
            return None
        except (ConnectionError, asyncio.TimeoutError) as exc:
            logger.warning("Network error during send: %s", type(exc).__name__)
            await asyncio.sleep(2 ** attempt)
    return None


async def safe_edit(callback: CallbackQuery, text: str,
                    reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await callback.message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
    except TelegramBadRequest:
        pass  # e.g. "message is not modified"
    except TelegramForbiddenError:
        pass


def e(text: str) -> str:
    """Escape user-controlled text for Telegram HTML."""
    return html.escape(text, quote=False)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

WELCOME = (
    "سلام! 👋\n"
    "من متن Markdown یا فایل OPML شما را به یک <b>نقشه ذهنی</b> (PNG + PDF) "
    "تبدیل می‌کنم.\n\n"
    "• فایل <code>.md</code> یا <code>.opml</code> بفرستید، یا متن ساختاریافته "
    "(تیترها با <code>#</code> و لیست‌ها با <code>-</code>) بنویسید.\n"
    "• حداکثر حجم فایل: ۵۱۲ کیلوبایت\n"
    "• سپس فونت دلخواه را انتخاب کنید."
)

router = Router()


def build_font_keyboard(pid: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=cfg["button_label"],
            callback_data=f"font:{idx}:{pid}",
        )]
        for idx, cfg in enumerate(FONT_CONFIG)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME, parse_mode=ParseMode.HTML)


def _pick_kind(name: str | None, mime: str | None) -> str | None:
    if name and name.lower().endswith(".opml"):
        return "opml"
    if name and name.lower().endswith((".md", ".markdown", ".txt")):
        return "md"
    if mime and "xml" in mime:
        return "opml"
    if mime and ("markdown" in mime or "text" in mime):
        return "md"
    return None


async def _store_and_ask_font(message: Message, bot: Bot,
                              content: bytes, kind: str, title: str) -> None:
    if len(content) > MAX_DOCUMENT_BYTES:
        await message.answer("فایل بیش از حد مجاز (۵۱۲ کیلوبایت) است.")
        return
    pid = STORE.add(message.from_user.id, content, kind, title)
    await message.answer(
        f"سند «{e(title)}» دریافت شد. فونت نقشه را انتخاب کنید:",
        reply_markup=build_font_keyboard(pid),
    )


@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    doc = message.document
    kind = _pick_kind(doc.file_name, doc.mime_type)
    if kind is None:
        await message.answer("فقط فایل‌های <code>.md</code> یا <code>.opml</code> پشتیبانی می‌شوند.")
        return
    if doc.file_size and doc.file_size > MAX_DOCUMENT_BYTES:
        await message.answer("فایل بیش از حد مجاز (۵۱۲ کیلوبایت) است.")
        return
    try:
        tg_file = await bot.get_file(doc.file_id)
        buf = await bot.download_file(tg_file.file_path)
        content = buf.read() if buf else b""
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 1)
        return
    except TelegramForbiddenError:
        return
    except (TelegramBadRequest, ConnectionError, asyncio.TimeoutError):
        logger.warning("Download failed for user %s.", message.from_user.id)
        await message.answer("دانلود فایل ناموفق بود؛ دوباره تلاش کنید.")
        return
    if len(content) > MAX_DOCUMENT_BYTES:
        await message.answer("فایل بیش از حد مجاز (۵۱۲ کیلوبایت) است.")
        return
    await _store_and_ask_font(message, bot, content, kind, doc.file_name or "سند")


@router.message(F.text)
async def on_text(message: Message, bot: Bot) -> None:
    text = message.text or ""
    if len(text) > MAX_MESSAGE_CHARS:
        await message.answer("متن بیش از حد طولانی است.")
        return
    await _store_and_ask_font(message, bot, text.encode("utf-8"), "md", "متن پیام")


@router.callback_query(F.data.startswith("font:"))
async def on_font_choice(callback: CallbackQuery, bot: Bot) -> None:
    try:
        _, idx_s, pid = callback.data.split(":", 2)
        font_index = int(idx_s)
    except (ValueError, AttributeError):
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    pending = STORE.get(pid, callback.from_user.id)
    if pending is None:
        await callback.answer("این درخواست منقضی شده است؛ دوباره سند را بفرستید.",
                              show_alert=True)
        return
    STORE.drop(pid)
    await safe_edit(callback, "در حال تولید نقشه ذهنی… ⏳")
    await callback.answer()

    try:
        if pending.kind == "opml":
            tree = mdparser.parse_opml(pending.content)
        else:
            tree = mdparser.parse_text(pending.content)
    except mdparser.ParseError as exc:
        await callback.message.answer(f"خطای تجزیه: {e(str(exc))}")
        return

    renderer = _get_renderer()
    try:
        result = await renderer.render(tree, font_index=font_index,
                                       config=RENDER_CONFIG)
    except RenderError as exc:
        await callback.message.answer(e(str(exc)))
        return
    except Exception:
        logger.exception("Unexpected render failure")
        await callback.message.answer("خطای غیرمنتظره در تولید نقشه رخ داد.")
        return

    note = await safe_send(
        bot, callback.message.chat.id,
        document=BufferedInputFile(result.png, filename="mindmap.png"),
        caption=(
            f"نقشه ذهنی «{e(pending.title)}»\n"
            f"فونت: {e(result.font_label)} | گره‌ها: {result.node_count}"
        ),
    )
    if note and result.pdf:
        await safe_send(
            bot, callback.message.chat.id,
            document=BufferedInputFile(result.pdf, filename="mindmap.pdf"),
        )


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

_renderer: Renderer | None = None


def _get_renderer() -> Renderer:
    global _renderer
    if _renderer is None:
        _renderer = Renderer(RENDER_CONFIG)
    return _renderer


async def main() -> None:
    if not BOT_TOKEN or "PUT-YOUR-TOKEN" in BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN در فایل .env تنظیم نشده است."
        )
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await _get_renderer().start()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await _get_renderer().stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
