import asyncio
import logging

from aiogram import Bot, Dispatcher

import config
from max_client import connect_logged_in

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger("bridge")

state = {"max": None, "bot": None}


async def on_max_packet(client, packet):
    opcode = packet.get("opcode")
    if opcode == 1:
        return
    if opcode == 128 and packet.get("cmd") == 0:
        # TODO Этап 2: переслать сообщение в нужный Telegram-топик
        p = packet.get("payload", {})
        msg = p.get("message", {})
        _logger.info("MAX msg chat=%s sender=%s text=%r",
                     p.get("chatId"), msg.get("sender"), msg.get("text"))


async def run_max():
    client = await connect_logged_in()
    state["max"] = client
    client.on_packet(on_max_packet)
    _logger.info("MAX client connected & logged in")
    await asyncio.Event().wait()


async def run_telegram():
    bot = Bot(config.TELEGRAM_BOT_TOKEN)
    state["bot"] = bot
    dp = Dispatcher()

    # TODO Этап 3: handler ответов из топиков → send_message в MAX
    # TODO Этап 4: forum topics, маппинг chatId <-> topic_id

    _logger.info("Telegram bot polling started")
    await dp.start_polling(bot)


async def main():
    await asyncio.gather(run_max(), run_telegram())


if __name__ == "__main__":
    asyncio.run(main())
