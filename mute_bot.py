import asyncio
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, Message

TOKEN = os.environ["BOT_TOKEN"]  # берётся из Environment Variables на Render

FLOOD_LIMIT = 5        # сообщений
FLOOD_WINDOW = 10      # за столько секунд
FLOOD_MUTE_MIN = 5     # минут мута за флуд
ADMIN_CACHE_TTL = 300  # секунд хранить статус админа

dp = Dispatcher()
history = defaultdict(lambda: deque(maxlen=FLOOD_LIMIT))
admin_cache = {}


async def is_admin(chat, user_id: int) -> bool:
    key = (chat.id, user_id)
    cached = admin_cache.get(key)
    if cached and time.time() - cached[1] < ADMIN_CACHE_TTL:
        return cached[0]
    member = await chat.get_member(user_id)
    result = member.status in ("administrator", "creator")
    admin_cache[key] = (result, time.time())
    return result


async def mute(chat, user_id: int, minutes: int):
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    await chat.restrict(
        user_id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until,
    )


@dp.message(Command("mute"))
async def cmd_mute(m: Message, command: CommandObject):
    if not await is_admin(m.chat, m.from_user.id):
        return await m.reply("Только для админов.")
    if not m.reply_to_message:
        return await m.reply("Ответьте командой на сообщение: /mute 10")
    minutes = int(command.args) if command.args and command.args.isdigit() else 10
    target = m.reply_to_message.from_user
    if await is_admin(m.chat, target.id):
        return await m.reply("Нельзя замутить админа.")
    await mute(m.chat, target.id, minutes)
    await m.reply(f"🔇 {target.full_name} в муте на {minutes} мин.")


@dp.message(Command("unmute"))
async def cmd_unmute(m: Message):
    if not await is_admin(m.chat, m.from_user.id):
        return await m.reply("Только для админов.")
    if not m.reply_to_message:
        return await m.reply("Ответьте командой на сообщение: /unmute")
    target = m.reply_to_message.from_user
    await m.chat.restrict(
        target.id,
        permissions=ChatPermissions(
            can_send_messages=True,
            can_send_other_messages=True,
            can_send_polls=True,
            can_add_web_page_previews=True,
        ),
    )
    await m.reply(f"🔊 {target.full_name} размучен.")


@dp.message()
async def anti_flood(m: Message):
    if m.chat.type not in ("group", "supergroup") or not m.from_user:
        return
    # Сначала дешёвая проверка по памяти, запрос к Telegram только при флуде
    key = (m.chat.id, m.from_user.id)
    now = time.time()
    h = history[key]
    h.append(now)
    if len(h) == FLOOD_LIMIT and now - h[0] < FLOOD_WINDOW:
        h.clear()
        if await is_admin(m.chat, m.from_user.id):
            return
        await mute(m.chat, m.from_user.id, FLOOD_MUTE_MIN)
        await m.reply(
            f"🔇 {m.from_user.full_name} замучен на {FLOOD_MUTE_MIN} мин. за флуд."
        )


async def health(request):
    return web.Response(text="ok")


async def keep_alive():
    """Пингует сам себя, чтобы бесплатный Render не усыплял сервис."""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(600)
            try:
                await session.get(url)
            except Exception:
                pass


async def main():
    bot = Bot(TOKEN)
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    asyncio.create_task(keep_alive())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
