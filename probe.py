import asyncio
import json
import logging

from max_client import connect_logged_in

logging.basicConfig(level=logging.INFO)


async def on_packet(client, packet):
    opcode = packet.get("opcode")
    if opcode == 1:
        return
    if opcode == 128 and packet.get("cmd") == 0:
        p = packet.get("payload", {})
        msg = p.get("message", {})
        chat = p.get("chat", {})
        print("--- incoming 128 ---")
        print(f"  chatId   : {p.get('chatId')}")
        print(f"  chatType : {chat.get('type')}")
        print(f"  sender   : {msg.get('sender')}")
        print(f"  text     : {msg.get('text')!r}")
        print(f"  attaches : {[a.get('_type') for a in msg.get('attaches', [])]}")
        return
    print(f"[packet] opcode={opcode} cmd={packet.get('cmd')} "
          f"keys={list(packet.get('payload', {}).keys())}")


async def main():
    client = await connect_logged_in()
    client.on_packet(on_packet)
    print("Connected and logged in. Listening for packets... (Ctrl+C to stop)")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
