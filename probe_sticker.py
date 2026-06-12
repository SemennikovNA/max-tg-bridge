import asyncio
import json

from max_client import connect_logged_in


async def on_packet(client, packet):
    if packet.get("opcode") != 128 or packet.get("cmd") != 0:
        return
    msg = packet.get("payload", {}).get("message", {})
    print(f"\n=== msg type={msg.get('type')} ===", flush=True)
    print(json.dumps(msg, ensure_ascii=False)[:700], flush=True)


async def main():
    client = await connect_logged_in()
    client.on_packet(on_packet)
    print("Listening for ALL incoming messages… (send a sticker)", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
