import asyncio
import json

from max_client import connect_logged_in


async def on_packet(client, packet):
    op = packet.get("opcode")
    if op == 1:
        return
    p = packet.get("payload", {}) or {}
    print(f"\nopcode={op} cmd={packet.get('cmd')} keys={list(p.keys())}", flush=True)
    print(json.dumps(p, ensure_ascii=False)[:400], flush=True)


async def main():
    client = await connect_logged_in()
    client.on_packet(on_packet)
    print("Listening for READ events… (send a msg to peer, ask peer to read it)", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
