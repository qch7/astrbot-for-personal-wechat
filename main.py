# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 shuizhaodemumu
# Modified 2026 by qch7 (fork): group context messages, member nicknames,
# per-group whitelist/profile defaults, bot-offline alerts, image replies,
# @-mentioning the asker, WeChat reply-style hints, incoming images/voice,
# quote replies, incoming files, private chats with friends and a WebUI
# management panel (panel.py, views/panel).

import asyncio
import copy
import hashlib
import json
import mimetypes
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode

import aiohttp
from astrbot.api import logger, sp
from astrbot.api.event import MessageChain
from astrbot.api.event import filter as event_filter
from astrbot.api.message_components import At, File, Image, Plain, Record
from astrbot.api.platform import (
    AstrBotMessage,
    AstrMessageEvent,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.api.star import Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.session_lock import session_lock_manager

try:  # Loaded as a package inside AstrBot; tests import main as a module.
    from .panel import (
        LogBuffer,
        WeChatPanel,
        note_error,
        note_poll,
        note_received,
        note_sent,
        register_adapter,
        unregister_adapter,
    )
except ImportError:
    from panel import (
        LogBuffer,
        WeChatPanel,
        note_error,
        note_poll,
        note_received,
        note_sent,
        register_adapter,
        unregister_adapter,
    )

LOCAL_BRIDGE = "http://127.0.0.1:6174"
MEMBER_NAME_TTL = 3600
MEMBER_REFRESH_INTERVAL = 600


MEDIA_SUCCESS_STATES = {"accepted", "completed", "succeeded"}
MEDIA_TERMINAL_STATES = MEDIA_SUCCESS_STATES | {
    "available",
    "failed",
    "partial",
    "cancelled",
    "expired",
    "needs_review",
}
IMAGE_MAX_BYTES = 26214383  # bridge mediaSendMaxBytes.image
MENTION_SEPARATOR = "\u2005"  # WeChat mention terminator (four-per-em space)
QUOTE_APP_MESSAGE_TYPE = 57  # type 49 "reply with quote"
FILE_APP_MESSAGE_TYPE = 6
FILE_MAX_BYTES = 50 * 1024 * 1024
MEDIA_MESSAGE_KINDS = {3: "image", 34: "voice", 47: "sticker"}
MEDIA_PLACEHOLDERS = {
    "image": "[图片]",
    "voice": "[语音] ",
    "sticker": "[动画表情]",
    "file": "[文件]",
}
MEDIA_VARIANTS = {
    "image": ("standard", "original", "thumbnail"),
    "voice": ("normal",),
    "file": ("original",),
}
MEDIA_PENDING_STATES = {"pending", "queued", "running", "downloading"}
MEDIA_FILE_TTL = 3600
VOICE_MAX_BYTES = 26214383
THREAD_KIND_TTL = 3600
STYLE_HINT_BODY = (
    "微信不渲染 Markdown：不要用 **加粗**、# 标题、表格或代码块，"
    "用自然的短段落。可以适当使用微信自带表情代码，例如 [捂脸] [旺柴] [破涕为笑] "
    "[抱拳] [机智] [裂开] [OK] [强] [爱心]，微信会把它们显示成表情；不要用 [微笑]，"
    "它在中文网络里常被理解为嘲讽。"
)
DEFAULT_STYLE_HINT = (
    "你正在微信群里回复。"
    + STYLE_HINT_BODY
    + "群成员显示为「昵称(微信ID)」：昵称可以随意修改甚至"
    "和别人重名，判断谁是谁、某人@了谁时，以括号里的微信 ID 为准。"
)
DEFAULT_PRIVATE_STYLE_HINT = "你正在微信私聊里回复。" + STYLE_HINT_BODY


class BridgeHTTPError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"WeChat bridge HTTP {status}")
        self.status = status


CRON_DELIVERED_EXTRA = "native_wechat_cron_delivered"
# Status remarks models append to scheduled results, e.g. "（任务完成，提醒已送达。）".
CRON_STATUS_NOTE = re.compile(
    r"[（(][^（）()]*(?:任务完成|已送达|已发送|已提醒|提醒已|已完成)[^（）()]*[）)]"
)


def cron_result_text(text):
    """The part of a scheduled task's final answer meant for the group."""
    return CRON_STATUS_NOTE.sub("", text or "").strip()


def is_self_mention(native):
    """Return whether a native message explicitly mentions the bot account."""
    mentions = (native or {}).get("mentions") or {}
    return mentions.get("state") == "known" and mentions.get("self") is True


def clean_markdown(text, strip=True):
    """Remove Markdown markup that WeChat would show literally."""
    text = re.sub(r"^```[^\n]*\n?", "", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.S)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    return text.strip() if strip else text


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def image_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None, None


def safe_filename(value):
    name = re.split(r"[\\/]", value or "attachment")[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", name).strip(" .")
    name = name.encode("utf-8")[:180].decode("utf-8", errors="ignore")
    return name or "attachment"


def outgoing_segments(chain, images=True):
    """Split a message chain into ordered text, image and file parts.

    Text parts are token lists of ("plain", str) and ("at", user_id), so At
    components (e.g. from send_message_to_user's mention_user) become real
    WeChat mentions.
    """
    segments, tokens = [], []

    def flush():
        if any(k == "at" or v.strip() for k, v in tokens):
            segments.append(("text", list(tokens)))
        tokens.clear()

    for part in chain:
        if isinstance(part, Plain):
            tokens.append(("plain", part.text))
        elif isinstance(part, At) and part.qq not in (None, "", "all"):
            tokens.append(("at", str(part.qq)))
        elif images and isinstance(part, Image):
            flush()
            segments.append(("image", part))
        elif isinstance(part, File):
            flush()
            segments.append(("file", part))
    flush()
    return segments


class NativeWeChatEvent(AstrMessageEvent):
    def __init__(self, message, platform):
        super().__init__(
            message.message_str, message, platform.meta(), message.session_id
        )
        self.platform = platform
        self.delivery = 0

    async def send(self, message: MessageChain):
        segments = outgoing_segments(message.chain, self.platform.send_images)
        if not segments:
            return
        if not self.platform.is_directed(self.message_obj):
            # Context-only messages never get replies (e.g. proactive replies or
            # plugins reacting to every group message); drop instead of failing.
            logger.info("Native WeChat dropped a reply to an unmentioned message")
            return
        await self.platform.ensure_reply_allowed(self.message_obj)
        thread = self.message_obj.session_id
        for kind, value in segments:
            key = hashlib.sha256(
                f"{self.message_obj.message_id}:{self.delivery}".encode()
            ).hexdigest()
            # Only the first text part of the first reply @-mentions the asker.
            mention = self.delivery == 0 and kind == "text"
            self.delivery += 1
            if kind == "text":
                await self.platform.send_text(
                    thread,
                    value,
                    key,
                    self.platform.mention_for(self.message_obj) if mention else None,
                )
            elif kind == "image":
                await self.platform.send_image(thread, value, key)
            else:
                await self.platform.send_file(thread, value, key)
        await super().send(message)

    async def send_streaming(self, generator, use_fallback=False):
        parts = []
        async for message in generator:
            parts.extend(
                part
                for part in message.chain
                if isinstance(part, Plain | Image | File | At)
            )
        if parts:
            await self.send(MessageChain(parts))


@register_platform_adapter(
    "native_wechat",
    "本机个人微信：指定群 @ 回复与好友私聊（文字、图片、文件）",
    default_config_tmpl={
        "server_url": "http://127.0.0.1:6174",
        "account_id": "",
        "token_file": "",
        "state_file": "",
        "allowed_groups": [],
        "chat_provider_id": "",
        "context_messages": False,
        "context_skip_prefixes": ["/"],
        "resolve_member_names": True,
        "send_images": True,
        "mention_sender": False,
        "strip_markdown": True,
        "proactive_window_seconds": 120,
        "proactive_messages": False,
        "show_member_ids": True,
        "text_mentions": False,
        "receive_media": True,
        "attach_recent_image_seconds": 120,
        "attach_recent_file_seconds": 120,
        "friend_whitelist": False,
        "allowed_friends": [],
    },
    adapter_display_name="本机个人微信",
    support_streaming_message=False,
)
class NativeWeChatAdapter(Platform):
    def __init__(self, config, settings, event_queue):
        super().__init__(config, event_queue)
        self.allowed_groups = set(config.get("allowed_groups", []))
        if any(not group.endswith("@chatroom") for group in self.allowed_groups):
            raise ValueError("Only explicit WeChat group IDs are supported")
        self.account_id = config["account_id"]
        self.base = config["server_url"].rstrip("/")
        if self.base != LOCAL_BRIDGE:
            raise ValueError("This adapter is restricted to the local bridge")
        self.prefix = "/v1/accounts/" + quote(self.account_id, safe="")
        self.state_file = Path(config["state_file"])
        # Unmentioned text is only forwarded as group-chat context; replies stay
        # limited to explicit mentions (see reply()).
        self.context_messages = bool(config.get("context_messages", False))
        self.context_skip_prefixes = tuple(
            p for p in config.get("context_skip_prefixes", ["/"]) if p
        )
        self.resolve_member_names = bool(config.get("resolve_member_names", True))
        self.send_images = bool(config.get("send_images", True))
        self.mention_sender = bool(config.get("mention_sender", False))
        self.strip_markdown = bool(config.get("strip_markdown", True))
        # Plugins such as meme managers deliver images through send_by_session;
        # allow that only shortly after a mention in the same allowed group.
        self.proactive_window = max(
            0, int(config.get("proactive_window_seconds", 120) or 0)
        )
        self.recent_mentions = {}
        self.proactive_sent = 0
        # When enabled, any proactive send (scheduled reminders, plugins, the
        # send_message_to_user tool) may reach the allowed groups at any time.
        self.proactive_messages = bool(config.get("proactive_messages", False))
        # Nicknames can be changed to impersonate others; show the WeChat ID.
        self.show_member_ids = bool(config.get("show_member_ids", True))
        # Also treat a typed "@<bot group nickname>" as a mention (WeChat only
        # attaches mention metadata when the member is picked from the list).
        self.text_mentions = bool(config.get("text_mentions", False))
        # Media is group context until an @ refers to a recent image or file.
        self.receive_media = bool(config.get("receive_media", True))
        self.attach_window = max(
            0, int(config.get("attach_recent_image_seconds", 120) or 0)
        )
        self.file_attach_window = max(
            0, int(config.get("attach_recent_file_seconds", 120) or 0)
        )
        # Private chats: every friend may talk to the bot, unless the friend
        # whitelist is enabled; then only allowed_friends (empty = nobody).
        # Only threads the bridge reports as "direct" count as private chats,
        # so system and official accounts never reach the model.
        self.friend_whitelist = bool(config.get("friend_whitelist", False))
        self.allowed_friends = {
            str(friend).strip()
            for friend in config.get("allowed_friends") or []
            if str(friend).strip()
        }
        if any(friend.endswith("@chatroom") for friend in self.allowed_friends):
            raise ValueError("allowed_friends takes WeChat IDs, not group IDs")
        self.thread_kinds = {}
        self.contact_names = {}
        self.media_dir = self.state_file.parent / "media"
        # AstrBot's workspace-scoped file tools may read its temporary directory.
        namespace = hashlib.sha256(str(self.state_file.resolve()).encode()).hexdigest()
        self.file_dir = Path(get_astrbot_temp_path()) / "native_wechat" / namespace[:16]
        self.recent_attachments = {}
        self.attachments_by_server_id = {}
        self.tasks = set()
        self.member_names = {}
        self.member_refreshed = {}
        self.http = None
        self.own_id = ""
        self.started = datetime.now(UTC)
        self.cursor = "0"
        self.seen = []
        self.health_file = self.state_file.with_suffix(".health.json")
        if self.state_file.exists():
            state = json.loads(self.state_file.read_text())
            if state.get("account_id") != self.account_id:
                raise ValueError("State belongs to another account")
            self.cursor = str(state["cursor"])
            self.seen = state.get("seen", [])[-2048:]

    def meta(self):
        return PlatformMetadata(
            "native_wechat",
            "Local WeChat text adapter",
            self.config["id"],
            support_streaming_message=False,
            # Lets AstrBot offer send_message_to_user to the model.
            support_proactive_message=self.proactive_messages,
        )

    async def request(self, method, path, **kwargs):
        async with self.http.request(
            method, self.base + self.prefix + path, **kwargs
        ) as response:
            if response.status >= 400:
                raise BridgeHTTPError(response.status)
            return await response.json()

    def friend_permitted(self, thread):
        """Whether the friend policy admits a private thread ID (kind unchecked)."""
        return (
            isinstance(thread, str)
            and bool(thread)
            and not thread.endswith("@chatroom")
            and thread != self.own_id
            and (not self.friend_whitelist or thread in self.allowed_friends)
        )

    def is_private_thread(self, thread):
        """A permitted friend whose chat was recently verified as direct."""
        if not self.friend_permitted(thread):
            return False
        cached = self.thread_kinds.get(thread)
        return bool(cached) and cached[0] == "direct" and cached[1] > time.monotonic()

    def thread_permitted(self, thread):
        """Approved group or verified private chat (uses cached lookups only)."""
        return thread in self.allowed_groups or self.is_private_thread(thread)

    async def thread_allowed(self, thread):
        """Like thread_permitted(), looking up the chat kind when needed.

        Transient bridge failures propagate (the poll loop retries); a chat
        the bridge does not know is remembered as not private.
        """
        if thread in self.allowed_groups:
            return True
        if not self.friend_permitted(thread):
            return False
        cached = self.thread_kinds.get(thread)
        if not cached or cached[1] <= time.monotonic():
            try:
                info = await self.request("GET", "/threads/" + quote(thread, safe=""))
                kind = info.get("kind") if info.get("id") == thread else None
            except BridgeHTTPError as exc:
                if exc.status != 404:
                    raise
                kind = None
            self.thread_kinds[thread] = (kind, time.monotonic() + THREAD_KIND_TTL)
        return self.is_private_thread(thread)

    def is_directed(self, source):
        """Whether a message may be answered: private chats, or a group @."""
        return source.type == MessageType.FRIEND_MESSAGE or self.is_mentioned(
            source.raw_message
        )

    def cached_name(self, group, username):
        cached = self.member_names.get((group, username))
        return cached[0] if cached else None

    def is_mentioned(self, native):
        """Explicit WeChat mention metadata, or a typed @<bot nickname> when enabled."""
        if is_self_mention(native):
            return True
        if not self.text_mentions or not self.own_id:
            return False
        name = self.cached_name((native or {}).get("threadId"), self.own_id)
        if not name or name == self.own_id:
            return False
        pattern = "@" + re.escape(name) + r"(?:\s|$)"  # \s covers U+2005
        return re.search(pattern, (native or {}).get("body") or "") is not None

    def display_name(self, name, username):
        if self.show_member_ids and name and name != username:
            return f"{name}({username})"
        return name or username

    def message_kind(self, native):
        """Classify supported text, images, voice, stickers and ordinary files."""
        kind = native.get("type")
        if kind == 1:
            return "text"
        if kind == 49 and native.get("appMessageType") == QUOTE_APP_MESSAGE_TYPE:
            return "text"  # reply with a quote: its body is ordinary text
        if self.receive_media:
            if kind == 49 and native.get("appMessageType") == FILE_APP_MESSAGE_TYPE:
                return "file"
            return MEDIA_MESSAGE_KINDS.get(kind)
        return None

    def convert(self, native, origin, require_mention=True):
        """Admit only fresh messages in approved groups or private chats.

        Args:
            native: Canonical native message from the authenticated bridge.
            origin: Event origin supplied by the bridge.
            require_mention: When False, unmentioned group text and media are
                admitted as context only (no At component, so AstrBot does not
                wake the model), except text starting with a skipped command
                prefix. Group media cannot carry an @ and is never answered.
                Private chats need no mention: every message is answered.

        Returns:
            An AstrBot message, or None when the message is outside the policy.
            Media messages carry a text placeholder until attach_media().
            Private chats are admitted only after thread_allowed() verified them.
        """
        kind = self.message_kind(native)
        thread = native.get("threadId")
        private = thread not in self.allowed_groups and self.is_private_thread(thread)
        if (
            kind is None
            or origin != "live"
            or native.get("accountId") != self.account_id
            or not (thread in self.allowed_groups or private)
            or native.get("direction") != "incoming"
            or not native.get("senderId")
            or native.get("senderId") == self.own_id
            or (private and native.get("senderId") != thread)
            or (kind == "text" and not native.get("body"))
            or native.get("recall")
        ):
            return None
        mentioned = kind == "text" and not private and self.is_mentioned(native)
        if not (mentioned or private) and (
            require_mention
            or (
                kind == "text"
                and native["body"].lstrip().startswith(self.context_skip_prefixes)
            )
        ):
            return None
        try:
            created = datetime.fromisoformat(native["createdAt"])
            if (
                created < self.started
                or (datetime.now(UTC) - created).total_seconds() > 300
            ):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        message = AstrBotMessage()
        message.type = (
            MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
        )
        message.self_id = self.own_id
        message.session_id = thread
        message.group_id = "" if private else thread
        message.message_id = native["id"]
        message.sender = MessageMember(native["senderId"], native["senderId"])
        message.wechat_kind = kind
        if kind == "text":
            message.message_str = native["body"]
            message.message = (
                [At(qq=self.own_id), Plain(native["body"])]
                if mentioned
                else [Plain(native["body"])]
            )
        else:
            message.message_str = MEDIA_PLACEHOLDERS[kind]
            message.message = [Plain(MEDIA_PLACEHOLDERS[kind])]
        message.timestamp = int(created.timestamp())
        message.raw_message = native
        return message

    async def run(self):
        token = Path(self.config["token_file"]).read_text().strip()
        self.http = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=False,
        )
        platform_id = self.config.get("id", "")
        register_adapter(platform_id, self)
        try:
            while True:
                try:
                    account = await self.request("GET", "")
                    if not account.get("authenticated") or not account.get(
                        "ownUsername"
                    ):
                        raise RuntimeError("WeChat account is not authenticated")
                    self.own_id = account["ownUsername"]
                    self.health_file.parent.mkdir(parents=True, exist_ok=True)
                    self.health_file.write_text(
                        json.dumps(
                            {
                                "updated_at": time.time(),
                                "connected": account.get("connection"),
                                "cursor": self.cursor,
                            }
                        )
                    )
                    self.health_file.chmod(0o600)
                    page = await self.request(
                        "GET", f"/events?after={self.cursor}&limit=100"
                    )
                    note_poll(platform_id)
                    for event in page["items"]:
                        message = None
                        data = event.get("data", {})
                        thread = data.get("threadId")
                        if event["kind"] == "message.committed" and (
                            thread in self.allowed_groups
                            or (
                                # Only fresh incoming private messages cost a
                                # chat-kind lookup.
                                data.get("origin") == "live"
                                and data.get("direction") != "outgoing"
                                and await self.thread_allowed(thread)
                            )
                        ):
                            native = await self.request(
                                "GET", "/messages/" + quote(data["messageId"], safe="")
                            )
                            if (
                                native.get("id") != data["messageId"]
                                or native.get("threadId") != data["threadId"]
                            ):
                                raise RuntimeError("WeChat message identity mismatch")
                            if native["id"] not in self.seen:
                                self.remember_attachment(native)
                                if (
                                    self.text_mentions
                                    and self.own_id
                                    and thread in self.allowed_groups
                                ):
                                    # Cache the bot's own nickname for typed @s.
                                    await self.member_name(
                                        native["threadId"], self.own_id
                                    )
                                message = self.convert(
                                    native,
                                    data.get("origin"),
                                    require_mention=not self.context_messages,
                                )
                                if message:
                                    self.seen = (self.seen + [native["id"]])[-2048:]
                                    await self.describe_people(message, native)
                        self.cursor = event["cursor"]
                        # Persist before dispatch to avoid repeated replies after a crash.
                        state = {
                            "account_id": self.account_id,
                            "cursor": self.cursor,
                            "seen": self.seen,
                        }
                        self.state_file.parent.mkdir(parents=True, exist_ok=True)
                        temporary = self.state_file.with_suffix(".tmp")
                        temporary.write_text(json.dumps(state))
                        temporary.chmod(0o600)
                        temporary.replace(self.state_file)
                        if message:
                            attachment_ref = self.referenced_attachment(message)
                            quoted_file = self.quoted_file(message)
                            if (
                                message.wechat_kind in MEDIA_VARIANTS
                                or attachment_ref
                                or quoted_file
                            ):
                                # Downloads may wait for the bridge; never stall
                                # the event loop for later messages.
                                self.spawn(
                                    self.finish_and_commit(message, attachment_ref)
                                )
                            else:
                                self.dispatch(message)
                    await asyncio.sleep(1 if len(page["items"]) < 100 else 0)
                except asyncio.CancelledError:
                    raise
                except (
                    aiohttp.ClientError,
                    TimeoutError,
                    RuntimeError,
                    ValueError,
                    KeyError,
                    OSError,
                ) as exc:
                    logger.error("Native WeChat receive failed: %s", str(exc))
                    note_error(platform_id, exc)
                    await asyncio.sleep(5)
        finally:
            unregister_adapter(platform_id, self)
            for task in list(self.tasks):
                task.cancel()
            await self.http.close()

    def dispatch(self, message):
        self.commit_event(NativeWeChatEvent(message, self))
        note_received(self.config.get("id", ""))
        if message.type == MessageType.FRIEND_MESSAGE:
            self.recent_mentions[message.session_id] = (time.monotonic(), message)
            logger.info("Native WeChat admitted one private message")
        elif self.is_mentioned(message.raw_message):
            self.recent_mentions[message.session_id] = (time.monotonic(), message)
            logger.info("Native WeChat admitted one mentioned group message")
        else:
            logger.debug("Native WeChat forwarded one group context message")

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def remember_attachment(self, native):
        """Track incoming images/files for a later @ or quote in the same chat.

        Private images are only kept for quotes. A file also accompanies the
        next private question, which may arrive while its download is running.
        """
        kind = self.message_kind(native)
        if (
            kind not in ("image", "file")
            or native.get("direction") != "incoming"
            or not self.thread_permitted(native.get("threadId"))
        ):
            return
        if native["threadId"] in self.allowed_groups or kind == "file":
            key = (native["threadId"], native.get("senderId"))
            self.recent_attachments[key] = (time.monotonic(), kind, native["id"])
        if native.get("serverId"):
            key = (native["threadId"], str(native["serverId"]))
            self.attachments_by_server_id[key] = (kind, native["id"])
            while len(self.attachments_by_server_id) > 500:
                self.attachments_by_server_id.pop(
                    next(iter(self.attachments_by_server_id))
                )

    def referenced_attachment(self, message):
        """Return (kind, message ID) for a quoted or recent image/file.

        A quote wins; otherwise use the sender's own latest attachment within
        its configured window, once. Attachments from other chats never match.
        """
        native = message.raw_message
        if message.wechat_kind != "text" or not self.is_directed(message):
            return None
        quoted = native.get("quote") or {}
        if quoted.get("serverId"):
            found = self.attachments_by_server_id.get(
                (native["threadId"], str(quoted["serverId"]))
            )
            if found:
                return found
            if self.quoted_file(message):
                return None
        key = (native["threadId"], native["senderId"])
        recent = self.recent_attachments.get(key)
        if recent:
            window = (
                self.attach_window if recent[1] == "image" else self.file_attach_window
            )
            if window and time.monotonic() - recent[0] <= window:
                self.recent_attachments.pop(key, None)
                return recent[1:]
        return None

    def quoted_file(self, message):
        quoted = message.raw_message.get("quote") or {}
        return (
            message.wechat_kind == "text"
            and self.is_directed(message)
            and quoted.get("type") == 49
            and quoted.get("appMessageType") == FILE_APP_MESSAGE_TYPE
            and bool(quoted.get("serverId"))
        )

    async def resolve_quoted_file(self, message):
        """Find a quoted original in this chat's saved history after a restart."""
        if not self.quoted_file(message):
            return None
        native = message.raw_message
        thread = native["threadId"]
        if not await self.thread_allowed(thread):
            return None
        quoted = native["quote"]
        cursor = None
        for _ in range(10):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = await self.request(
                "GET",
                "/threads/" + quote(thread, safe="") + "/messages?" + urlencode(params),
            )
            for original in page.get("items", []):
                if str(original.get("serverId")) != str(quoted["serverId"]):
                    continue
                if (
                    original.get("accountId") != self.account_id
                    or original.get("threadId") != thread
                ):
                    raise RuntimeError("Quoted WeChat file identity mismatch")
                if (
                    original.get("recall")
                    or original.get("direction") != "incoming"
                    or self.message_kind(original) != "file"
                    or (
                        quoted.get("senderId")
                        and quoted["senderId"] != original.get("senderId")
                    )
                ):
                    return None
                result = ("file", original["id"])
                self.attachments_by_server_id[(thread, str(quoted["serverId"]))] = (
                    result
                )
                if len(self.attachments_by_server_id) > 500:
                    self.attachments_by_server_id.pop(
                        next(iter(self.attachments_by_server_id))
                    )
                return result
            next_cursor = page.get("nextCursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return None

    async def finish_and_commit(self, message, attachment_ref=None):
        try:
            if attachment_ref is None and self.quoted_file(message):
                attachment_ref = await self.resolve_quoted_file(message)
                if attachment_ref is None:
                    label = "[引用文件未能获取，请重新发送原文件]"
                    message.message.append(Plain(label))
                    message.message_str += "\n" + label
            if message.wechat_kind in MEDIA_VARIANTS:
                await self.attach_media(message)
            if attachment_ref:
                await self.attach_media(message, attachment_ref)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # media is best effort; still deliver the text
            logger.warning("Native WeChat media handling failed: %s", str(exc))
            if (
                message.wechat_kind == "file"
                or self.quoted_file(message)
                or (attachment_ref and attachment_ref[0] == "file")
            ):
                label = "[文件]（未能获取）"
                if attachment_ref or self.quoted_file(message):
                    message.message.append(Plain(label))
                    message.message_str += "\n" + label
                else:
                    message.message = [Plain(label)]
                    message.message_str = label
        self.dispatch(message)

    async def attach_media(self, message, attachment_ref=None):
        """Attach a downloaded Image/Record/File, or an explicit failure label."""
        kind, message_id = attachment_ref or (message.wechat_kind, message.message_id)
        path = await self.download_media(
            message_id, kind, wait=60 if kind == "file" else 15
        )
        if not path and attachment_ref and kind == "image":
            return
        if not path:
            components = [Plain(MEDIA_PLACEHOLDERS[kind].strip() + "（未能获取）")]
        elif kind == "image":
            components = [Image.fromFileSystem(str(path))]
        elif kind == "file":
            name = re.sub(r"^file-[0-9a-f]{24}-", "", path.name)
            components = [Plain(f"[文件：{name}]"), File(name=name, file=str(path))]
        else:
            # AstrBot's speech-to-text replaces the Record with its transcript.
            components = [
                Plain(MEDIA_PLACEHOLDERS["voice"]),
                Record.fromFileSystem(str(path)),
            ]
        if kind == "file":
            label = components[0].text
            message.message_str = (
                message.message_str + "\n" + label if attachment_ref else label
            )
        if attachment_ref:
            message.message.extend(components)
        else:
            message.message = components

    async def download_media(self, message_id, kind, wait=15):
        """Fetch an image/voice/file asset, starting uncached file downloads once.

        Waits up to `wait` seconds while the bridge is still downloading it.
        Returns the file path, or None when no usable asset becomes available.
        """
        variants = MEDIA_VARIANTS[kind]
        deadline = time.monotonic() + wait
        transfer_started = False
        while True:
            try:
                page = await self.request(
                    "GET", "/messages/" + quote(message_id, safe="") + "/media"
                )
            except RuntimeError as exc:
                logger.warning("WeChat media lookup failed: %s", str(exc))
                return None
            assets = {
                a.get("variant"): a
                for a in page.get("items") or []
                if a.get("kind") == kind
            }
            if kind == "file":
                for asset in assets.values():
                    if asset.get("accountId", self.account_id) != self.account_id or (
                        asset.get("messageId", message_id) != message_id
                    ):
                        raise RuntimeError("WeChat file asset identity mismatch")
                    sizes = (asset.get("byteLength"), asset.get("advertisedByteLength"))
                    if any(isinstance(n, int) and n > FILE_MAX_BYTES for n in sizes):
                        logger.warning("WeChat file exceeds the 50 MiB receive limit")
                        return None
                    if (
                        asset.get("state") == "pending"
                        and not asset.get("jobId")
                        and not transfer_started
                    ):
                        await self.request(
                            "POST",
                            "/messages/"
                            + quote(message_id, safe="")
                            + "/media-transfers",
                            json={
                                "messageId": message_id,
                                "assetId": asset["id"],
                                "quality": "best",
                            },
                        )
                        transfer_started = True
            rank = next(
                (
                    i
                    for i, v in enumerate(variants)
                    if (assets.get(v) or {}).get("state") == "available"
                    and assets[v].get("id")
                ),
                len(variants),
            )
            # Wait for a better variant still downloading, then settle for
            # the best one available (e.g. a thumbnail).
            better_pending = any(
                (assets.get(v) or {}).get("state") in MEDIA_PENDING_STATES
                for v in variants[:rank]
            )
            if not better_pending or time.monotonic() >= deadline:
                if rank < len(variants):
                    return await self.save_asset(assets[variants[rank]], kind)
                return None
            await asyncio.sleep(1)

    async def save_asset(self, asset, kind):
        limit = {
            "image": IMAGE_MAX_BYTES,
            "voice": VOICE_MAX_BYTES,
            "file": FILE_MAX_BYTES,
        }[kind]
        data = await self.request_bytes(
            "/media/" + quote(asset["id"], safe="") + "/content", limit
        )
        expected = asset.get("byteLength")
        if isinstance(expected, int) and len(data) != expected:
            logger.warning(
                "WeChat media is incomplete (%d of %d bytes)", len(data), expected
            )
            return None
        checksum = asset.get("sha256")
        if isinstance(checksum, str) and hashlib.sha256(data).hexdigest() != checksum:
            logger.warning("WeChat media checksum does not match")
            return None
        if kind == "image":
            _, extension = image_mime(data)
            if not extension:
                logger.warning("WeChat image asset has an unknown format")
                return None
        elif kind == "voice":
            if data[:4] != b"RIFF":
                logger.warning("WeChat voice asset is not WAV")
                return None
            extension = "wav"
        digest = hashlib.sha256(data).hexdigest()[:24]
        if kind == "file":
            path = (
                self.file_dir / f"file-{digest}-{safe_filename(asset.get('filename'))}"
            )
        else:
            path = self.media_dir / f"{kind}-{digest}.{extension}"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.prune_media()
        # Concurrent references to the same attachment must see complete bytes.
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                temporary.chmod(0o600)
                stream.write(data)
            except BaseException:
                stream.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def prune_media(self):
        cutoff = time.time() - MEDIA_FILE_TTL
        for old in (*self.media_dir.glob("*"), *self.file_dir.glob("*")):
            try:
                if old.is_file() and old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass

    async def request_bytes(self, path, limit):
        """Read a whole response body (a single read() may return only part)."""
        async with self.http.get(self.base + self.prefix + path) as response:
            if response.status >= 400:
                raise BridgeHTTPError(response.status)
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > limit:
                    raise ValueError("WeChat media is too large")
                chunks.append(chunk)
        if not size:
            raise ValueError("WeChat media is empty")
        return b"".join(chunks)

    async def ensure_reply_allowed(self, source):
        """Re-read the source so recalled or ineligible messages get no reply."""
        if not await self.thread_allowed(source.session_id):
            raise ValueError("Reply target is not approved")
        current = await self.request(
            "GET", "/messages/" + quote(source.message_id, safe="")
        )
        if not self.convert(current, "live", require_mention=True):
            raise ValueError("Original message is no longer eligible for a reply")

    async def reply(self, source, text, key, mention=None):
        await self.ensure_reply_allowed(source)
        await self.send_text(source.session_id, text, key, mention)

    async def person_name(self, thread, username):
        """Group nickname in groups, contact name in private chats."""
        if thread in self.allowed_groups:
            return await self.member_name(thread, username)
        return await self.contact_name(username)

    async def describe_people(self, message, native):
        """Name the sender and mentioned members as nickname(WeChat ID).

        The plain nickname is kept on the message for @-mentions in replies;
        the model only sees the ID-qualified form, so renamed members cannot
        impersonate each other.
        """
        group, sender = native["threadId"], native["senderId"]
        name = await self.person_name(group, sender)
        message.wechat_sender_name = name
        message.sender = MessageMember(sender, self.display_name(name, sender))
        notes = []
        mentions = native.get("mentions") or {}
        others = [
            uid
            for uid in mentions.get("userIds") or []
            if isinstance(uid, str) and uid not in (self.own_id, sender)
        ]
        if others and self.show_member_ids:
            labels = [
                self.display_name(await self.member_name(group, uid), uid)
                for uid in others[:20]
            ]
            notes.append("（这条消息@了：" + "、".join(labels) + "）")
        quoted = native.get("quote") if native.get("type") == 49 else None
        if isinstance(quoted, dict):
            author = quoted.get("senderId")
            if author == self.own_id:
                who = "你（机器人）"
            elif isinstance(author, str) and author:
                who = self.display_name(await self.person_name(group, author), author)
            else:
                who = "某人"
            text = quoted.get("embeddedText")
            if quoted.get("type") == 3:
                text = "[图片]"
            elif quoted.get("type") == 34:
                text = "[语音]"
            elif not isinstance(text, str) or not text.strip():
                text = "[消息]"
            text = text.strip()
            if len(text) > 120:
                text = text[:120] + "…"
            notes.append(f"（引用了 {who} 的消息：{text}）")
        if not notes:
            return
        note = "\n" + "\n".join(notes)
        message.message_str += note
        for part in message.message:
            if isinstance(part, Plain):
                part.text += note
                break

    def mention_for(self, source):
        """Build a mention of the asker, or None when disabled, private or unsafe."""
        if (
            not self.mention_sender
            or not source.sender
            or source.type == MessageType.FRIEND_MESSAGE
        ):
            return None
        user_id = source.sender.user_id
        label = (
            getattr(source, "wechat_sender_name", None)
            or source.sender.nickname
            or user_id
            or ""
        )
        return self.make_mention(user_id, label)

    def make_mention(self, user_id, label):
        """Validate a mention target and label as the bridge requires them."""
        label = (label or user_id or "").strip()
        if (
            not user_id
            or user_id == self.own_id
            or not re.fullmatch(r"[A-Za-z0-9_@.-]{1,256}", user_id)
            or user_id == "notify@all"
            or not label
            or len(label.encode()) > 1024
            or re.search(r"[\u0000-\u001f\u007f-\u009f\u2005]", label)
        ):
            return None
        return {"userId": user_id, "label": label}

    async def compose_text(self, group, tokens, labels=None):
        """Build a bridge text body from ("plain"|"at", value) tokens.

        Mentions become "@label" + U+2005 with UTF-16 positions; unusable
        targets fall back to plain "@label" text. Returns None when empty.
        """
        labels = dict(labels or {})
        text, ids, positions = "", [], []
        for kind, value in tokens:
            if kind == "plain":
                text += (
                    clean_markdown(value, strip=False) if self.strip_markdown else value
                )
                continue
            label = labels.get(value)
            if not label:
                label = await self.member_name(group, value)
            mention = self.make_mention(value, label)
            if not mention:
                text += f"@{(label or value).strip()} "
                continue
            if value not in ids:
                ids.append(value)
            span = f"@{mention['label']}{MENTION_SEPARATOR}"
            start = utf16_len(text)
            text += span
            positions.append(
                {
                    "start": start,
                    "end": start + utf16_len(span),
                    "index": ids.index(value),
                }
            )
        # Trim ordinary whitespace only; a trailing U+2005 ends a mention.
        trimmed = text.lstrip(" \t\r\n")
        shift = utf16_len(text) - utf16_len(trimmed)
        text = trimmed.rstrip(" \t\r\n")
        if not text.strip():
            return None
        if not ids:
            return {"threadId": group, "text": text}
        for position in positions:
            position["start"] -= shift
            position["end"] -= shift
        return {
            "kind": "text",
            "threadId": group,
            "text": text,
            "mentions": {"userIds": ids, "positions": positions},
        }

    async def send_text(self, group, text, key, mention=None):
        """Send text (a str or token list); `mention` @s that user first.

        `group` is the thread: an allowed group or a verified private chat,
        where @-mentions do not exist and are dropped.
        """
        if not self.thread_permitted(group):
            raise ValueError("Reply target is not approved")
        tokens = [("plain", text)] if isinstance(text, str) else list(text)
        if group not in self.allowed_groups:
            tokens = [token for token in tokens if token[0] == "plain"]
            mention = None
        labels = {}
        if mention:
            labels[mention["userId"]] = mention["label"]
            if ("at", mention["userId"]) not in tokens:
                tokens.insert(0, ("at", mention["userId"]))
        body = await self.compose_text(group, tokens, labels)
        if not body:
            return
        intent = await self.request(
            "POST", "/sends", headers={"Idempotency-Key": key}, json=body
        )
        deadline = time.monotonic() + 65
        while (
            intent["state"] in ("queued", "submitting") and time.monotonic() < deadline
        ):
            await asyncio.sleep(1)
            intent = await self.request("GET", "/sends/" + quote(intent["id"], safe=""))
        if (
            intent.get("accountId", self.account_id) != self.account_id
            or intent.get("destination", group) != group
        ):
            raise RuntimeError("WeChat send receipt identity mismatch")
        if intent["state"] not in ("accepted", "sent", "server_accepted"):
            raise RuntimeError(
                "WeChat delivery is unconfirmed; do not automatically resend"
            )
        note_sent(self.config.get("id", ""))

    async def send_image(self, group, image, key):
        """Upload one image to the bridge and send it to an allowed thread."""
        await self.send_media(group, image, key, "image")

    async def send_file(self, group, file, key):
        """Upload a File component while preserving its display name."""
        await self.send_media(group, file, key, "file")

    async def send_media(self, group, component, key, kind):
        """Send an attachment once and verify the account-scoped media job."""
        if not self.thread_permitted(group):
            raise ValueError("Reply target is not approved")
        if kind == "file":
            file_path = await component.get_file()
            if not file_path:
                raise ValueError("WeChat outgoing file is unavailable")
            path = Path(file_path)
            filename = safe_filename(component.name or path.name)
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            limit = FILE_MAX_BYTES
        else:
            path = Path(await component.convert_to_file_path())
            limit = IMAGE_MAX_BYTES
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if not data or len(data) > limit:
            raise ValueError(f"WeChat {kind} is empty or larger than {limit} bytes")
        if kind == "image":
            mime, extension = image_mime(data)
            if not mime:
                raise ValueError("Unsupported image format")
            filename = f"image.{extension}"
        form = aiohttp.FormData()
        form.add_field("file", data, filename=filename, content_type=mime)
        source = await self.request("POST", "/uploads", data=form)
        job = await self.request(
            "POST",
            "/sends",
            headers={"Idempotency-Key": key},
            json={
                "threadId": group,
                "kind": kind,
                "sourceId": source["id"],
                "standardSourceId": None,
                "thumbnailSourceId": None,
                "playSeconds": None,
                "durationMs": None,
            },
        )
        deadline = time.monotonic() + 65
        while (
            job.get("state") not in MEDIA_TERMINAL_STATES
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(1)
            job = await self.request("GET", "/jobs/" + quote(job["id"], safe=""))
        if (
            job.get("accountId", self.account_id) != self.account_id
            or job.get("threadId", group) != group
        ):
            raise RuntimeError("WeChat media job identity mismatch")
        if job.get("state") not in MEDIA_SUCCESS_STATES:
            raise RuntimeError(
                f"WeChat {kind} delivery is unconfirmed ({job.get('state')}); "
                "do not automatically resend"
            )
        note_sent(self.config.get("id", ""))

    async def member_name(self, group, username):
        """Resolve a member's group nickname, falling back to the WeChat ID.

        Names are cached for an hour. A member missing from the bridge's cache
        triggers at most one member refresh per group every ten minutes; any
        failure keeps the ID so message handling never blocks on names.
        """
        if not self.resolve_member_names:
            return username
        key = (group, username)
        cached = self.member_names.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        path = (
            "/groups/" + quote(group, safe="") + "/members/" + quote(username, safe="")
        )
        name = None
        try:
            lookup = await self.request("GET", path)
            member = lookup.get("member") if lookup.get("state") == "present" else None
            if member:
                for field in ("roomDisplayName", "displayName", "mentionName"):
                    value = member.get(field)
                    if isinstance(value, str) and value.strip():
                        name = value.strip()
                        break
            elif (
                time.monotonic() - self.member_refreshed.get(group, -1e9)
                > MEMBER_REFRESH_INTERVAL
            ):
                self.member_refreshed[group] = time.monotonic()
                await self.request(
                    "POST",
                    "/groups/" + quote(group, safe="") + "/members/refresh",
                    json={},
                )
        except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError) as exc:
            logger.warning("Could not resolve WeChat member name: %s", str(exc))
        # Unresolved names are retried soon, e.g. after a member refresh lands.
        ttl = MEMBER_NAME_TTL if name else 60
        name = name or username
        self.member_names[key] = (name, time.monotonic() + ttl)
        return name

    async def contact_name(self, username):
        """Resolve a friend's display name, falling back to the WeChat ID."""
        if not self.resolve_member_names:
            return username
        cached = self.contact_names.get(username)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        name = None
        try:
            contact = await self.request("GET", "/contacts/" + quote(username, safe=""))
            for field in ("displayName", "remark", "nickname"):
                value = contact.get(field)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    break
        except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError) as exc:
            logger.warning("Could not resolve WeChat contact name: %s", str(exc))
        ttl = MEMBER_NAME_TTL if name else 60
        name = name or username
        self.contact_names[username] = (name, time.monotonic() + ttl)
        return name

    def cron_event(self, thread, message_type=MessageType.GROUP_MESSAGE):
        """The scheduled-task event currently sending to `thread`, if any.

        AstrBot runs a cron job's agent inside event_context(CronMessageEvent),
        so the job (and the member who created it) is visible while its
        send_message_to_user call, or the plugin's fallback delivery, reaches
        this adapter.
        """
        try:
            from astrbot.core.platform.astr_message_event import (
                current_message_event,
            )
        except ImportError:
            return None
        event = current_message_event.get()
        if event is None or event.get_platform_name() != "cron":
            return None
        payload = event.get_extra("cron_payload") or {}
        session = f"{self.config['id']}:{message_type.value}:{thread}"
        if payload.get("session") != session:
            return None
        return event

    def cron_payload(self, thread, message_type=MessageType.GROUP_MESSAGE):
        event = self.cron_event(thread, message_type)
        return (event.get_extra("cron_payload") or {}) if event else None

    async def send_by_session(self, session, message_chain):
        """Deliver proactive messages to an allowed group or private chat.

        With proactive_messages enabled, anything may be sent to an allowed
        thread (scheduled reminders, plugins, send_message_to_user). Otherwise
        only follow-ups (e.g. memes) within proactive_window_seconds of a
        mention or private message are allowed. Scheduled reminders in groups
        @-mention their creator.
        """
        group = getattr(session, "session_id", None)
        message_type = getattr(session, "message_type", None)
        if message_type == MessageType.GROUP_MESSAGE:
            allowed = group in self.allowed_groups
        elif message_type == MessageType.FRIEND_MESSAGE:
            allowed = group not in self.allowed_groups and await self.thread_allowed(
                group
            )
        else:
            allowed = False
        if not allowed:
            raise ValueError(
                "Proactive sending is limited to allowed WeChat groups and friends"
            )
        recent = self.recent_mentions.get(group)
        in_window = bool(
            recent and time.monotonic() - recent[0] <= self.proactive_window
        )
        if not self.proactive_messages and not in_window:
            raise ValueError(
                "Proactive sending is limited to groups mentioned in the last "
                f"{self.proactive_window} seconds"
            )
        segments = outgoing_segments(message_chain.chain, self.send_images)
        if not segments:
            return
        if not self.proactive_messages:
            await self.ensure_reply_allowed(recent[1])
        mention = None
        payload = self.cron_payload(group, message_type)
        explicit = any(
            k == "at" for kind, value in segments if kind == "text" for k, _ in value
        )
        if (
            payload
            and self.mention_sender
            and not explicit
            and message_type == MessageType.GROUP_MESSAGE
        ):
            creator = str(payload.get("sender_id") or "")
            name = await self.member_name(group, creator) if creator else None
            # Only @ a creator the group knows (not e.g. WebUI-created jobs).
            if name and name != creator:
                mention = self.make_mention(creator, name)
        first_text = True
        for kind, value in segments:
            self.proactive_sent += 1
            key = hashlib.sha256(
                f"proactive:{group}:{time.time_ns()}:{self.proactive_sent}".encode()
            ).hexdigest()
            if kind == "text":
                await self.send_text(group, value, key, mention if first_text else None)
                first_text = False
            elif kind == "image":
                await self.send_image(group, value, key)
            else:
                await self.send_file(group, value, key)
        if payload:
            # Tells the plugin's fallback that this run has already delivered.
            self.cron_event(group, message_type).set_extra(CRON_DELIVERED_EXTRA, True)
            logger.info("Native WeChat delivered a scheduled message to %s", group)

    async def terminate(self):
        if self.http and not self.http.closed:
            await self.http.close()


@register(
    "astrbot_plugin_native_wechat",
    "shuizhaodemumu",
    "本机微信指定群及好友接入",
    "0.9.0",
)
class NativeWeChatPlugin(Star):
    def __init__(self, context, config=None):
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self.watch_task = None
        # Offline-alert tracking per platform ID, also shown in the panel.
        self.alert_state = {}
        self.panel_logs = LogBuffer()
        self.panel = WeChatPanel(self, self.panel_logs)
        try:
            self.panel.register(context)
        except Exception as exc:  # the adapter must work without its panel
            logger.error("Could not register the WeChat panel: %s", str(exc))

    async def initialize(self):
        """Discover allowed groups, apply their personas and start offline alerts."""
        self.panel_logs.attach()
        await self.configure_groups()
        platforms = [
            p
            for p in self.context.astrbot_config_mgr.default_conf.get("platform", [])
            if p.get("type") == "native_wechat" and p.get("enable")
        ]
        if self.watch_task:
            self.watch_task.cancel()
        self.watch_task = None
        if platforms and self.alert_sessions():
            self.watch_task = asyncio.create_task(self.watch_accounts(platforms))

    async def terminate(self):
        if self.watch_task:
            self.watch_task.cancel()
            self.watch_task = None
        self.panel_logs.detach()

    @event_filter.on_llm_response()
    async def deliver_cron_result(self, event, resp):
        """Send a scheduled task's final answer when the model did not send it.

        AstrBot only delivers cron output that the model sends with
        send_message_to_user; models often just answer instead, and that
        answer is otherwise lost. Runs inside the cron event context, so the
        adapter still @-mentions the task's creator.
        """
        if event.get_platform_name() != "cron" or event.get_extra(CRON_DELIVERED_EXTRA):
            return
        payload = event.get_extra("cron_payload") or {}
        session = str(payload.get("session") or "")
        parts = session.split(":", 2)
        if (
            len(parts) != 3
            or parts[1] not in ("GroupMessage", "FriendMessage")
            or not parts[2]
        ):
            return
        platform_id = parts[0]
        platforms = self.context.astrbot_config_mgr.default_conf.get("platform", [])
        if not any(
            p.get("id") == platform_id
            and p.get("type") == "native_wechat"
            and p.get("enable")
            for p in platforms
        ):
            return
        if resp is None or getattr(resp, "role", "assistant") != "assistant":
            return
        text = cron_result_text(getattr(resp, "completion_text", "") or "")
        if not text:
            return
        try:
            await self.context.send_message(session, MessageChain().message(text))
            event.set_extra(CRON_DELIVERED_EXTRA, True)
        except Exception as exc:  # e.g. proactive messages disabled
            logger.error("Could not deliver scheduled WeChat message: %s", str(exc))

    @event_filter.on_llm_request()
    async def add_wechat_style_hint(self, event, req):
        """Tell the model how WeChat renders replies (no Markdown, face codes)."""
        if event.get_platform_name() != "native_wechat":
            return
        if event.is_private_chat():
            hint = self.config.get(
                "private_reply_style_hint", DEFAULT_PRIVATE_STYLE_HINT
            )
        else:
            hint = self.config.get("reply_style_hint", DEFAULT_STYLE_HINT)
        if isinstance(hint, str) and hint.strip():
            req.system_prompt = f"{req.system_prompt or ''}\n\n{hint.strip()}".strip()

    async def configure_groups(self):
        manager = self.context.astrbot_config_mgr
        rules = self.config.setdefault("groups", [])
        indexed = {}
        for rule in rules:
            key = (rule.get("platform_id"), rule.get("group_id"))
            if key in indexed:
                raise ValueError(f"Duplicate WeChat group setting: {key}")
            indexed[key] = rule
        original_rules = copy.deepcopy(rules)
        for platform in manager.default_conf.get("platform", []):
            if platform.get("type") != "native_wechat" or not platform.get("enable"):
                continue
            await self.configure_private_chats(platform)
            for group in platform.get("allowed_groups", []):
                origin = f"{platform['id']}:GroupMessage:{group}"
                profile_id = manager.get_conf_info(origin)["id"]
                routes = manager.ucr.umop_to_conf_id
                if (
                    profile_id == "default"
                    or routes.get(origin) != profile_id
                    or any(k != origin and v == profile_id for k, v in routes.items())
                ):
                    profile = copy.deepcopy(dict(manager.get_conf(origin)))
                    profile["provider_settings"]["wake_prefix"] = ""
                    profile["provider_settings"]["streaming_response"] = False
                    model = profile["agent_runner"]["config"]["model"]
                    model["provider_id"] = platform["chat_provider_id"]
                    model["fallback_provider_ids"] = []
                    if platform.get("context_messages"):
                        # Let the model tell group members apart in shared context.
                        profile["provider_settings"]["identifier"] = True
                    profile_id = await manager.create_conf(
                        profile, name=f"微信 · {group}"
                    )
                    Path(manager.confs[profile_id].config_path).chmod(0o600)
                    await manager.ucr.update_routing_data(
                        {
                            origin: profile_id,
                            **{k: v for k, v in routes.items() if k != origin},
                        }
                    )
                profile = manager.confs[profile_id]
                runner = profile["agent_runner"]
                if runner.get("runner_type", "local") != "local":
                    logger.warning("Skipping non-local WeChat profile: %s", origin)
                    continue
                self.apply_profile_defaults(origin, profile, platform)
                persona_config = runner["config"].setdefault("persona", {})
                key = (platform["id"], group)
                rule = indexed.get(key)
                if rule is None:
                    rule = {
                        "__template_key": "group",
                        "platform_id": platform["id"],
                        "group_id": group,
                        "group_name": group,
                        "persona_id": persona_config.get("persona_id", "default"),
                        "new_conversation_on_change": True,
                    }
                    rules.append(rule)
                    indexed[key] = rule
                name = await self.fetch_group_name(platform, group)
                if name:
                    rule["group_name"] = name
                name = rule.get("group_name") or group
                if manager.get_conf_info(origin).get("name") != f"微信 · {name}":
                    await manager.update_conf_info(profile_id, name=f"微信 · {name}")
                await self.apply_group_persona(origin, profile, rule)
        if rules != original_rules and hasattr(self.config, "save_config"):
            self.config.save_config()

    async def configure_private_chats(self, platform):
        """Route the platform's private chats to one isolated profile.

        The profile is created once from whatever applied to them before (a
        copy, so other channels keep their settings) and routed with
        "<platform>:FriendMessage:*". Its AstrBot ID whitelist is switched off
        on creation: inherited entries would silently drop every friend, and
        the adapter already enforces friend_whitelist/allowed_friends itself.
        Nothing happens when the friend whitelist is on and empty.
        """
        if platform.get("friend_whitelist") and not any(
            str(friend).strip() for friend in platform.get("allowed_friends") or []
        ):
            return None
        manager = self.context.astrbot_config_mgr
        pattern = f"{platform['id']}:FriendMessage:*"
        routes = manager.ucr.umop_to_conf_id
        profile_id = routes.get(pattern)
        if (
            not profile_id
            or profile_id == "default"
            or profile_id not in manager.confs
            or any(k != pattern and v == profile_id for k, v in routes.items())
        ):
            profile = copy.deepcopy(dict(manager.get_conf(pattern)))
            settings = profile["provider_settings"]
            settings["wake_prefix"] = ""
            settings["streaming_response"] = False
            # Friends share long-term memory with other channels; say who it is.
            settings["identifier"] = True
            model = profile["agent_runner"]["config"]["model"]
            model["provider_id"] = platform["chat_provider_id"]
            model["fallback_provider_ids"] = []
            profile.setdefault("platform_settings", {})["enable_id_white_list"] = False
            profile_id = await manager.create_conf(profile, name="微信 · 私聊")
            Path(manager.confs[profile_id].config_path).chmod(0o600)
            await manager.ucr.update_routing_data(
                {
                    pattern: profile_id,
                    **{k: v for k, v in routes.items() if k != pattern},
                }
            )
            logger.info("Routed WeChat private chats (%s) to %s", pattern, profile_id)
        return profile_id

    async def fetch_group_name(self, platform, group):
        """Read a group label without changing membership or sending messages.

        Args:
            platform: Enabled local bridge platform configuration.
            group: An already allowed group ID.

        Returns:
            The bridge label, or None if unavailable.
        """
        if platform.get("server_url", "").rstrip("/") != "http://127.0.0.1:6174":
            return None
        try:
            token = Path(platform["token_file"]).read_text().strip()
            url = (
                "http://127.0.0.1:6174/v1/accounts/"
                + quote(platform["account_id"], safe="")
                + "/groups/"
                + quote(group, safe="")
            )
            async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=3),
                trust_env=False,
            ) as client:
                async with client.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        return None
                    data = await response.json()
                    if data.get("id") != group:
                        return None
                    name = data.get("displayName") or data.get("name")
                    return name if isinstance(name, str) and name.strip() else None
        except (TimeoutError, OSError, aiohttp.ClientError, ValueError):
            logger.warning("Could not refresh WeChat group label: %s", group)
            return None

    def apply_profile_defaults(self, origin, profile, platform):
        """Keep a group's isolated profile usable for this adapter.

        Adds the group session to the profile's ID whitelist when the whitelist
        is enabled (it is inherited from the default profile and would silently
        stop every event), and disables proactive replies when context messages
        are forwarded, because only explicit mentions may be answered.

        Returns:
            Whether the profile was changed and saved.
        """
        changed = False
        settings = profile.setdefault("platform_settings", {})
        if settings.get("enable_id_white_list"):
            whitelist = settings.setdefault("id_whitelist", [])
            if origin not in whitelist:
                whitelist.append(origin)
                changed = True
                logger.info("Added %s to its profile's ID whitelist", origin)
        if platform.get("context_messages"):
            active = (profile.get("provider_ltm_settings") or {}).get("active_reply")
            if isinstance(active, dict) and active.get("enable"):
                active["enable"] = False
                changed = True
                logger.info("Disabled proactive replies for %s", origin)
        if changed:
            profile.save_config()
        return changed

    def alert_sessions(self):
        sessions = self.config.get("alert_sessions") or []
        return [s.strip() for s in sessions if isinstance(s, str) and s.strip()]

    async def account_status(self, platform):
        """Return (online, detail) for a platform's bridge account."""
        if platform.get("server_url", "").rstrip("/") != LOCAL_BRIDGE:
            return False, "桥接地址无效"
        try:
            token = Path(platform["token_file"]).read_text().strip()
            async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
                trust_env=False,
            ) as client:
                url = (
                    LOCAL_BRIDGE
                    + "/v1/accounts/"
                    + quote(platform["account_id"], safe="")
                )
                async with client.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        return False, f"桥接返回 HTTP {response.status}"
                    account = await response.json()
        except (
            TimeoutError,
            OSError,
            aiohttp.ClientError,
            ValueError,
            KeyError,
        ) as exc:
            return False, f"桥接不可达（{type(exc).__name__}）"
        if not account.get("authenticated"):
            return False, "账号已退出登录"
        if account.get("connection") != "online":
            return False, f"连接状态 {account.get('connection')}"
        return True, "在线"

    async def check_accounts(self, platforms, state, now):
        """One offline-alert round; state maps platform ID to its tracking dict."""
        delay = max(30, int(self.config.get("alert_after_seconds", 120) or 120))
        for platform in platforms:
            entry = state.setdefault(platform["id"], {"since": None, "alerted": False})
            online, detail = await self.account_status(platform)
            entry.update(detail=detail, checked_at=time.time())
            if online:
                if entry["alerted"]:
                    await self.alert(f"✅ 微信机器人（{platform['id']}）已恢复在线。")
                entry.update(since=None, alerted=False)
                continue
            if entry["since"] is None:
                entry["since"] = now
            if not entry["alerted"] and now - entry["since"] >= delay:
                entry["alerted"] = True
                await self.alert(
                    f"⚠️ 微信机器人（{platform['id']}）离线：{detail}。"
                    "请确认小号仍在手机上登录，然后在服务器上重新登录桥接。"
                )

    async def watch_accounts(self, platforms):
        state = self.alert_state
        state.clear()
        while True:
            try:
                await self.check_accounts(platforms, state, time.monotonic())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the watcher alive
                logger.error("WeChat offline watcher failed: %s", str(exc))
            await asyncio.sleep(30)

    async def alert(self, text):
        logger.warning(text)
        for session in self.alert_sessions():
            try:
                await self.context.send_message(session, MessageChain().message(text))
            except Exception as exc:  # one broken session must not block others
                logger.error("Could not send WeChat alert to %s: %s", session, str(exc))

    async def apply_group_persona(self, origin, profile, rule):
        """Apply a selection under the same lock used by incoming model requests.

        Args:
            origin: Exact platform and group session identity.
            profile: The group's isolated AstrBot configuration.
            rule: Persona selection and conversation-switch preference.
        """
        selected = rule.get("persona_id")
        if not selected:
            return
        try:
            await self.context.persona_manager.get_persona(selected)
        except ValueError:
            logger.warning(
                "Unknown WeChat persona %r for %s; keeping current setting",
                selected,
                origin,
            )
            return
        async with session_lock_manager.acquire_lock(origin):
            conversations = self.context.conversation_manager
            cid = await conversations.get_curr_conversation_id(origin)
            conversation = (
                await conversations.get_conversation(origin, cid) if cid else None
            )
            service = (
                await sp.get_async("umo", origin, "session_service_config", {}) or {}
            )
            persona = profile["agent_runner"]["config"].setdefault("persona", {})
            previous = (
                service.get("persona_id")
                or (conversation.persona_id if conversation else None)
                or persona.get("persona_id", "default")
            )
            if previous != selected:
                if rule.get("new_conversation_on_change", True):
                    await conversations.new_conversation(
                        origin,
                        title=f"{rule.get('group_name') or origin} · {selected}",
                        persona_id=selected,
                    )
                elif cid:
                    await conversations.update_conversation(
                        origin,
                        conversation_id=cid,
                        persona_id=selected,
                    )
            if persona.get("persona_id") != selected:
                persona["persona_id"] = selected
                profile.save_config()
            if service.get("persona_id") and service["persona_id"] != selected:
                service["persona_id"] = selected
                await sp.put_async("umo", origin, "session_service_config", service)
            logger.info(
                "WeChat persona for %s (%s): %s",
                rule.get("group_name"),
                origin,
                selected,
            )
