import asyncio
import logging

import config
from max_client import WebMaxClient, save_session, load_session

logging.basicConfig(level=logging.INFO)


async def main():
    existing = load_session()
    if existing:
        print(f"Session already exists for {existing.get('phone', '?')}.")
        if input("Overwrite? [y/N] ").strip().lower() != "y":
            return

    phone = config.MAX_PHONE or input("Phone (e.g. +79991234567): ").strip()

    client = WebMaxClient()
    await client.connect()

    sms_token = await client.send_code(phone)
    print("SMS code requested.")

    code = input("Enter SMS code: ").strip()
    response = await client.sign_in(sms_token, code)

    auth_token = response["payload"]["tokenAttrs"]["LOGIN"]["token"]
    save_session(client.device_id, auth_token, phone)
    print(f"Saved session. device_id={client.device_id}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
