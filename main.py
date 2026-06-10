import asyncio
import logging

import config
from bridge import Bridge

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger("main")


async def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
    if not config.TELEGRAM_GROUP_ID:
        raise SystemExit("TELEGRAM_GROUP_ID is not set in .env")

    bridge = Bridge()
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())
