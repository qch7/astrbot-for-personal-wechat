import asyncio
import copy
import hashlib
import json
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Image, Plain, Record
from astrbot.api.platform import MessageType

from main import (
    DEFAULT_STYLE_HINT,
    NativeWeChatAdapter,
    NativeWeChatEvent,
    NativeWeChatPlugin,
    clean_markdown,
    outgoing_segments,
)

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc000000301010018dd8db40000000049454e44ae426082"
)


class Profile(dict):
    def __init__(self, data, path=None):
        super().__init__(data)
        self.config_path = str(path) if path else "unused"
        self.save_config = Mock()


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {
            "id": "wechat-test",
            "type": "native_wechat",
            "enable": True,
            "server_url": "http://127.0.0.1:6174",
            "account_id": "account-test",
            "allowed_groups": ["123@chatroom"],
            "state_file": str(Path(self.temp.name) / "state.json"),
            "token_file": str(Path(self.temp.name) / "token"),
            "chat_provider_id": "test-model",
        }
        Path(self.config["token_file"]).write_text("test")
        self.adapter = NativeWeChatAdapter(self.config, {}, asyncio.Queue())
        self.adapter.own_id = "wxid_bot"
        self.native = {
            "id": "message1",
            "accountId": "account-test",
            "threadId": "123@chatroom",
            "senderId": "wxid_person",
            "type": 1,
            "direction": "incoming",
            "body": "@bot hello",
            "mentions": {"state": "known", "self": True, "all": False},
            "createdAt": datetime.now(UTC).isoformat(),
        }

    def test_only_explicit_mentions_in_target_group(self):
        message = self.adapter.convert(self.native, "live")
        self.assertIsNotNone(message)
        event = NativeWeChatEvent(message, self.adapter)
        self.assertEqual(event.get_group_id(), "123@chatroom")
        self.assertEqual(event.get_self_id(), "wxid_bot")
        variants = [
            {"threadId": "other@chatroom"},
            {"threadId": "wxid_person"},
            {"accountId": "another-account"},
            {"direction": "outgoing"},
            {"senderId": "wxid_bot"},
            {"type": 43},
            {"mentions": {"state": "unknown", "self": True}},
            {"mentions": {"state": "known", "all": True, "self": False}},
            {"createdAt": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
            {"recall": {"state": "recalled"}},
            {"createdAt": "invalid"},
        ]
        for change in variants:
            with self.subTest(change=change):
                self.assertIsNone(self.adapter.convert(self.native | change, "live"))
        self.assertIsNone(self.adapter.convert(self.native, "history"))

    async def test_poll_deduplicates_and_checkpoints(self):
        event = {
            "cursor": "1",
            "kind": "message.committed",
            "data": {
                "threadId": "123@chatroom",
                "messageId": "message1",
                "origin": "live",
            },
        }
        self.adapter.request = AsyncMock(
            side_effect=[
                {
                    "authenticated": True,
                    "ownUsername": "wxid_bot",
                    "connection": "online",
                },
                {"items": [event, event | {"cursor": "2"}]},
                self.native,
                {"state": "present", "member": {"displayName": "Person"}},
                self.native,
                asyncio.CancelledError(),
            ]
        )
        with self.assertRaises(asyncio.CancelledError):
            await self.adapter.run()
        self.assertEqual(self.adapter._event_queue.qsize(), 1)
        sender = self.adapter._event_queue.get_nowait().message_obj.sender
        self.assertEqual(
            (sender.user_id, sender.nickname), ("wxid_person", "Person(wxid_person)")
        )
        state = json.loads(Path(self.config["state_file"]).read_text())
        self.assertEqual(state["cursor"], "2")
        self.assertEqual(state["seen"], ["message1"])
        self.assertTrue(self.adapter.http.closed)

    async def test_recall_before_reply_blocks_send(self):
        source = self.adapter.convert(self.native, "live")
        self.adapter.request = AsyncMock(
            return_value=self.native | {"recall": {"state": "recalled"}}
        )
        with self.assertRaises(ValueError):
            await self.adapter.reply(source, "reply", "key")
        self.adapter.request.assert_awaited_once()

    async def test_submit_once_and_check_receipt(self):
        source = self.adapter.convert(self.native, "live")
        self.adapter.request = AsyncMock(
            side_effect=[
                self.native,
                {"id": "send1", "state": "queued"},
                {
                    "id": "send1",
                    "state": "server_accepted",
                    "accountId": "account-test",
                    "destination": "123@chatroom",
                },
            ]
        )
        await self.adapter.reply(source, "reply", "stable-key")
        posts = [x for x in self.adapter.request.await_args_list if x.args[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].kwargs["headers"]["Idempotency-Key"], "stable-key")

    async def test_profile_does_not_change_global_settings(self):
        profile_path = Path(self.temp.name) / "profile.json"
        profile_path.write_text("{}")
        default = {
            "platform": [self.config],
            "provider_settings": {"wake_prefix": "咪咪", "streaming_response": True},
            "agent_runner": {"config": {"model": {"provider_id": "old-model"}}},
        }
        previous = copy.deepcopy(default)
        manager = SimpleNamespace(
            default_conf=default,
            get_conf_info=lambda origin: {"id": "default"},
            get_conf=lambda origin: default,
            update_conf_info=AsyncMock(),
            create_conf=AsyncMock(return_value="profile-id"),
            confs={"profile-id": Profile(copy.deepcopy(default), profile_path)},
            ucr=SimpleNamespace(
                umop_to_conf_id={"qq::": "qq-profile"}, update_routing_data=AsyncMock()
            ),
        )
        plugin = NativeWeChatPlugin(SimpleNamespace(astrbot_config_mgr=manager))
        plugin.fetch_group_name = AsyncMock(return_value="Test group")
        plugin.apply_group_persona = AsyncMock()
        await plugin.initialize()
        self.assertEqual(plugin.config["groups"][0]["group_name"], "Test group")
        self.assertEqual(default, previous)
        supplied = manager.create_conf.await_args.args[0]
        self.assertEqual(supplied["provider_settings"]["wake_prefix"], "")
        self.assertEqual(
            supplied["agent_runner"]["config"]["model"]["provider_id"], "test-model"
        )
        routes = manager.ucr.update_routing_data.await_args.args[0]
        self.assertEqual(routes["wechat-test:GroupMessage:123@chatroom"], "profile-id")
        self.assertEqual(routes["qq::"], "qq-profile")


class ContextMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {
            "id": "wechat-test",
            "type": "native_wechat",
            "enable": True,
            "server_url": "http://127.0.0.1:6174",
            "account_id": "account-test",
            "allowed_groups": ["123@chatroom"],
            "state_file": str(Path(self.temp.name) / "state.json"),
            "token_file": str(Path(self.temp.name) / "token"),
            "chat_provider_id": "test-model",
            "context_messages": True,
        }
        Path(self.config["token_file"]).write_text("test")
        self.adapter = NativeWeChatAdapter(self.config, {}, asyncio.Queue())
        self.adapter.own_id = "wxid_bot"
        self.plain = {
            "id": "message2",
            "accountId": "account-test",
            "threadId": "123@chatroom",
            "senderId": "wxid_person",
            "type": 1,
            "direction": "incoming",
            "body": "我叫小明",
            "mentions": {"state": "known", "self": False, "all": False},
            "createdAt": datetime.now(UTC).isoformat(),
        }

    def test_unmentioned_text_is_context_without_at(self):
        message = self.adapter.convert(self.plain, "live", require_mention=False)
        self.assertIsNotNone(message)
        self.assertFalse(any(isinstance(c, At) for c in message.message))
        self.assertIsInstance(message.message[0], Plain)
        self.assertIsNone(self.adapter.convert(self.plain, "live"))
        mentioned = self.plain | {"mentions": {"state": "known", "self": True}}
        message = self.adapter.convert(mentioned, "live", require_mention=False)
        self.assertIsInstance(message.message[0], At)

    def test_context_keeps_other_admission_rules(self):
        variants = [
            {"threadId": "other@chatroom"},
            {"senderId": "wxid_bot"},
            {"direction": "outgoing"},
            {"type": 43},
            {"recall": {"state": "recalled"}},
            {"createdAt": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
            {"body": "/reset"},
            {"body": "  /new"},
        ]
        for change in variants:
            with self.subTest(change=change):
                self.assertIsNone(
                    self.adapter.convert(self.plain | change, "live", False)
                )
        command = self.plain | {
            "body": "/help",
            "mentions": {"state": "known", "self": True},
        }
        self.assertIsNotNone(self.adapter.convert(command, "live", False))

    async def test_context_messages_are_never_answered(self):
        message = self.adapter.convert(self.plain, "live", require_mention=False)
        event = NativeWeChatEvent(message, self.adapter)
        self.adapter.request = AsyncMock()
        await event.send(MessageChain([Plain("插话"), Image.fromFileSystem("x.png")]))
        self.adapter.request.assert_not_awaited()

    async def test_reply_requires_mention_even_for_context_source(self):
        source = self.adapter.convert(self.plain, "live", require_mention=False)
        self.adapter.request = AsyncMock(return_value=self.plain)
        with self.assertRaises(ValueError):
            await self.adapter.reply(source, "reply", "key")
        posts = [x for x in self.adapter.request.await_args_list if x.args[0] == "POST"]
        self.assertEqual(posts, [])

    async def test_poll_uses_member_nickname(self):
        event = {
            "cursor": "1",
            "kind": "message.committed",
            "data": {
                "threadId": "123@chatroom",
                "messageId": "message2",
                "origin": "live",
            },
        }
        self.adapter.request = AsyncMock(
            side_effect=[
                {
                    "authenticated": True,
                    "ownUsername": "wxid_bot",
                    "connection": "online",
                },
                {"items": [event]},
                self.plain,
                {
                    "state": "present",
                    "member": {"displayName": "小明", "roomDisplayName": None},
                },
                asyncio.CancelledError(),
            ]
        )
        with self.assertRaises(asyncio.CancelledError):
            await self.adapter.run()
        committed = self.adapter._event_queue.get_nowait()
        self.assertEqual(committed.message_obj.sender.user_id, "wxid_person")
        self.assertEqual(committed.message_obj.sender.nickname, "小明(wxid_person)")
        self.assertEqual(committed.message_obj.wechat_sender_name, "小明")

    async def test_member_name_cache_refresh_and_fallback(self):
        self.adapter.request = AsyncMock(
            side_effect=[
                {"state": "absent", "member": None},
                {"id": "refresh", "state": "queued"},
                {"state": "absent", "member": None},
            ]
        )
        self.assertEqual(
            await self.adapter.member_name("123@chatroom", "wxid_new"), "wxid_new"
        )
        calls = self.adapter.request.await_args_list
        self.assertEqual(calls[1].args[0], "POST")
        self.assertTrue(calls[1].args[1].endswith("/members/refresh"))
        # Cached fallback: no further bridge calls within its short TTL.
        self.assertEqual(
            await self.adapter.member_name("123@chatroom", "wxid_new"), "wxid_new"
        )
        self.assertEqual(len(self.adapter.request.await_args_list), 2)
        self.adapter.member_names.clear()
        await self.adapter.member_name("123@chatroom", "wxid_new")
        # No second refresh within the refresh interval.
        self.assertEqual(
            [x.args[0] for x in self.adapter.request.await_args_list],
            ["GET", "POST", "GET"],
        )
        self.adapter.request = AsyncMock(
            side_effect=RuntimeError("WeChat bridge HTTP 500")
        )
        self.adapter.member_names.clear()
        self.assertEqual(
            await self.adapter.member_name("123@chatroom", "wxid_x"), "wxid_x"
        )
        self.adapter.resolve_member_names = False
        self.assertEqual(
            await self.adapter.member_name("1@chatroom", "wxid_y"), "wxid_y"
        )


class OutgoingMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {
            "id": "wechat-test",
            "type": "native_wechat",
            "enable": True,
            "server_url": "http://127.0.0.1:6174",
            "account_id": "account-test",
            "allowed_groups": ["123@chatroom"],
            "state_file": str(Path(self.temp.name) / "state.json"),
            "token_file": str(Path(self.temp.name) / "token"),
            "chat_provider_id": "test-model",
            "mention_sender": True,
        }
        self.adapter = NativeWeChatAdapter(self.config, {}, asyncio.Queue())
        self.adapter.own_id = "wxid_bot"
        self.native = {
            "id": "message1",
            "accountId": "account-test",
            "threadId": "123@chatroom",
            "senderId": "wxid_person",
            "type": 1,
            "direction": "incoming",
            "body": "@bot 来个表情",
            "mentions": {"state": "known", "self": True, "all": False},
            "createdAt": datetime.now(UTC).isoformat(),
        }
        self.source = self.adapter.convert(self.native, "live")
        self.source.sender.nickname = "小明😀"
        self.png = Path(self.temp.name) / "meme.png"
        self.png.write_bytes(PNG_1X1)
        self.accepted = {
            "id": "send1",
            "state": "server_accepted",
            "accountId": "account-test",
            "destination": "123@chatroom",
        }
        self.job_done = {
            "id": "job1",
            "state": "completed",
            "accountId": "account-test",
            "threadId": "123@chatroom",
        }

    def test_segments_and_markdown_cleanup(self):
        image = Image.fromFileSystem(str(self.png))
        segments = outgoing_segments([Plain("a"), Plain("b"), image, Plain(" ")])
        self.assertEqual(
            segments, [("text", [("plain", "a"), ("plain", "b")]), ("image", image)]
        )
        self.assertEqual(outgoing_segments([image], images=False), [])
        self.assertEqual(
            clean_markdown("## 标题\n**重点** 和 __强调__\n```py\ncode\n```"),
            "标题\n重点 和 强调\ncode",
        )

    async def test_text_reply_mentions_asker_with_utf16_positions(self):
        self.adapter.request = AsyncMock(side_effect=[self.native, self.accepted])
        await self.adapter.reply(
            self.source, "**你好**", "key", self.adapter.mention_for(self.source)
        )
        body = self.adapter.request.await_args_list[1].kwargs["json"]
        prefix = "@小明😀\u2005"
        self.assertEqual(body["kind"], "text")
        self.assertEqual(body["text"], prefix + "你好")
        self.assertEqual(body["mentions"]["userIds"], ["wxid_person"])
        # The emoji is two UTF-16 code units, as the bridge counts positions.
        self.assertEqual(
            body["mentions"]["positions"], [{"start": 0, "end": 6, "index": 0}]
        )

    def test_unsafe_or_disabled_mentions_are_skipped(self):
        self.source.sender.nickname = "bad\u2005name"
        self.assertIsNone(self.adapter.mention_for(self.source))
        self.source.sender.nickname = "ok"
        self.adapter.mention_sender = False
        self.assertIsNone(self.adapter.mention_for(self.source))

    def test_mention_label_uses_plain_nickname_not_id_form(self):
        self.source.sender.nickname = "-(wxid_person)"
        self.source.wechat_sender_name = "-"
        self.assertEqual(
            self.adapter.mention_for(self.source),
            {"userId": "wxid_person", "label": "-"},
        )

    async def test_duplicate_nicknames_are_told_apart_by_id(self):
        # Two members share the nickname "-"; the message mentions the other one.
        native = self.native | {
            "body": "@martyr\u2005@-\u2005\u8fd9\u4e2a\u662f\u732b\u5a18",
            "mentions": {
                "state": "known",
                "self": True,
                "userIds": ["wxid_bot", "wxid_other"],
            },
        }
        message = self.adapter.convert(native, "live")
        names = {"wxid_person": "-", "wxid_other": "-"}
        self.adapter.member_name = AsyncMock(side_effect=lambda g, u: names[u])
        await self.adapter.describe_people(message, native)
        self.assertEqual(message.sender.nickname, "-(wxid_person)")
        self.assertEqual(message.wechat_sender_name, "-")
        self.assertTrue(
            message.message_str.endswith(
                "\uff08\u8fd9\u6761\u6d88\u606f@\u4e86\uff1a-(wxid_other)\uff09"
            )
        )
        plain = [p for p in message.message if isinstance(p, Plain)][0]
        self.assertTrue(plain.text.endswith("-(wxid_other)\uff09"))
        self.adapter.show_member_ids = False
        message = self.adapter.convert(native, "live")
        await self.adapter.describe_people(message, native)
        self.assertEqual(message.sender.nickname, "-")
        self.assertNotIn("\u8fd9\u6761\u6d88\u606f@\u4e86", message.message_str)

    def test_typed_bot_nickname_counts_as_mention_when_enabled(self):
        typed = self.native | {
            "body": "@martyr\u2005\u90a3\u4f60\u77e5\u4e0d\u77e5\u9053\u662f\u8c01",
            "mentions": {"state": "known", "self": False, "userIds": []},
        }
        self.assertIsNone(self.adapter.convert(typed, "live"))
        self.adapter.text_mentions = True
        self.assertIsNone(self.adapter.convert(typed, "live"))  # own name unknown
        self.adapter.member_names[("123@chatroom", "wxid_bot")] = (
            "martyr",
            time.monotonic() + 60,
        )
        message = self.adapter.convert(typed, "live")
        self.assertIsInstance(message.message[0], At)
        self.assertIsNone(
            self.adapter.convert(
                typed | {"body": "@martyrs \u4e0d\u662f\u6211"}, "live"
            )
        )
        self.assertIsNotNone(
            self.adapter.convert(typed | {"body": "hi @martyr"}, "live")
        )

    async def test_image_upload_send_and_job_wait(self):
        self.adapter.request = AsyncMock(
            side_effect=[
                {"id": "upload_1", "mediaType": "image/png"},
                {"id": "job1", "state": "queued", "accountId": "account-test"},
                self.job_done,
            ]
        )
        with patch("main.asyncio.sleep", AsyncMock()):
            await self.adapter.send_image(
                "123@chatroom", Image.fromFileSystem(str(self.png)), "img-key"
            )
        upload, send, poll = self.adapter.request.await_args_list
        self.assertEqual(upload.args[:2], ("POST", "/uploads"))
        self.assertEqual(send.kwargs["json"]["kind"], "image")
        self.assertEqual(send.kwargs["json"]["sourceId"], "upload_1")
        self.assertEqual(send.kwargs["headers"]["Idempotency-Key"], "img-key")
        self.assertEqual(poll.args, ("GET", "/jobs/job1"))

    async def test_image_failures_are_not_reported_as_sent(self):
        self.adapter.request = AsyncMock(
            side_effect=[{"id": "upload_1"}, self.job_done | {"state": "failed"}]
        )
        with self.assertRaises(RuntimeError):
            await self.adapter.send_image(
                "123@chatroom", Image.fromFileSystem(str(self.png)), "k"
            )
        bad = Path(self.temp.name) / "bad.png"
        bad.write_bytes(b"not an image")
        self.adapter.request = AsyncMock()
        with self.assertRaises(ValueError):
            await self.adapter.send_image(
                "123@chatroom", Image.fromFileSystem(str(bad)), "k"
            )
        with self.assertRaises(ValueError):
            await self.adapter.send_image(
                "other@chatroom", Image.fromFileSystem(str(self.png)), "k"
            )
        self.adapter.request.assert_not_awaited()

    async def test_event_send_checks_once_then_sends_text_and_image(self):
        event = NativeWeChatEvent(self.source, self.adapter)
        self.adapter.ensure_reply_allowed = AsyncMock()
        self.adapter.send_text = AsyncMock()
        self.adapter.send_image = AsyncMock()
        image = Image.fromFileSystem(str(self.png))
        await event.send(MessageChain([Plain("给你"), image]))
        self.adapter.ensure_reply_allowed.assert_awaited_once()
        text_call = self.adapter.send_text.await_args
        self.assertEqual(text_call.args[1], [("plain", "给你")])
        self.assertEqual(text_call.args[3]["userId"], "wxid_person")
        self.adapter.send_image.assert_awaited_once()
        self.assertIs(self.adapter.send_image.await_args.args[1], image)
        await event.send(MessageChain([Plain("第二条")]))
        self.assertIsNone(self.adapter.send_text.await_args.args[3])

    async def test_follow_up_only_shortly_after_a_mention(self):
        session = SimpleNamespace(
            session_id="123@chatroom", message_type=MessageType.GROUP_MESSAGE
        )
        chain = MessageChain([Image.fromFileSystem(str(self.png))])
        self.adapter.ensure_reply_allowed = AsyncMock()
        self.adapter.send_image = AsyncMock()
        with self.assertRaises(ValueError):
            await self.adapter.send_by_session(session, chain)
        self.adapter.recent_mentions["123@chatroom"] = (time.monotonic(), self.source)
        await self.adapter.send_by_session(session, chain)
        self.adapter.ensure_reply_allowed.assert_awaited_once_with(self.source)
        self.adapter.send_image.assert_awaited_once()
        self.adapter.recent_mentions["123@chatroom"] = (
            time.monotonic() - 500,
            self.source,
        )
        with self.assertRaises(ValueError):
            await self.adapter.send_by_session(session, chain)
        other = SimpleNamespace(
            session_id="other@chatroom", message_type=MessageType.GROUP_MESSAGE
        )
        self.adapter.recent_mentions["other@chatroom"] = (time.monotonic(), self.source)
        with self.assertRaises(ValueError):
            await self.adapter.send_by_session(other, chain)
        self.assertEqual(self.adapter.send_image.await_count, 1)

    async def test_style_hint_only_for_wechat_requests(self):
        plugin = NativeWeChatPlugin(SimpleNamespace(), {})
        req = SimpleNamespace(system_prompt="人设")
        wechat = SimpleNamespace(get_platform_name=lambda: "native_wechat")
        await plugin.add_wechat_style_hint(wechat, req)
        self.assertEqual(req.system_prompt, "人设\n\n" + DEFAULT_STYLE_HINT)
        other = SimpleNamespace(system_prompt="人设")
        await plugin.add_wechat_style_hint(
            SimpleNamespace(get_platform_name=lambda: "aiocqhttp"), other
        )
        self.assertEqual(other.system_prompt, "人设")
        plugin.config["reply_style_hint"] = " "
        empty = SimpleNamespace(system_prompt="人设")
        await plugin.add_wechat_style_hint(wechat, empty)
        self.assertEqual(empty.system_prompt, "人设")


class IncomingMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {
            "id": "wechat-test",
            "type": "native_wechat",
            "enable": True,
            "server_url": "http://127.0.0.1:6174",
            "account_id": "account-test",
            "allowed_groups": ["123@chatroom"],
            "state_file": str(Path(self.temp.name) / "state.json"),
            "token_file": str(Path(self.temp.name) / "token"),
            "chat_provider_id": "test-model",
            "context_messages": True,
        }
        self.adapter = NativeWeChatAdapter(self.config, {}, asyncio.Queue())
        self.adapter.own_id = "wxid_bot"
        base = {
            "accountId": "account-test",
            "threadId": "123@chatroom",
            "senderId": "wxid_person",
            "direction": "incoming",
            "body": None,
            "mentions": {"state": "known", "self": False, "userIds": []},
            "createdAt": datetime.now(UTC).isoformat(),
        }
        self.image = base | {"id": "img1", "type": 3, "serverId": "999"}
        self.voice = base | {"id": "voice1", "type": 34}
        self.sticker = base | {"id": "st1", "type": 47}
        self.ask = base | {
            "id": "ask1",
            "type": 1,
            "body": "@martyr 这是什么",
            "mentions": {"state": "known", "self": True, "userIds": ["wxid_bot"]},
        }
        self.quote = base | {
            "id": "q1",
            "type": 49,
            "appMessageType": 57,
            "body": "@martyr 这张图里是什么",
            "mentions": {"state": "known", "self": True, "userIds": ["wxid_bot"]},
            "quote": {
                "type": 3,
                "serverId": "999",
                "senderId": "wxid_other",
                "embeddedText": None,
            },
        }

    def test_media_is_admitted_only_as_context(self):
        for native, kind, text in (
            (self.image, "image", "[图片]"),
            (self.voice, "voice", "[语音] "),
            (self.sticker, "sticker", "[动画表情]"),
        ):
            with self.subTest(kind=kind):
                message = self.adapter.convert(native, "live", require_mention=False)
                self.assertEqual(message.wechat_kind, kind)
                self.assertEqual(message.message_str, text)
                self.assertFalse(any(isinstance(c, At) for c in message.message))
                self.assertIsNone(self.adapter.convert(native, "live"))
        video = self.image | {"type": 43}
        self.assertIsNone(self.adapter.convert(video, "live", require_mention=False))
        self.adapter.receive_media = False
        self.assertIsNone(self.adapter.convert(self.image, "live", False))

    def test_quote_reply_is_text_that_can_mention(self):
        message = self.adapter.convert(self.quote, "live")
        self.assertEqual(message.wechat_kind, "text")
        self.assertIsInstance(message.message[0], At)
        plain = self.quote | {
            "body": "哈哈",
            "mentions": {"state": "known", "self": False},
        }
        self.assertIsNone(self.adapter.convert(plain, "live"))
        self.assertIsNotNone(self.adapter.convert(plain, "live", False))
        other_app = self.quote | {"appMessageType": 5}
        self.assertIsNone(self.adapter.convert(other_app, "live", False))

    async def test_quote_note_names_quoted_author_and_content(self):
        names = {"wxid_person": "小明", "wxid_other": "小红"}
        self.adapter.member_name = AsyncMock(side_effect=lambda g, u: names[u])
        message = self.adapter.convert(self.quote, "live")
        await self.adapter.describe_people(message, self.quote)
        self.assertTrue(
            message.message_str.endswith("（引用了 小红(wxid_other) 的消息：[图片]）")
        )
        text_quote = self.quote | {
            "quote": {"type": 1, "senderId": "wxid_bot", "embeddedText": "原话"}
        }
        message = self.adapter.convert(text_quote, "live")
        await self.adapter.describe_people(message, text_quote)
        self.assertIn("（引用了 你（机器人） 的消息：原话）", message.message_str)

    async def test_download_waits_for_preferred_variant(self):
        self.adapter.request = AsyncMock(
            side_effect=[
                {
                    "items": [
                        {
                            "id": "a1",
                            "kind": "image",
                            "variant": "standard",
                            "state": "pending",
                        },
                        {
                            "id": "a2",
                            "kind": "image",
                            "variant": "thumbnail",
                            "state": "available",
                        },
                    ]
                },
                {
                    "items": [
                        {
                            "id": "a1",
                            "kind": "image",
                            "variant": "standard",
                            "state": "available",
                        }
                    ]
                },
            ]
        )
        self.adapter.request_bytes = AsyncMock(return_value=PNG_1X1)
        with patch("main.asyncio.sleep", AsyncMock()):
            path = await self.adapter.download_media("img1", "image")
        self.assertEqual(path.read_bytes(), PNG_1X1)
        self.assertEqual(path.suffix, ".png")
        self.assertEqual(path.parent, Path(self.temp.name) / "media")
        self.assertIn(
            "/media/a1/content", self.adapter.request_bytes.await_args.args[0]
        )

    async def test_request_bytes_reads_every_chunk(self):
        chunks = [b"a" * 70000, b"b" * 70000, b"c" * 5]

        class Content:
            async def iter_chunked(self, size):
                for chunk in chunks:
                    yield chunk

        class Response:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        self.adapter.http = SimpleNamespace(get=lambda url: Response())
        data = await self.adapter.request_bytes("/media/a/content", 1 << 20)
        self.assertEqual(data, b"".join(chunks))
        with self.assertRaises(ValueError):
            await self.adapter.request_bytes("/media/a/content", 100000)

    async def test_incomplete_or_corrupt_media_is_rejected(self):
        asset = {"id": "a1", "byteLength": len(PNG_1X1) + 10}
        self.adapter.request_bytes = AsyncMock(return_value=PNG_1X1)
        self.assertIsNone(await self.adapter.save_asset(asset, "image"))
        asset = {"id": "a1", "byteLength": len(PNG_1X1), "sha256": "0" * 64}
        self.assertIsNone(await self.adapter.save_asset(asset, "image"))
        asset["sha256"] = hashlib.sha256(PNG_1X1).hexdigest()
        path = await self.adapter.save_asset(asset, "image")
        self.assertEqual(path.read_bytes(), PNG_1X1)

    async def test_download_falls_back_to_thumbnail_after_wait(self):
        page = {
            "items": [
                {
                    "id": "a1",
                    "kind": "image",
                    "variant": "standard",
                    "state": "pending",
                },
                {
                    "id": "a2",
                    "kind": "image",
                    "variant": "thumbnail",
                    "state": "available",
                },
            ]
        }
        self.adapter.request = AsyncMock(return_value=page)
        self.adapter.request_bytes = AsyncMock(return_value=PNG_1X1)
        path = await self.adapter.download_media("img1", "image", wait=0)
        self.assertIsNotNone(path)
        self.assertIn(
            "/media/a2/content", self.adapter.request_bytes.await_args.args[0]
        )

    async def test_download_gives_up_cleanly(self):
        self.adapter.request = AsyncMock(
            return_value={
                "items": [
                    {
                        "id": "a",
                        "kind": "image",
                        "variant": "standard",
                        "state": "failed",
                    }
                ]
            }
        )
        self.assertIsNone(await self.adapter.download_media("img1", "image"))
        self.adapter.request = AsyncMock(
            side_effect=RuntimeError("WeChat bridge HTTP 409")
        )
        self.assertIsNone(await self.adapter.download_media("st1", "image"))
        self.adapter.request = AsyncMock(
            return_value={
                "items": [
                    {
                        "id": "v",
                        "kind": "voice",
                        "variant": "normal",
                        "state": "available",
                    }
                ]
            }
        )
        self.adapter.request_bytes = AsyncMock(return_value=b"not wav")
        self.assertIsNone(await self.adapter.download_media("voice1", "voice"))

    async def test_attach_media_builds_image_record_or_placeholder(self):
        png = Path(self.temp.name) / "x.png"
        png.write_bytes(PNG_1X1)
        self.adapter.download_media = AsyncMock(return_value=png)
        image = self.adapter.convert(self.image, "live", False)
        await self.adapter.attach_media(image)
        self.assertEqual(len(image.message), 1)
        self.assertIsInstance(image.message[0], Image)
        voice = self.adapter.convert(self.voice, "live", False)
        await self.adapter.attach_media(voice)
        self.assertIsInstance(voice.message[0], Plain)
        self.assertIsInstance(voice.message[1], Record)
        self.adapter.download_media = AsyncMock(return_value=None)
        failed = self.adapter.convert(self.image, "live", False)
        await self.adapter.attach_media(failed)
        self.assertEqual(failed.message[0].text, "[图片]（未能获取）")

    def test_recent_image_is_attached_once_within_window(self):
        self.adapter.remember_image(self.image)
        ask = self.adapter.convert(self.ask, "live")
        self.assertEqual(self.adapter.referenced_image(ask), "img1")
        self.assertIsNone(self.adapter.referenced_image(ask))
        self.adapter.remember_image(self.image)
        other = self.adapter.convert(self.ask | {"senderId": "wxid_x"}, "live")
        self.assertIsNone(self.adapter.referenced_image(other))
        key = ("123@chatroom", "wxid_person")
        self.adapter.recent_images[key] = (time.monotonic() - 500, "img1")
        self.assertIsNone(self.adapter.referenced_image(ask))
        context = self.adapter.convert(
            self.ask | {"mentions": {"state": "known", "self": False}}, "live", False
        )
        self.adapter.remember_image(self.image)
        self.assertIsNone(self.adapter.referenced_image(context))

    def test_quoted_image_is_found_by_server_id(self):
        self.adapter.remember_image(self.image | {"senderId": "wxid_other"})
        quote = self.adapter.convert(self.quote, "live")
        self.assertEqual(self.adapter.referenced_image(quote), "img1")
        unknown = self.adapter.convert(
            self.quote | {"quote": self.quote["quote"] | {"serverId": "1"}}, "live"
        )
        self.assertIsNone(self.adapter.referenced_image(unknown))

    async def test_finish_and_commit_appends_image_and_always_dispatches(self):
        png = Path(self.temp.name) / "x.png"
        png.write_bytes(PNG_1X1)
        self.adapter.download_media = AsyncMock(return_value=png)
        ask = self.adapter.convert(self.ask, "live")
        await self.adapter.finish_and_commit(ask, "img1")
        event = self.adapter._event_queue.get_nowait()
        self.assertIsInstance(event.message_obj.message[0], At)
        self.assertIsInstance(event.message_obj.message[-1], Image)
        self.assertIn("123@chatroom", self.adapter.recent_mentions)
        self.adapter.download_media = AsyncMock(side_effect=OSError("disk"))
        ask = self.adapter.convert(self.ask | {"id": "ask2"}, "live")
        await self.adapter.finish_and_commit(ask, "img1")
        self.assertEqual(self.adapter._event_queue.qsize(), 1)


class ProactiveAndMentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {
            "id": "wechat-test",
            "type": "native_wechat",
            "enable": True,
            "server_url": "http://127.0.0.1:6174",
            "account_id": "account-test",
            "allowed_groups": ["123@chatroom"],
            "state_file": str(Path(self.temp.name) / "state.json"),
            "token_file": str(Path(self.temp.name) / "token"),
            "chat_provider_id": "test-model",
            "mention_sender": True,
            "proactive_messages": True,
        }
        self.adapter = NativeWeChatAdapter(self.config, {}, asyncio.Queue())
        self.adapter.own_id = "wxid_bot"
        names = {"wxid_a": "小明", "wxid_b": "小红", "astrbot": "astrbot"}
        self.adapter.member_name = AsyncMock(side_effect=lambda g, u: names.get(u, u))
        self.session = SimpleNamespace(
            session_id="123@chatroom", message_type=MessageType.GROUP_MESSAGE
        )

    def cron(self, payload):
        from astrbot.core.platform.astr_message_event import current_message_event

        event = SimpleNamespace(
            get_platform_name=lambda: "cron",
            get_extra=lambda key, default=None: (
                payload if key == "cron_payload" else default
            ),
        )
        token = current_message_event.set(event)
        self.addCleanup(current_message_event.reset, token)

    def test_at_components_become_tokens(self):
        segments = outgoing_segments(
            [Plain("提醒 "), At(qq="wxid_b"), Plain(" 喝水"), At(qq="all")]
        )
        self.assertEqual(
            segments,
            [("text", [("plain", "提醒 "), ("at", "wxid_b"), ("plain", " 喝水")])],
        )
        self.assertEqual(
            outgoing_segments([At(qq="wxid_b")]), [("text", [("at", "wxid_b")])]
        )

    async def test_compose_text_positions_and_fallbacks(self):
        body = await self.adapter.compose_text(
            "123@chatroom",
            [
                ("plain", "  **早上好** "),
                ("at", "wxid_a"),
                ("plain", "和"),
                ("at", "wxid_b"),
                ("at", "wxid_a"),
                ("plain", "  "),
            ],
        )
        sep = "\u2005"
        self.assertEqual(body["text"], f"早上好 @小明{sep}和@小红{sep}@小明{sep}")
        self.assertEqual(body["mentions"]["userIds"], ["wxid_a", "wxid_b"])
        self.assertEqual(
            body["mentions"]["positions"],
            [
                {"start": 4, "end": 8, "index": 0},
                {"start": 9, "end": 13, "index": 1},
                {"start": 13, "end": 17, "index": 0},
            ],
        )
        own = await self.adapter.compose_text(
            "123@chatroom", [("at", "wxid_bot"), ("plain", "hi")]
        )
        self.assertEqual(own, {"threadId": "123@chatroom", "text": "@wxid_bot hi"})
        self.assertIsNone(
            await self.adapter.compose_text("123@chatroom", [("plain", " ")])
        )

    async def test_open_proactive_send_without_recent_mention(self):
        self.adapter.ensure_reply_allowed = AsyncMock()
        self.adapter.send_text = AsyncMock()
        await self.adapter.send_by_session(self.session, MessageChain([Plain("公告")]))
        self.adapter.send_text.assert_awaited_once()
        self.assertIsNone(self.adapter.send_text.await_args.args[3])
        self.adapter.ensure_reply_allowed.assert_not_awaited()
        other = SimpleNamespace(
            session_id="other@chatroom", message_type=MessageType.GROUP_MESSAGE
        )
        with self.assertRaises(ValueError):
            await self.adapter.send_by_session(other, MessageChain([Plain("x")]))
        private = SimpleNamespace(
            session_id="123@chatroom", message_type=MessageType.FRIEND_MESSAGE
        )
        with self.assertRaises(ValueError):
            await self.adapter.send_by_session(private, MessageChain([Plain("x")]))
        self.assertTrue(self.adapter.meta().support_proactive_message)

    async def test_scheduled_reminder_mentions_creator(self):
        self.cron(
            {"session": "wechat-test:GroupMessage:123@chatroom", "sender_id": "wxid_a"}
        )
        self.adapter.send_text = AsyncMock()
        self.adapter.send_image = AsyncMock()
        await self.adapter.send_by_session(
            self.session, MessageChain([Plain("该喝水了"), Plain("！")])
        )
        call = self.adapter.send_text.await_args
        self.assertEqual(call.args[3], {"userId": "wxid_a", "label": "小明"})

    async def test_scheduled_reminder_respects_explicit_or_unknown_targets(self):
        self.cron(
            {"session": "wechat-test:GroupMessage:123@chatroom", "sender_id": "wxid_a"}
        )
        self.adapter.send_text = AsyncMock()
        await self.adapter.send_by_session(
            self.session, MessageChain([At(qq="wxid_b"), Plain("开会")])
        )
        self.assertIsNone(self.adapter.send_text.await_args.args[3])

    async def test_webui_or_other_session_jobs_do_not_mention(self):
        self.adapter.send_text = AsyncMock()
        self.cron(
            {"session": "wechat-test:GroupMessage:123@chatroom", "sender_id": "astrbot"}
        )
        await self.adapter.send_by_session(self.session, MessageChain([Plain("日报")]))
        self.assertIsNone(self.adapter.send_text.await_args.args[3])
        self.assertIsNone(self.adapter.cron_payload("other@chatroom"))

    async def test_closed_proactive_keeps_follow_up_window(self):
        self.adapter.proactive_messages = False
        self.assertFalse(self.adapter.meta().support_proactive_message)
        with self.assertRaises(ValueError):
            await self.adapter.send_by_session(self.session, MessageChain([Plain("x")]))


class ProfileAndAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.origin = "wechat:GroupMessage:1@chatroom"
        self.platform = {"id": "wechat", "context_messages": True}
        self.plugin = NativeWeChatPlugin(
            SimpleNamespace(send_message=AsyncMock()),
            {"alert_sessions": ["qq:FriendMessage:1", " "], "alert_after_seconds": 60},
        )

    def test_profile_defaults_whitelist_and_proactive_reply(self):
        profile = Profile(
            {
                "platform_settings": {
                    "enable_id_white_list": True,
                    "id_whitelist": ["qq:FriendMessage:1"],
                },
                "provider_ltm_settings": {"active_reply": {"enable": True}},
            }
        )
        self.assertTrue(
            self.plugin.apply_profile_defaults(self.origin, profile, self.platform)
        )
        self.assertEqual(
            profile["platform_settings"]["id_whitelist"],
            ["qq:FriendMessage:1", self.origin],
        )
        self.assertFalse(profile["provider_ltm_settings"]["active_reply"]["enable"])
        profile.save_config.assert_called_once()
        self.assertFalse(
            self.plugin.apply_profile_defaults(self.origin, profile, self.platform)
        )
        profile.save_config.assert_called_once()

    def test_profile_defaults_leave_disabled_whitelist_and_mention_only_mode(self):
        profile = Profile(
            {
                "platform_settings": {"enable_id_white_list": False},
                "provider_ltm_settings": {"active_reply": {"enable": True}},
            }
        )
        self.assertFalse(
            self.plugin.apply_profile_defaults(
                self.origin, profile, {"id": "wechat", "context_messages": False}
            )
        )
        self.assertNotIn("id_whitelist", profile["platform_settings"])
        self.assertTrue(profile["provider_ltm_settings"]["active_reply"]["enable"])

    async def test_offline_alert_after_delay_then_recovery(self):
        statuses = iter(
            [(False, "账号已退出登录")] * 3 + [(True, "在线"), (True, "在线")]
        )
        self.plugin.account_status = AsyncMock(side_effect=lambda _: next(statuses))
        state = {}
        send = self.plugin.context.send_message
        await self.plugin.check_accounts([self.platform], state, 0)
        await self.plugin.check_accounts([self.platform], state, 30)
        send.assert_not_awaited()
        await self.plugin.check_accounts([self.platform], state, 61)
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[0], "qq:FriendMessage:1")
        self.assertIn("离线", send.await_args.args[1].chain[0].text)
        await self.plugin.check_accounts([self.platform], state, 90)
        self.assertEqual(send.await_count, 2)
        self.assertIn("恢复", send.await_args.args[1].chain[0].text)
        await self.plugin.check_accounts([self.platform], state, 120)
        self.assertEqual(send.await_count, 2)

    async def test_brief_disconnect_does_not_alert(self):
        statuses = iter([(False, "连接状态 connecting"), (True, "在线")])
        self.plugin.account_status = AsyncMock(side_effect=lambda _: next(statuses))
        state = {}
        await self.plugin.check_accounts([self.platform], state, 0)
        await self.plugin.check_accounts([self.platform], state, 30)
        self.plugin.context.send_message.assert_not_awaited()

    async def test_failed_alert_session_does_not_block_others(self):
        self.plugin.config["alert_sessions"] = ["bad", "qq:FriendMessage:1"]
        send = self.plugin.context.send_message
        send.side_effect = [ValueError("bad session"), True]
        await self.plugin.alert("test")
        self.assertEqual(send.await_count, 2)

    async def test_account_status_rejects_nonlocal_bridge(self):
        online, detail = await self.plugin.account_status(
            {"server_url": "https://example.com"}
        )
        self.assertFalse(online)


class PersonaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.profile = Profile(
            {"agent_runner": {"config": {"persona": {"persona_id": "old"}}}}
        )
        self.conversation = SimpleNamespace(persona_id=None)
        self.cm = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="old-conversation"),
            get_conversation=AsyncMock(return_value=self.conversation),
            new_conversation=AsyncMock(),
            update_conversation=AsyncMock(),
        )
        self.pm = SimpleNamespace(get_persona=AsyncMock(return_value=object()))
        self.plugin = NativeWeChatPlugin(
            SimpleNamespace(
                conversation_manager=self.cm,
                persona_manager=self.pm,
            )
        )
        self.get = AsyncMock(return_value={})
        self.put = AsyncMock()
        self.patches = [
            patch("main.sp.get_async", self.get),
            patch("main.sp.put_async", self.put),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.rule = {
            "persona_id": "new",
            "group_name": "Test group",
            "new_conversation_on_change": True,
        }

    async def test_switch_preserves_history_and_only_changes_target(self):
        await self.plugin.apply_group_persona(
            "wechat:GroupMessage:1@chatroom", self.profile, self.rule
        )
        self.cm.new_conversation.assert_awaited_once_with(
            "wechat:GroupMessage:1@chatroom",
            title="Test group · new",
            persona_id="new",
        )
        self.cm.update_conversation.assert_not_awaited()
        self.profile.save_config.assert_called_once()
        self.assertEqual(
            self.profile["agent_runner"]["config"]["persona"]["persona_id"], "new"
        )

    async def test_repeated_save_does_not_start_another_conversation(self):
        self.conversation.persona_id = "new"
        await self.plugin.apply_group_persona("group", self.profile, self.rule)
        await self.plugin.apply_group_persona("group", self.profile, self.rule)
        self.cm.new_conversation.assert_not_awaited()

    async def test_keep_history_updates_existing_conversation(self):
        self.rule["new_conversation_on_change"] = False
        await self.plugin.apply_group_persona("group", self.profile, self.rule)
        self.cm.new_conversation.assert_not_awaited()
        self.cm.update_conversation.assert_awaited_once_with(
            "group", conversation_id="old-conversation", persona_id="new"
        )

    async def test_invalid_persona_keeps_existing_settings(self):
        self.pm.get_persona.side_effect = ValueError("not found")
        await self.plugin.apply_group_persona("group", self.profile, self.rule)
        self.profile.save_config.assert_not_called()
        self.cm.new_conversation.assert_not_awaited()
        self.get.assert_not_awaited()

    async def test_session_override_is_changed_without_losing_other_options(self):
        self.get.return_value = {"persona_id": "override", "provider_id": "model"}
        await self.plugin.apply_group_persona("group", self.profile, self.rule)
        self.put.assert_awaited_once_with(
            "umo",
            "group",
            "session_service_config",
            {"persona_id": "new", "provider_id": "model"},
        )

    async def test_different_groups_use_different_personas(self):
        other = copy.deepcopy(self.profile)
        await self.plugin.apply_group_persona("group-a", self.profile, self.rule)
        await self.plugin.apply_group_persona(
            "group-b", other, self.rule | {"persona_id": "another"}
        )
        self.assertEqual(
            self.profile["agent_runner"]["config"]["persona"]["persona_id"], "new"
        )
        self.assertEqual(
            other["agent_runner"]["config"]["persona"]["persona_id"], "another"
        )

    async def test_duplicate_group_rows_are_rejected_before_changes(self):
        rule = {"platform_id": "wechat", "group_id": "1@chatroom"}
        plugin = NativeWeChatPlugin(
            SimpleNamespace(astrbot_config_mgr=object()), {"groups": [rule, rule]}
        )
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            await plugin.initialize()

    async def test_group_name_refresh_and_outside_whitelist_ignored(self):
        profile = Profile(
            {
                "agent_runner": {
                    "runner_type": "local",
                    "config": {"persona": {"persona_id": "old"}},
                }
            }
        )
        origin = "wechat:GroupMessage:1@chatroom"
        manager = SimpleNamespace(
            default_conf={
                "platform": [
                    {
                        "id": "wechat",
                        "type": "native_wechat",
                        "enable": True,
                        "allowed_groups": ["1@chatroom"],
                    }
                ]
            },
            get_conf_info=lambda origin: {"id": "profile", "name": "old title"},
            ucr=SimpleNamespace(umop_to_conf_id={origin: "profile"}),
            confs={"profile": profile},
            update_conf_info=AsyncMock(),
        )
        config = Profile(
            {
                "groups": [
                    dict(self.rule, platform_id="wechat", group_id="1@chatroom"),
                    dict(self.rule, platform_id="wechat", group_id="outside@chatroom"),
                ]
            }
        )
        plugin = NativeWeChatPlugin(SimpleNamespace(astrbot_config_mgr=manager), config)
        plugin.fetch_group_name = AsyncMock(return_value="Renamed group")
        plugin.apply_group_persona = AsyncMock()
        await plugin.initialize()
        self.assertEqual(config["groups"][0]["group_name"], "Renamed group")
        plugin.apply_group_persona.assert_awaited_once()
        self.assertEqual(plugin.apply_group_persona.await_args.args[0], origin)
        config.save_config.assert_called_once()
        plugin.fetch_group_name.return_value = None
        await plugin.initialize()
        self.assertEqual(config["groups"][0]["group_name"], "Renamed group")

    async def test_shared_profile_is_cloned_with_exact_route_first(self):
        with tempfile.TemporaryDirectory() as temp:
            old = Profile(
                {
                    "provider_settings": {"wake_prefix": "old"},
                    "agent_runner": {
                        "config": {
                            "model": {"provider_id": "model"},
                            "persona": {"persona_id": "old"},
                        }
                    },
                }
            )
            before = copy.deepcopy(dict(old))
            path = Path(temp) / "isolated.json"
            path.write_text("{}")
            isolated = Profile(copy.deepcopy(dict(old)), path)
            origin = "wechat:GroupMessage:1@chatroom"
            routes = {"wechat::": "shared", origin: "shared", "qq::": "shared"}
            manager = SimpleNamespace(
                default_conf={
                    "platform": [
                        {
                            "id": "wechat",
                            "type": "native_wechat",
                            "enable": True,
                            "allowed_groups": ["1@chatroom"],
                            "chat_provider_id": "model",
                        }
                    ]
                },
                get_conf_info=lambda _: {"id": "shared", "name": "Shared"},
                get_conf=lambda _: old,
                confs={"shared": old, "isolated": isolated},
                create_conf=AsyncMock(return_value="isolated"),
                update_conf_info=AsyncMock(),
                ucr=SimpleNamespace(
                    umop_to_conf_id=routes, update_routing_data=AsyncMock()
                ),
            )
            plugin = NativeWeChatPlugin(SimpleNamespace(astrbot_config_mgr=manager))
            plugin.fetch_group_name = AsyncMock(return_value=None)
            plugin.apply_group_persona = AsyncMock()
            await plugin.initialize()
            updated = manager.ucr.update_routing_data.await_args.args[0]
            self.assertEqual(next(iter(updated)), origin)
            self.assertEqual(updated[origin], "isolated")
            self.assertEqual(updated["qq::"], "shared")
            self.assertEqual(updated["wechat::"], "shared")
            self.assertEqual(dict(old), before)

    async def test_group_name_fetch_rejects_nonlocal_host_before_reading_token(self):
        result = await self.plugin.fetch_group_name(
            {"server_url": "https://example.com"}, "1@chatroom"
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
