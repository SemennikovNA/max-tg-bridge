import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

import websockets
import vkmax.client as vc
from vkmax.client import MaxClient

import config

vc.USER_AGENT = config.WEB_USER_AGENT
vc.APP_VERSION = config.WEB_APP_VERSION

# vkmax логирует каждый запрос/ответ на INFO, включая auth-токен при логине.
# По умолчанию глушим до WARNING; для отладки: VKMAX_LOG=INFO
logging.getLogger("vkmax.client").setLevel(os.getenv("VKMAX_LOG", "WARNING"))

_logger = logging.getLogger(__name__)

PacketCallback = Callable[["WebMaxClient", dict], Awaitable[None]]


class WebMaxClient(MaxClient):
    def __init__(self, device_id: Optional[str] = None):
        super().__init__()
        self._fixed_device_id = device_id or f"{uuid.uuid4()}"
        self._user_callback: Optional[PacketCallback] = None

    async def _send_hello_packet(self, device_id: Optional[str] = None):
        self._device_id = device_id or self._fixed_device_id
        self._fixed_device_id = self._device_id
        return await self.invoke_method(
            opcode=6,
            payload={
                "userAgent": dict(config.WEB_FINGERPRINT),
                "deviceId": self._device_id,
            },
        )

    async def _recv_loop(self):
        while True:
            try:
                packet = json.loads(await self._connection.recv())
            except asyncio.CancelledError:
                return
            except websockets.exceptions.ConnectionClosedError as err:
                if not self._is_logged_in:
                    raise err
                if self._reconnect_callback:
                    asyncio.create_task(self._reconnect_callback())
                return
            except websockets.exceptions.ConnectionClosedOK:
                return
            except json.JSONDecodeError:
                continue

            if os.getenv("DEBUG_PACKETS"):
                _logger.warning("RAW <- cmd=%s seq=%s opcode=%s payload=%s",
                                packet.get("cmd"), packet.get("seq"),
                                packet.get("opcode"), packet.get("payload"))

            # резолвим future на ЛЮБОЙ ответ на наш запрос (cmd != 0):
            #   cmd==1 — успех, cmd==3 — ошибка.
            # server-push (cmd==0) с тем же seq не должен перехватывать future.
            if packet.get("cmd") != 0:
                future = self._pending.pop(packet.get("seq"), None)
                if future and not future.done():
                    future.set_result(packet)
                    continue

            if packet.get("opcode") == 136:
                payload = packet.get("payload", {})
                fut = None
                if "videoId" in payload:
                    fut = self._video_pending.pop(payload["videoId"], None)
                elif "fileId" in payload:
                    fut = self._file_pending.pop(payload["fileId"], None)
                if fut and not fut.done():
                    fut.set_result(None)

            if self._incoming_event_callback:
                asyncio.create_task(self._incoming_event_callback(self, packet))

    async def _send_ack(self, packet: dict):
        payload = packet.get("payload", {})
        message = payload.get("message", {})
        ack = {
            "ver": 11,
            "cmd": 1,
            "seq": packet["seq"],
            "opcode": 128,
            "payload": {
                "chatId": payload.get("chatId"),
                "messageId": message.get("id"),
            },
        }
        await self._connection.send(json.dumps(ack))

    def on_packet(self, callback: PacketCallback):
        self._user_callback = callback

        async def internal(client: "WebMaxClient", packet: dict):
            if (packet.get("opcode") == 128 and packet.get("cmd") == 0
                    and packet.get("payload", {}).get("message")):
                try:
                    await client._send_ack(packet)
                except Exception as err:
                    _logger.warning("failed to send ACK: %s", err)
            if self._user_callback:
                await self._user_callback(client, packet)

        self.set_packet_callback(internal)

    async def get_contacts(self, ids) -> list:
        resp = await self.invoke_method(32, {"contactIds": [int(i) for i in ids]})
        return resp.get("payload", {}).get("contacts", []) or []

    async def get_history(self, chat_id: int, from_time: int, backward: int = 10) -> list:
        resp = await self.invoke_method(49, {
            "chatId": chat_id, "from": from_time,
            "forward": 0, "backward": backward, "getMessages": True,
        })
        return resp.get("payload", {}).get("messages", []) or []

    async def send_text(self, chat_id: int, text: str):
        from vkmax.functions.messages import send_message
        return await send_message(self, chat_id, text)

    async def mark_read(self, chat_id: int, message_id, mark=None):
        return await self.invoke_method(50, {
            "type": "READ_MESSAGE",
            "chatId": chat_id,
            "messageId": str(message_id),
            "mark": int(mark or time.time() * 1000),
        })

    async def set_reaction(self, chat_id: int, message_id, emoji: str):
        return await self.invoke_method(178, {
            "chatId": chat_id,
            "messageId": str(message_id),
            "reaction": {"reactionType": "EMOJI", "id": emoji},
        })

    async def remove_reaction(self, chat_id: int, message_id):
        return await self.invoke_method(179, {
            "chatId": chat_id,
            "messageId": str(message_id),
        })

    async def get_chats(self, token: str, count: int = 100) -> list:
        resp = await self.invoke_method(19, {
            "interactive": False, "token": token, "chatsCount": count,
            "chatsSync": 0, "contactsSync": 0, "presenceSync": -1, "draftsSync": 0,
        })
        return resp.get("payload", {}).get("chats", []) or []

    async def cleanup_for_reconnect(self):
        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self._connection:
            try:
                await self._connection.close()
            except Exception:
                pass
            self._connection = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()


def load_session(path: Path = config.SESSION_FILE) -> Optional[dict]:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_session(device_id: str, auth_token: str, phone: str = "",
                 path: Path = config.SESSION_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "device_id": device_id,
        "auth_token": auth_token,
        "phone": phone,
    }, ensure_ascii=False, indent=2))


async def connect_logged_in() -> WebMaxClient:
    session = load_session()
    if not session:
        raise RuntimeError("No session. Run login.py first.")
    client = WebMaxClient(device_id=session["device_id"])
    await client.connect()
    await client.login_by_token(session["auth_token"], session["device_id"])
    return client
