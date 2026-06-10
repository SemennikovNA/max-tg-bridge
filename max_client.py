import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

import websockets
import vkmax.client as vc
from vkmax.client import MaxClient

import config

vc.USER_AGENT = config.WEB_USER_AGENT
vc.APP_VERSION = config.WEB_APP_VERSION

# vkmax логирует каждый запрос/ответ на INFO, включая auth-токен при логине
logging.getLogger("vkmax.client").setLevel(logging.WARNING)

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

            # резолвим future ТОЛЬКО на ответ (cmd==1) на наш запрос;
            # server-push (cmd==0) с тем же seq не должен перехватывать future
            if packet.get("cmd") == 1:
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
