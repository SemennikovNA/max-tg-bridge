import asyncio
import logging
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

import config
from max_client import WebMaxClient, load_session
from storage import Storage

_logger = logging.getLogger("bridge")


class Bridge:
    def __init__(self):
        self.store = Storage()
        self.max: WebMaxClient = None
        self.bot = Bot(config.TELEGRAM_BOT_TOKEN)
        self.dp = Dispatcher()
        self.group_id = config.TELEGRAM_GROUP_ID
        self.me_id = None
        self.tz = ZoneInfo(config.WEB_TIMEZONE)
        self._reconnecting = False
        self._hb_task = None
        self._register_handlers()

    # ---------- names ----------

    @staticmethod
    def _name_from_contact(contact: dict):
        names = contact.get("names") or []
        if not names:
            return None
        n = names[0]
        return (n.get("name")
                or " ".join(filter(None, [n.get("firstName"), n.get("lastName")]))
                or None)

    async def resolve_name(self, user_id: int) -> str:
        cached = self.store.get_name(user_id)
        if cached:
            return cached
        name = None
        try:
            contacts = await self.max.get_contacts([user_id])
            if contacts:
                name = self._name_from_contact(contacts[0])
        except Exception as err:
            _logger.warning("resolve_name(%s) failed: %s", user_id, err)
        name = name or f"id{user_id}"
        self.store.set_name(user_id, name)
        return name

    async def chat_display_name(self, chat: dict) -> str:
        if chat.get("title"):
            return chat["title"]
        others = [int(x) for x in chat.get("participants", {}).keys()
                  if int(x) != self.me_id]
        if others:
            return await self.resolve_name(others[0])
        return f"Чат {chat.get('id')}"

    # ---------- MAX session ----------

    async def start_max(self):
        session = load_session()
        if not session:
            raise RuntimeError("No session. Provide session.json first.")
        self.max = WebMaxClient(device_id=session["device_id"])
        await self.max.connect()
        resp = await self.max.login_by_token(session["auth_token"], session["device_id"])
        payload = resp["payload"]
        self.me_id = payload["profile"]["contact"]["id"]
        _logger.info("MAX logged in, me_id=%s", self.me_id)
        await self.cleanup_ignored()
        await self.init_topics(payload.get("chats", []))
        self.max.set_reconnect_callback(self._reconnect)
        self.max.on_packet(self.on_max_packet)

    async def _reconnect(self):
        if self._reconnecting:
            return
        self._reconnecting = True
        backoff = 1
        try:
            while True:
                await self.max.cleanup_for_reconnect()
                try:
                    session = load_session()
                    await self.max.connect()
                    await self.max.login_by_token(
                        session["auth_token"], session["device_id"])
                    self.max.on_packet(self.on_max_packet)
                    _logger.info("MAX reconnected")
                    return
                except Exception as err:
                    _logger.warning("MAX reconnect failed: %s; retry in %ss",
                                    err, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
        finally:
            self._reconnecting = False

    # ---------- topics ----------

    async def cleanup_ignored(self):
        for chat_id in config.IGNORED_CHAT_IDS:
            topic_id = self.store.get_topic(chat_id)
            if topic_id is None:
                continue
            try:
                await self.bot.delete_forum_topic(self.group_id, topic_id)
            except Exception as err:
                _logger.warning("delete ignored topic %s failed: %s", topic_id, err)
            self.store.del_topic(chat_id)
            _logger.info("removed ignored chat %s (topic %s)", chat_id, topic_id)

    async def init_topics(self, chats: list):
        new = [c for c in chats
               if c["id"] not in config.IGNORED_CHAT_IDS
               and self.store.get_topic(c["id"]) is None]
        _logger.info("init_topics: %s chats, %s new", len(chats), len(new))
        for chat in new:
            chat_id = chat["id"]
            try:
                name = await self.chat_display_name(chat)
                topic_id = await self.create_topic(name)
                self.store.set_topic(chat_id, topic_id)
                last = chat.get("lastMessage") or {}
                await self.send_history(chat_id, topic_id, last.get("time"))
            except Exception as err:
                _logger.warning("init topic for chat %s failed: %s", chat_id, err)
            await asyncio.sleep(config.TOPIC_THROTTLE)

    async def create_topic(self, name: str) -> int:
        ft = await self.bot.create_forum_topic(self.group_id, name=(name or "чат")[:128])
        return ft.message_thread_id

    async def ensure_topic(self, chat_id: int, fallback_name: str) -> int:
        topic_id = self.store.get_topic(chat_id)
        if topic_id is not None:
            return topic_id
        topic_id = await self.create_topic(fallback_name)
        self.store.set_topic(chat_id, topic_id)
        return topic_id

    async def send_history(self, chat_id: int, topic_id: int, from_time):
        if not from_time:
            return
        try:
            msgs = await self.max.get_history(chat_id, from_time, config.HISTORY_DEPTH)
        except Exception as err:
            _logger.warning("history for %s failed: %s", chat_id, err)
            return
        for m in msgs:
            self.store.mark_seen(m.get("id"))
            await self.deliver_to_tg(topic_id, m, history=True)
            await asyncio.sleep(0.4)

    # ---------- MAX -> Telegram ----------

    async def on_max_packet(self, client, packet: dict):
        if packet.get("opcode") != 128 or packet.get("cmd") != 0:
            return
        payload = packet.get("payload", {})
        msg = payload.get("message")
        if not msg:
            return
        chat_id = payload.get("chatId")
        if chat_id in config.IGNORED_CHAT_IDS:
            return
        msg_id = msg.get("id")
        if self.store.is_seen(msg_id):
            return
        self.store.mark_seen(msg_id)
        sender = msg.get("sender")
        name = await self.resolve_name(sender) if sender != self.me_id else "Я"
        topic_id = await self.ensure_topic(chat_id, name)
        await self.deliver_to_tg(topic_id, msg)

    async def deliver_to_tg(self, topic_id: int, msg: dict, history: bool = False):
        sender = msg.get("sender")
        sender_name = "Я" if sender == self.me_id else await self.resolve_name(sender)
        text = (msg.get("text") or "").strip()
        attaches = msg.get("attaches") or []
        if attaches:
            kinds = ", ".join(a.get("_type", "?") for a in attaches)
            text = (text + f"\n[вложение: {kinds}]").strip()
        if not text:
            text = "[пусто]"
        prefix = "🕓 " if history else ""
        out = f"{prefix}{sender_name}: {text}"
        try:
            await self.bot.send_message(self.group_id, out, message_thread_id=topic_id)
        except Exception as err:
            _logger.warning("tg send to topic %s failed: %s", topic_id, err)

    # ---------- Telegram -> MAX ----------

    def _register_handlers(self):
        @self.dp.message(
            F.chat.id == self.group_id,
            F.message_thread_id.is_not(None),
            F.text,
        )
        async def _on_tg(message: Message):
            await self.on_tg_reply(message)

    def in_quiet_hours(self) -> bool:
        h = datetime.now(self.tz).hour
        a, b = config.QUIET_HOURS_START, config.QUIET_HOURS_END
        return (a <= h < b) if a <= b else (h >= a or h < b)

    async def on_tg_reply(self, message: Message):
        if message.from_user and message.from_user.is_bot:
            return
        chat_id = self.store.chat_for_topic(message.message_thread_id)
        if chat_id is None:
            return
        if self.in_quiet_hours():
            await message.reply("🌙 Тихие часы — в MAX не отправлено.")
            return
        await asyncio.sleep(random.uniform(config.HUMAN_DELAY_MIN, config.HUMAN_DELAY_MAX))
        try:
            await self.max.send_text(chat_id, message.text)
        except Exception as err:
            _logger.warning("max send to %s failed: %s", chat_id, err)
            await message.reply(f"⚠️ Не отправлено в MAX: {err}")

    # ---------- lifecycle ----------

    async def _heartbeat_loop(self):
        while True:
            try:
                config.HEARTBEAT_FILE.write_text(str(int(time.time())))
            except Exception:
                pass
            await asyncio.sleep(config.HEARTBEAT_INTERVAL)

    async def shutdown(self):
        _logger.info("Shutting down...")
        if self._hb_task:
            self._hb_task.cancel()
        try:
            if self.max:
                await self.max.disconnect()
        except Exception:
            pass
        try:
            await self.bot.session.close()
        except Exception:
            pass
        try:
            self.store.db.close()
        except Exception:
            pass

    async def run(self):
        await self.start_max()
        self._hb_task = asyncio.create_task(self._heartbeat_loop())
        _logger.info("Bridge started; polling Telegram")
        try:
            await self.dp.start_polling(self.bot)
        finally:
            await self.shutdown()
