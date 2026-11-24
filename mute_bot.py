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

TOKEN = os.environ["BOT_TOKEN"]

FLOOD_LIMIT = 5
FLOOD_WINDOW = 10
FLOOD_MUTE_MIN = 5
ADMIN_CACHE_TTL = 300

dp = Dispatcher()
history = defaultdict(lambda: deque(maxlen=FLOOD_LIMIT))
admin_cache = {}
muted = set()  # (chat_id, user_id): сообщения этих людей удаляются сразу


async def is_admin(chat, user_id: int) -> bool:
    key = (chat.id, user_id)
    cached = admin_cache.get(key)
    if cached and time.time() - cached[1] < ADMIN_CACHE_TTL:
        return cached[0]
    member = await chat.get_member(user_id)
    result = member.status in ("administrator", "creator")
    admin_cache[key] = (result, time.time())
    return result


def is_muted(m: Message) -> bool:
    return bool(m.from_user) and (m.chat.id, m.from_user.id) in muted


# 1) Самый первый обработчик: мгновенно удаляет сообщения замученных
@dp.message(is_muted)
async def delete_muted(m: Message):
    try:
        await m.delete()
    except Exception:
        pass


# 2) .mute / /mute (ответом на сообщение) — удалять все его сообщения
@dp.message(Command("mute", prefix="./"))
async def cmd_mute(m: Message):
    if not await is_admin(m.chat, m.from_user.id):
        return
    if not m.reply_to_message or not m.reply_to_message.from_user:
        return await m.reply("Ответьте командой на сообщение: .mute")
    target = m.reply_to_message.from_user
    if await is_admin(m.chat, target.id):
        return await m.reply("Нельзя замутить админа.")
    muted.add((m.chat.id, target.id))
    await m.reply_to_message.delete()
    await m.delete()


# 3) .unmute — отменить удаление
@dp.message(Command("unmute", prefix="./"))
async def cmd_unmute(m: Message):
    if not await is_admin(m.chat, m.from_user.id):
        return
    if not m.reply_to_message or not m.reply_to_message.from_user:
        return await m.reply("Ответьте командой на сообщение: .unmute")
    muted.discard((m.chat.id, m.reply_to_message.from_user.id))
    await m.reply("🔊 Удаление отключено.")


# 4) Автомут за флуд
@dp.message()
async def anti_flood(m: Message):
    if m.chat.type not in ("group", "supergroup") or not m.from_user:
        return
    key = (m.chat.id, m.from_user.id)
    now = time.time()
    h = history[key]
    h.append(now)
    if len(h) == FLOOD_LIMIT and now - h[0] < FLOOD_WINDOW:
        h.clear()
        if await is_admin(m.chat, m.from_user.id):
            return
        until = datetime.now(timezone.utc) + timedelta(minutes=FLOOD_MUTE_MIN)
        await m.chat.restrict(
            m.from_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await m.reply(f"🔇 {m.from_user.full_name} замучен за флуд.")


async def health(request):
    return web.Response(text="ok")


async def keep_alive():
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
