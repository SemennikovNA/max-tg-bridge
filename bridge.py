import asyncio
import logging
import random
import time

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, MessageReactionUpdated, BufferedInputFile

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
        self._reconnecting = False
        self._hb_task = None
        self.session = None
        self.chats_meta = {}
        self.last_incoming = {}
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

    async def get_chat_meta(self, chat_id: int):
        meta = self.chats_meta.get(chat_id)
        if meta:
            return meta
        try:
            chats = await asyncio.wait_for(
                self.max.get_chats(self.session["auth_token"]), timeout=10)
            self.chats_meta = {c["id"]: c for c in chats}
        except Exception as err:
            _logger.warning("get_chats refresh failed: %s", err)
        return self.chats_meta.get(chat_id)

    async def chat_name_for(self, chat_id: int, fallback: str) -> str:
        meta = await self.get_chat_meta(chat_id)
        if meta:
            return await self.chat_display_name(meta)
        return fallback

    def is_group_chat(self, chat_id) -> bool:
        meta = self.chats_meta.get(chat_id)
        if meta:
            return meta.get("type") != "DIALOG"
        return (chat_id or 0) < 0

    # ---------- MAX session ----------

    async def start_max(self):
        self.session = load_session()
        if not self.session:
            raise RuntimeError("No session. Provide session.json first.")
        self.max = WebMaxClient(device_id=self.session["device_id"])
        await self.max.connect()
        resp = await self.max.login_by_token(
            self.session["auth_token"], self.session["device_id"])
        payload = resp["payload"]
        self.me_id = payload["profile"]["contact"]["id"]
        _logger.info("MAX logged in, me_id=%s", self.me_id)
        chats = payload.get("chats", [])
        self.chats_meta = {c["id"]: c for c in chats}
        await self.cleanup_ignored()
        await self.init_topics(chats)
        await self.refresh_topic_names()
        self.max.set_reconnect_callback(self._reconnect)
        self.max.on_packet(self.on_max_packet)

    async def refresh_topic_names(self):
        for chat_id, meta in self.chats_meta.items():
            if chat_id in config.IGNORED_CHAT_IDS or meta.get("type") == "DIALOG":
                continue
            topic_id = self.store.get_topic(chat_id)
            if topic_id is None:
                continue
            try:
                name = await self.chat_display_name(meta)
                await self.bot.edit_forum_topic(
                    self.group_id, topic_id, name=name[:128])
                _logger.info("renamed topic %s -> %s", topic_id, name)
            except Exception as err:
                _logger.warning("rename topic %s failed: %s", topic_id, err)
            await asyncio.sleep(0.5)

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
        name = await self.chat_name_for(chat_id, fallback_name)
        topic_id = await self.create_topic(name)
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
            await self.deliver_to_tg(topic_id, m, chat_id=chat_id, history=True)
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
        await self.deliver_to_tg(topic_id, msg, chat_id=chat_id)
        # A: входящее доставлено в Telegram -> помечаем прочитанным в MAX
        if sender != self.me_id:
            self.last_incoming[chat_id] = (msg_id, msg.get("time"))
            try:
                await self.max.mark_read(chat_id, msg_id, msg.get("time"))
            except Exception as err:
                _logger.warning("mark_read (deliver) failed: %s", err)

    async def deliver_to_tg(self, topic_id: int, msg: dict,
                            chat_id: int = None, history: bool = False):
        sender = msg.get("sender")
        text = (msg.get("text") or "").strip()
        attaches = msg.get("attaches") or []
        prefix = "🕓 " if history else ""
        if sender == self.me_id:
            label = "Я: "
        elif self.is_group_chat(chat_id):
            label = f"{await self.resolve_name(sender)}: "
        else:
            label = ""
        caption = f"{prefix}{label}{text}".rstrip()

        if not attaches:
            out = caption if text else (f"{prefix}{label}[пусто]".rstrip() or "[пусто]")
            try:
                sent = await self.bot.send_message(
                    self.group_id, out, message_thread_id=topic_id)
                self._map(sent, chat_id, msg.get("id"))
            except Exception as err:
                _logger.warning("tg send to topic %s failed: %s", topic_id, err)
            return

        first = True
        for a in attaches:
            cap = caption if first else None
            sent = await self.deliver_attach(topic_id, chat_id, msg.get("id"), a, cap)
            if first and sent:
                self._map(sent, chat_id, msg.get("id"))
            first = False

    def _map(self, sent, chat_id, max_msg_id):
        if sent and chat_id is not None and max_msg_id:
            self.store.set_msg_map(sent.message_id, chat_id, max_msg_id)

    async def deliver_attach(self, topic_id, chat_id, msg_id, a, caption):
        t = a.get("_type")
        g = self.group_id
        try:
            if t == "PHOTO":
                data = await self.max.download_bytes(a["baseUrl"])
                return await self.bot.send_photo(
                    g, BufferedInputFile(data, "photo.jpg"),
                    caption=caption, message_thread_id=topic_id)
            if t == "VIDEO":
                try:
                    url = await self.max.get_video_url(chat_id, msg_id, a["videoId"])
                    data = await self.max.download_bytes(url)
                except Exception as err:
                    _logger.warning("video download failed (%s); fallback to preview", err)
                    thumb = a.get("thumbnail")
                    if thumb:
                        tdata = await self.max.download_bytes(thumb)
                        cap = ((caption or "") + "\n🎥 видео").strip()
                        return await self.bot.send_photo(
                            g, BufferedInputFile(tdata, "video_preview.jpg"),
                            caption=cap, message_thread_id=topic_id)
                    raise
                if a.get("videoType") == 1:
                    sent = await self.bot.send_video_note(
                        g, BufferedInputFile(data, "round.mp4"),
                        message_thread_id=topic_id)
                    if caption:
                        await self.bot.send_message(g, caption, message_thread_id=topic_id)
                    return sent
                return await self.bot.send_video(
                    g, BufferedInputFile(data, "video.mp4"),
                    caption=caption, message_thread_id=topic_id)
            if t == "AUDIO":
                data = await self.max.download_bytes(a["url"])
                try:
                    return await self.bot.send_voice(
                        g, BufferedInputFile(data, "voice.ogg"),
                        caption=caption, message_thread_id=topic_id)
                except Exception:
                    return await self.bot.send_audio(
                        g, BufferedInputFile(data, "audio.m4a"),
                        caption=caption, message_thread_id=topic_id)
            if t == "FILE":
                url = await self.max.get_file_url(chat_id, msg_id, a["fileId"])
                data = await self.max.download_bytes(url)
                fname = a.get("name") or a.get("fileName") or "file.bin"
                return await self.bot.send_document(
                    g, BufferedInputFile(data, fname),
                    caption=caption, message_thread_id=topic_id)
            return await self.bot.send_message(
                g, f"{caption or ''}\n[вложение: {t}]".strip(),
                message_thread_id=topic_id)
        except Exception as err:
            _logger.warning("deliver attach %s failed: %s", t, err)
            try:
                return await self.bot.send_message(
                    g, f"{caption or ''}\n[вложение {t} — не доставлено]".strip(),
                    message_thread_id=topic_id)
            except Exception:
                return None

    # ---------- Telegram -> MAX ----------

    def _register_handlers(self):
        @self.dp.message(
            F.chat.id == self.group_id,
            F.message_thread_id.is_not(None),
            F.text,
        )
        async def _on_tg(message: Message):
            await self.on_tg_reply(message)

        @self.dp.edited_message(
            F.chat.id == self.group_id,
            F.message_thread_id.is_not(None),
            F.text,
        )
        async def _on_edit(message: Message):
            await self.on_tg_edit(message)

        @self.dp.message_reaction(F.chat.id == self.group_id)
        async def _on_reaction(event: MessageReactionUpdated):
            await self.on_tg_reaction(event)

    async def on_tg_edit(self, message: Message):
        if message.from_user and message.from_user.is_bot:
            return
        mapping = self.store.get_max_msg(message.message_id)
        if not mapping:
            return
        chat_id, max_msg_id = mapping
        try:
            await self.max.edit_message(chat_id, max_msg_id, message.text)
        except Exception as err:
            _logger.warning("edit in MAX failed: %s", err)

    async def on_tg_reaction(self, event: MessageReactionUpdated):
        mapping = self.store.get_max_msg(event.message_id)
        if not mapping:
            return
        chat_id, max_msg_id = mapping
        emoji = None
        for r in (event.new_reaction or []):
            if getattr(r, "type", None) == "emoji":
                emoji = r.emoji
                break
        try:
            if emoji:
                await self.max.set_reaction(chat_id, max_msg_id, emoji)
            else:
                # пустая new_reaction => реакция снята
                await self.max.remove_reaction(chat_id, max_msg_id)
        except Exception as err:
            _logger.warning("reaction sync failed: %s", err)

    async def on_tg_reply(self, message: Message):
        if message.from_user and message.from_user.is_bot:
            return
        chat_id = self.store.chat_for_topic(message.message_thread_id)
        if chat_id is None:
            return
        await asyncio.sleep(random.uniform(config.HUMAN_DELAY_MIN, config.HUMAN_DELAY_MAX))
        try:
            result = await self.max.send_text(chat_id, message.text)
        except Exception as err:
            _logger.warning("max send to %s failed: %s", chat_id, err)
            await message.reply(f"⚠️ Не отправлено в MAX: {err}")
            return
        # сохраняем tg<->max id отправленного (для редактирования/реакций)
        try:
            max_id = (result or {}).get("payload", {}).get("message", {}).get("id")
            if max_id:
                self.store.set_msg_map(message.message_id, chat_id, max_id)
        except Exception:
            pass
        # B: ответил -> помечаем последнее входящее прочитанным в MAX
        last = self.last_incoming.get(chat_id)
        if last:
            try:
                await self.max.mark_read(chat_id, last[0], last[1])
            except Exception as err:
                _logger.warning("mark_read (reply) failed: %s", err)

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
            await self.dp.start_polling(
                self.bot,
                allowed_updates=self.dp.resolve_used_update_types(),
            )
        finally:
            await self.shutdown()
