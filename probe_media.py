import asyncio
import json

from max_client import connect_logged_in


async def on_packet(client, packet):
    if packet.get("opcode") != 128 or packet.get("cmd") != 0:
        return
    msg = packet.get("payload", {}).get("message", {})
    att = msg.get("attaches") or []
    if not att:
        return
    print("\n=== MEDIA message (msg.type=%s) ===" % msg.get("type"), flush=True)
    for a in att:
        print("  _type:", a.get("_type"), flush=True)
        print(json.dumps(a, ensure_ascii=False, indent=1), flush=True)


async def main():
    client = await connect_logged_in()
    client.on_packet(on_packet)
    print("Listening for media attaches... (send photo/video/voice/round to test MAX)", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
