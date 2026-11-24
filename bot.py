import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BusinessConnection, Message


logging.basicConfig(
    level=logging.INFO,
    format=«%(asctime)s | %(levelname)s | %(message)s»,
)
logger = logging.getLogger(«biz-fast-bot»)


def _get_required_env(name: str) -> str:
    value = os.getenv(name, «»).strip()
    if not value:
        raise RuntimeError(f»Не задана переменная окружения: {name}»)
    return value


BOT_TOKEN = _get_required_env(«BOT_TOKEN»)
PORT = int(os.getenv(«PORT», «10000»))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dataclass
class ChatConfig:
    muted: bool = False
    antimute: bool = False
    spam_batch_size: int = 6
    spam_delay: float = 0.06
    intercepted_mutes: int = 0
    deleted_messages: int = 0
    last_updated: float = field(default_factory=time.time)


# Маппинги в памяти для мгновенного доступа O(1)
connections_map: dict[str, BusinessConnection] = {}
chat_configs: dict[tuple[str, int], ChatConfig] = {}
spam_tasks: dict[tuple[str, int], asyncio.Task[Any]] = {}
background_tasks: set[asyncio.Task[Any]] = set()

# Быстрый маппинг алиасов команд и триггеров антимута
COMMAND_ALIASES: dict[str, str] = {
    «.mute»: «mute»,
    «mute»: «mute»,
    «.unmute»: «unmute»,
    «unmute»: «unmute»,
    «.antimute»: «antimute»,
    «antimute»: «antimute»,
    «.status»: «status»,
    «.stop»: «stop_spam»,
}

ANTIMUTE_TRIGGERS: tuple[str, ...] = (
    «.mute»,
    «mute»,
    «/mute»,
    «!mute»,
)


def _track_task(coro: Any) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


def _get_config(conn_id: str, chat_id: int) -> ChatConfig:
    key = (conn_id, chat_id)
    cfg = chat_configs.get(key)
    if cfg is None:
        cfg = ChatConfig()
        chat_configs[key] = cfg
    return cfg


def _is_antimute_trigger(raw_text: str) -> bool:
    low = raw_text.strip().lower()
    if not low:
        return False
    for trig in ANTIMUTE_TRIGGERS:
        if low == trig or low.startswith(trig + « «) or low.startswith(trig + «\n»):
            return True
    return False


async def _delete_message_fast(conn_id: str, message_id: int, cfg: ChatConfig) -> None:
    try:
        await bot.delete_business_messages(
            business_connection_id=conn_id,
            message_ids=[message_id],
        )
        cfg.deleted_messages += 1
        cfg.last_updated = time.time()
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await bot.delete_business_messages(
                business_connection_id=conn_id,
                message_ids=[message_id],
            )
            cfg.deleted_messages += 1
        except Exception:
            pass
    except Exception:
        pass


async def _send_message_fast(conn_id: str, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            business_connection_id=conn_id,
        )
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                business_connection_id=conn_id,
            )
        except Exception:
            pass
    except Exception:
        pass


async def _spam_worker(
    conn_id: str,
    chat_id: int,
    count: int,
    text: str,
    cfg: ChatConfig,
) -> None:
    key = (conn_id, chat_id)
    batch_size = cfg.spam_batch_size
    delay = cfg.spam_delay
    sent = 0
    try:
while sent < count:
            chunk = min(batch_size, count - sent)
            await asyncio.gather(
                *(_send_message_fast(conn_id, chat_id, text) for _ in range(chunk))
            )
            sent += chunk
            cfg.last_updated = time.time()
            if sent < count and delay > 0:
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        logger.info(«Спам остановлен в чате %s (conn=%s)», chat_id, conn_id)
    finally:
        spam_tasks.pop(key, None)


def _cancel_spam(conn_id: str, chat_id: int) -> bool:
    key = (conn_id, chat_id)
    existing = spam_tasks.get(key)
    if existing and not existing.done():
        existing.cancel()
        spam_tasks.pop(key, None)
        return True
    return False


async def _handle_owner_command(
    conn_id: str,
    chat_id: int,
    message_id: int,
    text: str,
    cfg: ChatConfig,
) -> None:
    low = text.strip().lower()

    if low.startswith(«.spam «):
        parts = text.strip().split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit():
            return
        count = int(parts[1])
        spam_word = parts[2].strip()
        if not (1 <= count <= 200) or not spam_word:
            return

        _track_task(_delete_message_fast(conn_id, message_id, cfg))
        _cancel_spam(conn_id, chat_id)

        key = (conn_id, chat_id)
        spam_tasks[key] = asyncio.create_task(
            _spam_worker(conn_id, chat_id, count, spam_word, cfg)
        )
        return

    action = COMMAND_ALIASES.get(low)
    if not action:
        return

    _track_task(_delete_message_fast(conn_id, message_id, cfg))
    cfg.last_updated = time.time()

    if action == «mute»:
        cfg.muted = True
    elif action == «unmute»:
        cfg.muted = False
    elif action == «antimute»:
        cfg.antimute = not cfg.antimute
    elif action == «stop_spam»:
        _cancel_spam(conn_id, chat_id)
    elif action == «status»:
        status_text = (
            f»mute={cfg.muted} | antimute={cfg.antimute} | «
            f»deleted={cfg.deleted_messages} | intercepted={cfg.intercepted_mutes}»
        )
        _track_task(_send_message_fast(conn_id, chat_id, status_text))


@dp.business_connection()
async def on_business_connection(connection: BusinessConnection) -> None:
    connections_map[connection.id] = connection
    logger.info(
        «Business connection обновлено: id=%s, user=%s, enabled=%s»,
        connection.id,
        connection.user.id,
        connection.is_enabled,
    )


@dp.business_message()
async def on_business_message(message: Message) -> None:
    conn_id = message.business_connection_id
    if not conn_id or not message.from_user:
        return

    chat_id = message.chat.id
    cfg = _get_config(conn_id, chat_id)
    raw_text = message.text or message.caption or «»

    # В личном бизнес-чате сообщение от собеседника имеет from_user.id == chat.id
    is_partner_message = message.from_user.id == chat_id

    if is_partner_message:
        if cfg.muted:
            _track_task(_delete_message_fast(conn_id, message.message_id, cfg))

        if cfg.antimute and _is_antimute_trigger(raw_text):
            cfg.intercepted_mutes += 1
            cfg.last_updated = time.time()
            _track_task(_send_message_fast(conn_id, chat_id, «.mute»))
        return

    # Сообщение от владельца бизнес-аккаунта
    await _handle_owner_command(conn_id, chat_id, message.message_id, raw_text, cfg)


async def _health_handler(request: web.Request) -> web.Response:
    active_spam = sum(1 for t in spam_tasks.values() if not t.done())
    payload = {
        «status»: «ok»,
        «connections»: len(connections_map),
        «tracked_chats»: len(chat_configs),
        «active_spam_tasks»: active_spam,
    }
    return web.json_response(payload)
async def _stats_handler(request: web.Request) -> web.Response:
    items = []
    for (conn_id, chat_id), cfg in chat_configs.items():
        items.append(
            {
                «conn_id»: conn_id,
                «chat_id»: chat_id,
                «muted»: cfg.muted,
                «antimute»: cfg.antimute,
                «deleted_messages»: cfg.deleted_messages,
                «intercepted_mutes»: cfg.intercepted_mutes,
            }
        )
    return web.json_response({«chats»: items})


async def _start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get(«/«, _health_handler)
    app.router.add_get(«/health», _health_handler)
    app.router.add_get(«/stats», _stats_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, «0.0.0.0», PORT)
    await site.start()
    logger.info(«HTTP-сервер запущен на порту %s», PORT)
    return runner


async def main() -> None:
    runner = await _start_web_server()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info(«Бизнес-бот запущен (polling)...»)
        await dp.start_polling(
            bot,
            allowed_updates=[«business_message», «business_connection»],
        )
    finally:
        for task in list(spam_tasks.values()):
            task.cancel()
        await runner.cleanup()


if name == «main»:
    asyncio.run(main())

