import asyncio
import logging
import os
from pathlib import Path

import config
from max_client import WebMaxClient, save_session, load_session

logging.basicConfig(level=logging.INFO)

CODE_FILE = os.getenv("SMS_CODE_FILE")


async def get_code() -> str:
    if CODE_FILE:
        path = Path(CODE_FILE)
        print(f"Waiting for SMS code in {CODE_FILE} ...", flush=True)
        while True:
            if path.exists():
                code = path.read_text().strip()
                if code:
                    path.unlink(missing_ok=True)
                    return code
            await asyncio.sleep(2)
    return input("Enter SMS code: ").strip()


async def main():
    existing = load_session()
    if existing and not CODE_FILE:
        print(f"Session already exists for {existing.get('phone', '?')}.")
        if input("Overwrite? [y/N] ").strip().lower() != "y":
            return

    phone = config.MAX_PHONE or input("Phone (e.g. +79991234567): ").strip()

    client = WebMaxClient()
    await client.connect()

    sms_token = await client.send_code(phone)
    print("SMS code requested.", flush=True)

    code = await get_code()
    response = await client.sign_in(sms_token, code)

    auth_token = response["payload"]["tokenAttrs"]["LOGIN"]["token"]
    save_session(client.device_id, auth_token, phone)
    print(f"Saved session. device_id={client.device_id}", flush=True)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
