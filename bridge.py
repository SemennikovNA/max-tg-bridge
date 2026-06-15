import asyncio
import logging
import random
import time

from aiogram import Bot, Dispatcher, F
from aiogram.types import (Message, MessageReactionUpdated, BufferedInputFile,
                           InputMediaPhoto, InputMediaVideo, ReactionTypeEmoji)
from aiogram.exceptions import TelegramRetryAfter

READ_RECEIPT_EMOJI = "👀"
SERVICE_ATTACH = {"INLINE_KEYBOARD", "CONTROL"}

import config
from max_client import WebMaxClient, load_session
from storage import Storage

_logger = logging.getLogger("bridge")


class Bridge:
    def __init__(self):
        self.store = Storage()
        self.max: WebMaxClient = None
        self.bot = Bot(config.TELEGRAM_BOT_TOKEN)
        self.bot.session.middleware(self._flood_mw)
        self.dp = Dispatcher()
        self.group_id = config.TELEGRAM_GROUP_ID
        self.me_id = None
        self._reconnecting = False
        self._hb_task = None
        self.session = None
        self.chats_meta = {}
        self.last_incoming = {}
        self.media_groups = {}
        self._register_handlers()

    async def _flood_mw(self, make_request, bot, method):
        for _ in range(6):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as err:
                _logger.info("flood control: wait %ss", err.retry_after)
                await asyncio.sleep(err.retry_after + 1)
        return await make_request(bot, method)

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
        if not user_id:
            return "?"
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
        # смена аккаунта -> чистим БД, чтобы подтянулись топики нового аккаунта
        saved = self.store.get_meta("account_id")
        if saved and saved != str(self.me_id):
            _logger.info("account changed %s -> %s — clearing DB", saved, self.me_id)
            self.store.clear_all()
        self.store.set_meta("account_id", str(self.me_id))
        chats = payload.get("chats", [])
        self.chats_meta = {c["id"]: c for c in chats}
        await self.cleanup_ignored()
        await self.setup_service_topic()
        await self.init_topics(chats)
        await self.refresh_topic_names()
        await self.catch_up(chats)
        self.max.set_reconnect_callback(self._reconnect)
        self.max.on_packet(self.on_max_packet)

    async def setup_service_topic(self):
        try:
            await self.bot.edit_general_forum_topic(
                self.group_id, name="⚙️ Сервисные функции")
        except Exception as err:
            _logger.info("rename General skipped: %s", err)

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
                    resp = await self.max.login_by_token(
                        session["auth_token"], session["device_id"])
                    self.max.on_packet(self.on_max_packet)
                    _logger.info("MAX reconnected")
                    try:
                        chats = resp.get("payload", {}).get("chats", [])
                        self.chats_meta = {c["id"]: c for c in chats}
                        await self.catch_up(chats)
                    except Exception as err:
                        _logger.warning("catch_up after reconnect failed: %s", err)
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

    async def catch_up(self, chats: list):
        """Подтянуть сообщения, пропущенные пока мост был offline."""
        last_online = self.store.get_meta("last_online_ms")
        if not last_online:
            return  # первый запуск — историю заливает init_topics, catch_up не нужен
        hwm = int(last_online)
        for chat in chats:
            chat_id = chat.get("id")
            if chat_id in config.IGNORED_CHAT_IDS:
                continue
            topic_id = self.store.get_topic(chat_id)
            if topic_id is None:
                continue  # новый чат — историю зальёт init_topics
            last = chat.get("lastMessage") or {}
            last_id = last.get("id")
            if not last_id or self.store.is_seen(last_id):
                continue  # ничего не пропущено
            try:
                msgs = await self.max.get_history(
                    chat_id, last.get("time"), config.CATCHUP_DEPTH)
            except Exception as err:
                _logger.warning("catch_up history %s failed: %s", chat_id, err)
                continue
            # доставляем только пришедшее ПОКА мост был offline (time > last_online)
            fresh = [m for m in msgs
                     if (m.get("time") or 0) > hwm and not self.store.is_seen(m.get("id"))]
            fresh.sort(key=lambda m: m.get("time") or 0)
            if fresh:
                _logger.info("catch_up: chat %s — %d missed", chat_id, len(fresh))
            for m in fresh:
                self.store.mark_seen(m.get("id"))
                await self.deliver_to_tg(topic_id, m, chat_id=chat_id)
                await asyncio.sleep(0.3)

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

    async def ensure_topic_fresh(self, chat_id: int, name: str) -> int:
        """Как ensure_topic, но проверяет, что топик реально жив (не удалён руками)."""
        topic_id = self.store.get_topic(chat_id)
        if topic_id is not None:
            try:
                await self.bot.edit_forum_topic(
                    self.group_id, topic_id, name=name[:128])
                return topic_id
            except Exception as err:
                low = str(err).lower()
                if "not_modified" in low or "not modified" in low:
                    return topic_id  # жив, имя не изменилось
                self.store.del_topic(chat_id)  # удалён руками → пересоздать
        new_topic = await self.create_topic(name)
        self.store.set_topic(chat_id, new_topic)
        return new_topic

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
        # opcode 155 — реакция на сообщение (собеседник реагирует на моё) -> показать в TG
        if packet.get("opcode") == 155 and packet.get("cmd") == 0:
            await self._sync_reaction_to_tg(packet.get("payload", {}))
            return
        # opcode 137 — входящий звонок
        if packet.get("opcode") == 137 and packet.get("cmd") == 0:
            await self._notify_incoming_call(packet.get("payload", {}))
            return
        # opcode 135 — обновление чата; status REMOVED → удалить топик
        if packet.get("opcode") == 135 and packet.get("cmd") == 0:
            chat = packet.get("payload", {}).get("chat", {})
            if chat.get("status") == "REMOVED":
                await self._remove_chat(chat.get("id"))
            return
        # opcode 130 — отметка прочтения; если прочитал собеседник → 👀 на мои сообщения
        if packet.get("opcode") == 130 and packet.get("cmd") == 0:
            p = packet.get("payload", {})
            if (not p.get("setAsUnread") and p.get("userId") != self.me_id):
                await self._read_receipts(p.get("chatId"), p.get("mark"))
            return
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

    async def _sync_reaction_to_tg(self, p: dict):
        max_msg_id = p.get("messageId")
        tg_msg_id = self.store.get_tg_by_max(max_msg_id) if max_msg_id else None
        if tg_msg_id is None:
            return  # реакция не на наше отправленное сообщение
        counters = p.get("counters") or []
        emoji = counters[-1].get("reaction") if counters else READ_RECEIPT_EMOJI
        try:
            await self.bot.set_message_reaction(
                self.group_id, tg_msg_id, reaction=[ReactionTypeEmoji(emoji=emoji)])
        except Exception:
            # эмодзи не из набора Telegram -> пробуем toggle FE0F, иначе 👀
            try:
                alt = emoji.replace("️", "") if "️" in emoji else emoji + "️"
                await self.bot.set_message_reaction(
                    self.group_id, tg_msg_id, reaction=[ReactionTypeEmoji(emoji=alt)])
            except Exception as err:
                _logger.warning("sync reaction %r failed: %s", emoji, err)

    async def _notify_incoming_call(self, p: dict):
        caller = p.get("callerId")
        ctype = p.get("type", "AUDIO")
        emoji = "📹" if ctype == "VIDEO" else "📞"
        kind = "видео" if ctype == "VIDEO" else "аудио"
        name = await self.resolve_name(caller) if caller and caller != self.me_id else "?"
        text = f"{emoji} Входящий {kind}-звонок от {name}"
        chat_id = p.get("chatId") or (self.me_id ^ int(caller) if caller else None)
        topic_id = self.store.get_topic(chat_id) if chat_id else None
        try:
            if topic_id is not None:
                await self.bot.send_message(self.group_id, text, message_thread_id=topic_id)
            else:
                await self.bot.send_message(self.group_id, text)  # General
        except Exception as err:
            _logger.warning("call notify failed: %s", err)

    async def _remove_chat(self, chat_id):
        topic_id = self.store.get_topic(chat_id)
        self.chats_meta.pop(chat_id, None)
        if topic_id is None:
            return
        try:
            await self.bot.delete_forum_topic(self.group_id, topic_id)
        except Exception as err:
            _logger.warning("delete topic %s failed: %s", topic_id, err)
        self.store.del_topic(chat_id)
        _logger.info("chat %s REMOVED -> deleted topic %s", chat_id, topic_id)

    async def _read_receipts(self, chat_id, mark):
        if chat_id is None or mark is None:
            return
        for tg_msg_id in self.store.unread_outbox(chat_id, mark):
            try:
                await self.bot.set_message_reaction(
                    self.group_id, tg_msg_id,
                    reaction=[ReactionTypeEmoji(emoji=READ_RECEIPT_EMOJI)])
                self.store.mark_outbox_read(tg_msg_id)
            except Exception as err:
                _logger.warning("read receipt 👀 failed: %s", err)

    async def deliver_to_tg(self, topic_id: int, msg: dict,
                            chat_id: int = None, history: bool = False):
        sender = msg.get("sender")
        text = (msg.get("text") or "").strip()
        raw_attaches = msg.get("attaches") or []
        attaches = [a for a in raw_attaches
                    if a.get("_type") not in SERVICE_ATTACH]
        # только служебное вложение (CONTROL и т.п.) без текста — не доставляем
        if not text and not attaches and raw_attaches:
            return
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

        # альбом-группируемые: PHOTO и обычное VIDEO (не кружок)
        albumable = [a for a in attaches
                     if a.get("_type") == "PHOTO"
                     or (a.get("_type") == "VIDEO" and a.get("videoType") != 1)]
        _logger.info("deliver: attaches=%d albumable=%d types=%s",
                     len(attaches), len(albumable),
                     [a.get("_type") for a in attaches])
        sent_first = None
        used_caption = False

        if len(albumable) >= 2:
            media = []
            for i, a in enumerate(albumable):
                try:
                    data = await self._attach_bytes(a, chat_id, msg.get("id"))
                    cap = caption if i == 0 else None
                    if a.get("_type") == "PHOTO":
                        media.append(InputMediaPhoto(
                            media=BufferedInputFile(data, "p.jpg"), caption=cap))
                    else:
                        media.append(InputMediaVideo(
                            media=BufferedInputFile(data, "v.mp4"), caption=cap))
                except Exception as err:
                    _logger.warning("album item failed: %s", err)
            # Telegram: media group максимум 10 элементов -> бьём на чанки
            for chunk_start in range(0, len(media), 10):
                chunk = media[chunk_start:chunk_start + 10]
                try:
                    sent = await self.bot.send_media_group(
                        self.group_id, chunk, message_thread_id=topic_id)
                    if sent_first is None and sent:
                        sent_first = sent[0]
                    used_caption = True
                except Exception as err:
                    _logger.warning("send_media_group failed (chunk %d, size %d): %s",
                                    chunk_start, len(chunk), err)
            rest = [a for a in attaches if a not in albumable]
        else:
            rest = attaches

        first = sent_first is None
        for a in rest:
            cap = caption if (first and not used_caption) else None
            sent = await self.deliver_attach(topic_id, chat_id, msg.get("id"), a, cap)
            if first and sent:
                sent_first = sent
            first = False

        self._map(sent_first, chat_id, msg.get("id"))

    def _map(self, sent, chat_id, max_msg_id):
        if sent and chat_id is not None and max_msg_id:
            self.store.set_msg_map(sent.message_id, chat_id, max_msg_id)

    async def _attach_bytes(self, a, chat_id, msg_id):
        t = a.get("_type")
        if t == "PHOTO":
            return await self.max.download_bytes(a["baseUrl"])
        if t == "VIDEO":
            url = await self.max.get_video_url(chat_id, msg_id, a["videoId"])
            return await self.max.download_bytes(url)
        raise RuntimeError(f"no bytes for {t}")

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
            if t == "CALL":
                ctype = a.get("callType", "AUDIO")
                dur = a.get("duration", 0) or 0
                cemoji = "📹" if ctype == "VIDEO" else "📞"
                if a.get("hangupType") == "CANCELED" or dur == 0:
                    body = f"📵 Пропущенный {'видео' if ctype == 'VIDEO' else 'аудио'}-звонок"
                else:
                    m, s = dur // 60000, (dur // 1000) % 60
                    body = f"{cemoji} Звонок · {m}:{s:02d}"
                return await self.bot.send_message(
                    g, f"{caption or ''}\n{body}".strip(),
                    message_thread_id=topic_id)
            if t == "STICKER":
                import gzip
                # 1) анимированный: lottie -> .tgs (gzip lottie json)
                if a.get("stickerType") == "LOTTIE" and a.get("lottieUrl"):
                    try:
                        raw = await self.max.download_bytes(a["lottieUrl"])
                        tgs = gzip.compress(raw)
                        return await self.bot.send_sticker(
                            g, BufferedInputFile(tgs, "s.tgs"),
                            message_thread_id=topic_id)
                    except Exception as e:
                        _logger.info("tgs sticker rejected (%s), fallback to image", e)
                # 2) статичная картинка
                if a.get("url"):
                    data = await self.max.download_bytes(a["url"])
                    try:
                        return await self.bot.send_sticker(
                            g, BufferedInputFile(data, "s.webp"),
                            message_thread_id=topic_id)
                    except Exception:
                        return await self.bot.send_photo(
                            g, BufferedInputFile(data, "s.png"),
                            caption=caption, message_thread_id=topic_id)
                raise RuntimeError("sticker: no url")
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

        @self.dp.message(F.chat.id == self.group_id, F.message_thread_id.is_not(None),
                         F.photo)
        async def _ph(message: Message):
            await self.on_tg_media(message, "photo")

        @self.dp.message(F.chat.id == self.group_id, F.message_thread_id.is_not(None),
                         F.video)
        async def _vd(message: Message):
            await self.on_tg_media(message, "video")

        @self.dp.message(F.chat.id == self.group_id, F.message_thread_id.is_not(None),
                         F.voice)
        async def _vo(message: Message):
            await self.on_tg_media(message, "voice")

        @self.dp.message(F.chat.id == self.group_id, F.message_thread_id.is_not(None),
                         F.video_note)
        async def _vn(message: Message):
            await self.on_tg_media(message, "video_note")

        @self.dp.message(F.chat.id == self.group_id, F.message_thread_id.is_not(None),
                         F.document)
        async def _doc(message: Message):
            await self.on_tg_media(message, "document")

        @self.dp.edited_message(
            F.chat.id == self.group_id,
            F.message_thread_id.is_not(None),
            F.text,
        )
        async def _on_edit(message: Message):
            await self.on_tg_edit(message)

        @self.dp.message(F.chat.id == self.group_id, F.text,
                         F.message_thread_id.is_(None))
        async def _service(message: Message):
            await self.on_service_command(message)

        @self.dp.message(F.chat.id == self.group_id, F.forum_topic_closed)
        async def _closed(message: Message):
            await self.on_topic_closed(message)

    async def on_topic_closed(self, message: Message):
        topic_id = message.message_thread_id
        chat_id = self.store.chat_for_topic(topic_id)
        if chat_id is None:
            return
        try:
            await self.max.delete_chat(chat_id)
            _logger.info("topic %s closed -> delete chat %s in MAX", topic_id, chat_id)
        except Exception as err:
            _logger.warning("delete_chat %s failed: %s", chat_id, err)
        # топик уберётся по эхо opcode 135 REMOVED; подстраховка:
        try:
            await self.bot.delete_forum_topic(self.group_id, topic_id)
        except Exception:
            pass
        self.store.del_topic(chat_id)
        self.chats_meta.pop(chat_id, None)

    async def on_service_command(self, message: Message):
        if message.from_user and message.from_user.is_bot:
            return
        import re
        digits = re.sub(r"[^\d+]", "", (message.text or "").replace("/find", ""))
        if len(re.sub(r"\D", "", digits)) < 10:
            await message.reply(
                "📇 Отправь номер телефона (например +79991234567) — "
                "найду контакт в MAX и создам чат.")
            return
        if not digits.startswith("+"):
            digits = "+" + digits
        try:
            contact = await self.max.search_by_phone(digits)
        except Exception as err:
            await message.reply(f"⚠️ Ошибка поиска: {err}")
            return
        if not contact:
            await message.reply(f"❌ {digits} не найден в MAX.")
            return
        peer_id = int(contact.get("id"))
        name = self._name_from_contact(contact) or f"id{peer_id}"
        chat_id = self.me_id ^ peer_id
        try:
            await self.max.subscribe_chat(chat_id)
        except Exception as err:
            _logger.warning("subscribe_chat failed: %s", err)
        self.chats_meta[chat_id] = {
            "id": chat_id, "type": "DIALOG",
            "participants": {str(self.me_id): 0, str(peer_id): 0},
        }
        self.store.set_name(peer_id, name)
        await self.ensure_topic_fresh(chat_id, name)
        await message.reply(
            f"✅ Чат с *{name}* ({digits}) готов — открой топик «{name}» и пиши.",
            parse_mode="Markdown")

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
        if event.user and event.user.is_bot:
            return  # реакция бота (напр. 👀 read-receipt) — не синкать в MAX
        mapping = self.store.get_max_msg(event.message_id)
        if not mapping:
            return
        emoji = None
        for r in (event.new_reaction or []):
            if getattr(r, "type", None) == "emoji":
                emoji = r.emoji
                break
        chat_id, max_msg_id = mapping
        try:
            if emoji:
                res = await self.max.set_reaction(chat_id, max_msg_id, emoji)
                # MAX не знает эмодзи без/с variation selector -> пробуем toggle FE0F
                if isinstance(res, dict) and res.get("cmd") == 3:
                    alt = (emoji.replace("️", "") if "️" in emoji
                           else emoji + "️")
                    res = await self.max.set_reaction(chat_id, max_msg_id, alt)
                    if isinstance(res, dict) and res.get("cmd") == 3:
                        _logger.warning("reaction %r not supported by MAX", emoji)
            else:
                await self.max.remove_reaction(chat_id, max_msg_id)
        except Exception as err:
            _logger.warning("reaction sync failed: %s", err)

    def _save_sent(self, tg_msg_id, chat_id, result):
        msg_obj = (result or {}).get("payload", {}).get("message", {})
        max_id = msg_obj.get("id")
        if max_id:
            self.store.set_msg_map(tg_msg_id, chat_id, max_id)
            self.store.add_outbox(tg_msg_id, chat_id, msg_obj.get("time") or 0)

    def reply_target(self, message: Message):
        rt = message.reply_to_message
        if rt and rt.message_id != message.message_thread_id:
            mapping = self.store.get_max_msg(rt.message_id)
            if mapping:
                return mapping[1]
        return None

    async def on_tg_reply(self, message: Message):
        if message.from_user and message.from_user.is_bot:
            return
        chat_id = self.store.chat_for_topic(message.message_thread_id)
        if chat_id is None:
            return
        await asyncio.sleep(random.uniform(config.HUMAN_DELAY_MIN, config.HUMAN_DELAY_MAX))
        try:
            result = await self.max.send_text(
                chat_id, message.text, reply_to=self.reply_target(message))
        except Exception as err:
            _logger.warning("max send to %s failed: %s", chat_id, err)
            await message.reply(f"⚠️ Не отправлено в MAX: {err}")
            return
        # сохраняем tg<->max id + outbox (для редактирования/реакций/read-receipt)
        self._save_sent(message.message_id, chat_id, result)
        # B: ответил -> помечаем последнее входящее прочитанным в MAX
        last = self.last_incoming.get(chat_id)
        if last:
            try:
                await self.max.mark_read(chat_id, last[0], last[1])
            except Exception as err:
                _logger.warning("mark_read (reply) failed: %s", err)

    async def _upload_media(self, message: Message, kind: str, chat_id: int):
        from vkmax.functions.uploads import upload_photo, upload_video, upload_file
        if kind == "photo":
            buf = await self.bot.download(message.photo[-1].file_id)
            return await upload_photo(self.max, chat_id, buf)
        if kind == "video":
            buf = await self.bot.download(message.video.file_id)
            return await upload_video(self.max, chat_id, buf)
        if kind == "video_note":
            buf = await self.bot.download(message.video_note.file_id)
            attach = await upload_video(self.max, chat_id, buf)
            attach["videoType"] = 1
            return attach
        if kind == "document":
            buf = await self.bot.download(message.document.file_id)
            return await upload_file(
                self.max, chat_id, buf, message.document.file_name or "file.bin")
        return None

    async def on_tg_media(self, message: Message, kind: str):
        if message.from_user and message.from_user.is_bot:
            return
        if kind == "voice":
            await message.reply("🎤 Голосовые MAX (web) не поддерживает — не отправлено.")
            return
        if message.media_group_id:
            self._buffer_media_group(message, kind)
            return
        chat_id = self.store.chat_for_topic(message.message_thread_id)
        if chat_id is None:
            return
        await asyncio.sleep(random.uniform(config.HUMAN_DELAY_MIN, config.HUMAN_DELAY_MAX))
        from vkmax.functions.messages import send_message
        try:
            attach = await self._upload_media(message, kind, chat_id)
            if not attach:
                return
            result = await send_message(
                self.max, chat_id, message.caption or "",
                attaches=[attach], reply_to=self.reply_target(message))
            self._save_sent(message.message_id, chat_id, result)
        except Exception as err:
            _logger.warning("tg->max media %s failed: %s", kind, err)
            await message.reply(f"⚠️ Медиа не отправлено в MAX: {err}")

    def _buffer_media_group(self, message: Message, kind: str):
        gid = message.media_group_id
        grp = self.media_groups.get(gid)
        if grp is None:
            grp = {"items": []}
            self.media_groups[gid] = grp
            asyncio.create_task(self._flush_media_group(gid))
        grp["items"].append((kind, message))

    async def _flush_media_group(self, gid: str):
        await asyncio.sleep(1.5)
        grp = self.media_groups.pop(gid, None)
        if not grp or not grp["items"]:
            return
        first = grp["items"][0][1]
        chat_id = self.store.chat_for_topic(first.message_thread_id)
        if chat_id is None:
            return
        caption = next((m.caption for _, m in grp["items"] if m.caption), "") or ""
        await asyncio.sleep(random.uniform(config.HUMAN_DELAY_MIN, config.HUMAN_DELAY_MAX))
        from vkmax.functions.messages import send_message
        attaches = []
        for k, m in grp["items"]:
            try:
                a = await self._upload_media(m, k, chat_id)
                if a:
                    attaches.append(a)
            except Exception as err:
                _logger.warning("group media %s failed: %s", k, err)
        if not attaches:
            return
        try:
            result = await send_message(
                self.max, chat_id, caption,
                attaches=attaches, reply_to=self.reply_target(first))
            self._save_sent(first.message_id, chat_id, result)
        except Exception as err:
            _logger.warning("group send failed: %s", err)
            await first.reply(f"⚠️ Альбом не отправлен в MAX: {err}")

    # ---------- lifecycle ----------

    async def _heartbeat_loop(self):
        while True:
            try:
                config.HEARTBEAT_FILE.write_text(str(int(time.time())))
                self.store.set_meta("last_online_ms", str(int(time.time() * 1000)))
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
