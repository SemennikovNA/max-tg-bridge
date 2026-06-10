import asyncio
import json

from max_client import WebMaxClient, load_session


def schema(o):
    if isinstance(o, dict):
        return {k: schema(v) for k, v in o.items()}
    if isinstance(o, list):
        return [schema(o[0])] if o else []
    return type(o).__name__


async def main():
    s = load_session()
    client = WebMaxClient(device_id=s["device_id"])
    await client.connect()
    resp = await client.login_by_token(s["auth_token"], s["device_id"])
    payload = resp["payload"]

    print("=== payload keys ===", list(payload.keys()))

    chats = payload.get("chats") or []
    print("=== chats count ===", len(chats))
    if chats:
        print("=== chat[0] SCHEMA (no values) ===")
        print(json.dumps(schema(chats[0]), ensure_ascii=False, indent=1))
        print("=== chat types (first 20) ===",
              [c.get("type") for c in chats[:20]])
        print("=== sample non-PII fields chat[0] ===")
        c0 = chats[0]
        print("  id:", c0.get("id"), "type:", c0.get("type"),
              "has_title:", bool(c0.get("title")),
              "title_len:", len(c0.get("title") or ""))

    contacts = payload.get("contacts") or []
    print("=== contacts count ===", len(contacts))
    if contacts:
        print("=== contact[0] SCHEMA ===")
        print(json.dumps(schema(contacts[0]), ensure_ascii=False, indent=1))

    profile = payload.get("profile")
    if profile:
        print("=== profile SCHEMA ===")
        print(json.dumps(schema(profile), ensure_ascii=False, indent=1))
        print("=== my_id ===", profile.get("contact", {}).get("id"))

    if chats:
        c0 = chats[0]
        parts = [int(x) for x in c0.get("participants", {}).keys()]
        print("=== opcode 32 (contacts) for participants ===", parts)
        try:
            r32 = await client.invoke_method(32, {"contactIds": parts})
            p32 = r32.get("payload", {})
            print("  payload keys:", list(p32.keys()))
            conts = p32.get("contacts") or p32.get("payload") or []
            if isinstance(conts, list) and conts:
                print("  contact[0] SCHEMA:")
                print(json.dumps(schema(conts[0]), ensure_ascii=False, indent=1))
            else:
                print("  raw schema:", json.dumps(schema(p32), ensure_ascii=False))
        except Exception as e:
            print("  opcode32 error:", e)

        print("=== opcode 49 (history) for chat[0] ===", c0.get("id"))
        try:
            r49 = await client.invoke_method(49, {
                "chatId": c0["id"], "from": c0["lastMessage"]["time"],
                "forward": 0, "backward": 10, "getMessages": True,
            })
            p49 = r49.get("payload", {})
            print("  payload keys:", list(p49.keys()))
            msgs = p49.get("messages") or []
            print("  messages count:", len(msgs))
            if msgs:
                print("  message[0] SCHEMA:")
                print(json.dumps(schema(msgs[0]), ensure_ascii=False, indent=1))
        except Exception as e:
            print("  opcode49 error:", e)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
