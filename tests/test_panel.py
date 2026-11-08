import asyncio
import base64
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import panel
from main import NativeWeChatAdapter, NativeWeChatPlugin
from panel import (
    LOGOUT_CONFIRMATION,
    PLUGIN_NAME,
    ROUTE_PREFIX,
    BridgeError,
    LogBuffer,
    PanelError,
)

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc000000301010018dd8db40000000049454e44ae426082"
)


class FakeRequest:
    def __init__(self, query=None, body=None, username="admin"):
        self.query = dict(query or {})
        self.body = body
        self.username = username

    async def json(self, default=None):
        return self.body if self.body is not None else default


class PanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        panel.RUNTIME.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platform = {
            "id": "wechat-test",
            "type": "native_wechat",
            "enable": True,
            "server_url": "http://127.0.0.1:6174",
            "account_id": "account-test",
            "token_file": str(Path(self.temp.name) / "token"),
            "state_file": str(Path(self.temp.name) / "state.json"),
            "allowed_groups": ["123@chatroom"],
            "friend_whitelist": False,
            "allowed_friends": [],
            "chat_provider_id": "test-model",
        }
        Path(self.platform["token_file"]).write_text("secret-token")
        self.register = Mock()
        self.context = SimpleNamespace(
            send_message=AsyncMock(),
            register_web_api=self.register,
            astrbot_config_mgr=SimpleNamespace(
                default_conf={"platform": [self.platform]}
            ),
        )
        self.plugin = NativeWeChatPlugin(
            self.context, {"alert_sessions": ["qq:FriendMessage:1"]}
        )
        self.routes = {
            call.args[0][len(ROUTE_PREFIX) + 1 :]: call.args[1]
            for call in self.register.call_args_list
        }
        self.bridge = AsyncMock()
        self.plugin.panel.bridge = self.bridge

    def use_request(self, **kwargs):
        patcher = patch.object(panel, "request", FakeRequest(**kwargs))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def call(self, route, **request):
        self.use_request(**request)
        return await self.routes[route]()

    def test_registers_every_route_under_the_plugin_prefix(self):
        self.assertEqual(
            set(self.routes),
            {
                "overview",
                "avatar",
                "login",
                "login/attempt",
                "login/qr",
                "login/cancel",
                "connection",
                "logout",
                "events",
                "logs",
                "threads",
                "contacts",
                "send-test",
            },
        )
        for call in self.register.call_args_list:
            self.assertTrue(call.args[0].startswith(f"/{PLUGIN_NAME}/panel/"))
            self.assertIn(call.args[2], (["GET"], ["POST"]))

    def test_plugin_without_web_api_support_still_loads(self):
        plugin = NativeWeChatPlugin(SimpleNamespace(send_message=AsyncMock()), {})
        self.assertIsNotNone(plugin.panel)

    async def test_overview_combines_bridge_runtime_and_alert_state(self):
        async def fake(platform, method, path, **kwargs):
            if path == "":
                return {
                    "id": "account-test",
                    "alias": "bot",
                    "authenticated": True,
                    "connection": "online",
                    "incomingTransport": "native_push",
                    "ownUsername": "wxid_bot",
                    "secretField": "hidden",
                }
            if path == "/v1/status":
                self.assertFalse(kwargs.get("account", True))
                return {"state": "ready", "version": {"version": "1.5.1"}}
            if path == "/contacts/wxid_bot":
                return {"displayName": "Robot", "remark": None}
            raise AssertionError(path)

        self.bridge.side_effect = fake
        panel.note_received("wechat-test")
        panel.note_sent("wechat-test")
        self.plugin.alert_state["wechat-test"] = {
            "since": time.monotonic() - 50,
            "alerted": False,
            "detail": "连接状态 connecting",
        }
        result = await self.call("overview")
        self.assertEqual(result["status"], "ok")
        data = result["data"]
        self.assertEqual(data["platformId"], "wechat-test")
        self.assertEqual(data["botName"], "Robot")
        self.assertEqual(data["bridge"]["version"], "1.5.1")
        self.assertTrue(data["account"]["authenticated"])
        self.assertNotIn("secretField", data["account"])
        self.assertEqual(data["runtime"]["receivedToday"], 1)
        self.assertEqual(data["runtime"]["sentTotal"], 1)
        self.assertTrue(data["alerts"]["offline"])
        self.assertGreaterEqual(data["alerts"]["offlineSeconds"], 49)
        self.assertEqual(data["alerts"]["sessions"], ["qq:FriendMessage:1"])
        self.assertEqual(data["policy"]["allowedGroups"], ["123@chatroom"])
        self.assertTrue(data["policy"]["privateChats"])

    async def test_overview_reports_unreachable_bridge_without_failing(self):
        self.bridge.side_effect = OSError("refused")
        result = await self.call("overview")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["bridge"]["reachable"])
        self.assertIsNone(result["data"]["account"]["authenticated"])

    async def test_login_sends_the_cli_request_body_with_idempotency_key(self):
        self.bridge.return_value = {
            "id": "op1",
            "kind": "login",
            "state": "starting",
            "result": {},
        }
        result = await self.call("login", body={"strategy": "qr"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["id"], "op1")
        args, kwargs = self.bridge.await_args
        self.assertEqual(args[1:], ("POST", "/auth/login"))
        self.assertEqual(
            kwargs["json"],
            {
                "allowQr": True,
                "qrRefresh": {"maxRefreshes": 3, "mode": "auto"},
                "strategy": "qr",
                "timeoutSeconds": 300,
            },
        )
        self.assertTrue(kwargs["headers"]["Idempotency-Key"])

    async def test_login_rejects_unknown_strategy(self):
        result = await self.call("login", body={"strategy": "password"})
        self.assertEqual(result["status"], "error")
        self.bridge.assert_not_awaited()

    async def test_login_attempt_and_cancel_use_operation_routes(self):
        self.bridge.return_value = {
            "id": "op1",
            "state": "awaiting_scan",
            "result": {"qrAvailable": True, "qrGeneration": 2},
        }
        result = await self.call("login/attempt", query={"id": "op1"})
        self.assertEqual(result["data"]["state"], "awaiting_scan")
        self.assertEqual(self.bridge.await_args.args[1:], ("GET", "/operations/op1"))
        result = await self.call("login/cancel", body={"id": "op1"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.bridge.await_args.args[1:], ("DELETE", "/operations/op1"))
        result = await self.call("login/attempt", query={})
        self.assertEqual(result["status"], "error")

    async def test_qr_is_returned_as_png_data_url(self):
        self.bridge.return_value = (PNG, "image/png")
        result = await self.call("login/qr", query={"id": "op1", "generation": "2"})
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["data"]["available"])
        self.assertEqual(
            result["data"]["dataUrl"],
            "data:image/png;base64," + base64.b64encode(PNG).decode(),
        )
        self.assertEqual(
            self.bridge.await_args.args[2], "/operations/op1/qr?generation=2"
        )
        self.assertTrue(self.bridge.await_args.kwargs["raw"])

    async def test_missing_or_invalid_qr(self):
        self.bridge.side_effect = BridgeError(404, "login_challenge_unavailable")
        result = await self.call("login/qr", query={"id": "op1"})
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["available"])
        self.bridge.side_effect = None
        self.bridge.return_value = (b"<html>", "text/html")
        result = await self.call("login/qr", query={"id": "op1"})
        self.assertEqual(result["status"], "error")

    async def test_connection_actions_are_limited(self):
        self.bridge.return_value = {"connection": "connecting"}
        result = await self.call("connection", body={"action": "connect"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.bridge.await_args.args[1:], ("POST", "/connect"))
        result = await self.call("connection", body={"action": "delete"})
        self.assertEqual(result["status"], "error")

    async def test_logout_requires_typed_confirmation(self):
        result = await self.call("logout", body={})
        self.assertEqual(result["status"], "error")
        self.bridge.assert_not_awaited()
        self.bridge.return_value = {}
        result = await self.call("logout", body={"confirm": LOGOUT_CONFIRMATION})
        self.assertEqual(result["status"], "ok")
        args, kwargs = self.bridge.await_args
        self.assertEqual(args[1:], ("POST", "/logout"))
        self.assertTrue(kwargs["headers"]["Idempotency-Key"])

    async def test_events_show_recent_important_kinds_newest_first(self):
        adapter = SimpleNamespace(cursor="950")
        panel.register_adapter("wechat-test", adapter)
        self.bridge.return_value = {
            "items": [
                {"cursor": "800", "kind": "message.committed", "data": {}},
                {
                    "cursor": "801",
                    "kind": "account.connection",
                    "data": {"state": "disconnected", "serviceCode": -13},
                },
                {
                    "cursor": "802",
                    "kind": "operation.updated",
                    "data": {"kind": "login", "state": "succeeded"},
                },
                {
                    "cursor": "803",
                    "kind": "operation.updated",
                    "data": {"kind": "metadata", "state": "queued"},
                },
                {
                    "cursor": "804",
                    "kind": "account.push",
                    "data": {"command": 24, "state": "notified"},
                },
                {
                    "cursor": "805",
                    "kind": "account.push",
                    "data": {"state": "connected", "transport": "native_longlink"},
                },
                {"cursor": "806", "kind": "account.control_observed", "data": {}},
            ],
            "nextCursor": None,
        }
        result = await self.call("events", query={})
        self.assertIn("after=750", self.bridge.await_args.args[2])
        items = result["data"]["items"]
        self.assertEqual([item["cursor"] for item in items], ["805", "802", "801"])
        self.assertIn("serviceCode=-13", items[2]["summary"])
        self.assertIn("transport=native_longlink", items[0]["summary"])
        result = await self.call("events", query={"all": "1"})
        self.assertEqual(len(result["data"]["items"]), 7)

    async def test_threads_mark_where_the_bot_replies(self):
        self.bridge.return_value = {
            "items": [
                {"id": "123@chatroom", "kind": "group", "displayName": "Team"},
                {"id": "456@chatroom", "kind": "group", "displayName": "Other"},
                {
                    "id": "wxid_friend",
                    "kind": "direct",
                    "lastMessage": {"text": "hi", "senderName": "Friend"},
                },
                {"id": "weixin", "kind": "system"},
            ],
            "nextCursor": "next",
        }
        result = await self.call("threads", query={})
        permitted = {t["id"]: t["permitted"] for t in result["data"]["items"]}
        self.assertEqual(
            permitted,
            {
                "123@chatroom": True,
                "456@chatroom": False,
                "wxid_friend": True,
                "weixin": False,
            },
        )
        self.assertEqual(result["data"]["items"][2]["last"]["sender"], "Friend")
        self.platform["friend_whitelist"] = True
        result = await self.call("threads", query={})
        self.assertFalse(result["data"]["items"][2]["permitted"])

    async def test_send_test_is_limited_to_reply_threads(self):
        adapter = SimpleNamespace(
            thread_allowed=AsyncMock(return_value=False), send_text=AsyncMock()
        )
        result = await self.call(
            "send-test", body={"thread_id": "wxid_x", "text": "hi"}
        )
        self.assertEqual(result["status"], "error")  # adapter not running
        panel.register_adapter("wechat-test", adapter)
        result = await self.call(
            "send-test", body={"thread_id": "wxid_x", "text": "hi"}
        )
        self.assertEqual(result["status"], "error")
        adapter.send_text.assert_not_awaited()
        result = await self.call(
            "send-test", body={"thread_id": "123@chatroom", "text": "x" * 501}
        )
        self.assertEqual(result["status"], "error")
        adapter.thread_allowed.return_value = True
        result = await self.call(
            "send-test", body={"thread_id": "123@chatroom", "text": " test "}
        )
        self.assertEqual(result["status"], "ok")
        args = adapter.send_text.await_args.args
        self.assertEqual(args[:2], ("123@chatroom", "test"))
        adapter.send_text.side_effect = RuntimeError("delivery is unconfirmed")
        result = await self.call(
            "send-test", body={"thread_id": "123@chatroom", "text": "again"}
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("unconfirmed", result["message"])

    async def test_bridge_refuses_non_local_server(self):
        real = panel.WeChatPanel(self.plugin, LogBuffer())
        platform = dict(self.platform, server_url="http://example.com:6174")
        with self.assertRaises(PanelError):
            await real.bridge(platform, "GET", "")

    async def test_unexpected_errors_are_hidden(self):
        self.bridge.side_effect = ZeroDivisionError("boom")
        result = await self.call("threads", query={})
        self.assertEqual(result["status"], "error")
        self.assertNotIn("boom", result["message"])

    async def test_unknown_platform_is_an_error(self):
        result = await self.call("overview", query={"platform": "missing"})
        self.assertEqual(result["status"], "error")


class LogBufferTests(unittest.IsolatedAsyncioTestCase):
    def test_keeps_only_this_plugins_records(self):
        buffer = LogBuffer(capacity=2)
        plugin_logger = logging.getLogger(f"astrbot.plugin.{PLUGIN_NAME}")
        plugin_logger.setLevel(logging.INFO)
        other = logging.getLogger("astrbot.plugin.other_plugin")
        other.setLevel(logging.INFO)
        buffer.attach()
        self.addCleanup(buffer.detach)
        other.propagate = False
        plugin_logger.propagate = False
        plugin_logger.info("one %s", 1)
        other.info("not ours")
        plugin_logger.warning("two")
        plugin_logger.error("three")
        self.assertEqual([r["message"] for r in buffer.records], ["two", "three"])
        self.assertEqual(buffer.since(2), [buffer.records[1]])
        self.assertEqual(buffer.records[1]["level"], "ERROR")
        buffer.detach()
        plugin_logger.info("after detach")
        self.assertEqual(buffer.seq, 3)

    async def test_logs_route_returns_lines_after_a_sequence(self):
        plugin = NativeWeChatPlugin(SimpleNamespace(send_message=AsyncMock()), {})
        plugin.panel_logs.records.extend(
            [{"seq": 1, "message": "a"}, {"seq": 2, "message": "b"}]
        )
        plugin.panel_logs.seq = 2
        with patch.object(panel, "request", FakeRequest(query={"after": "1"})):
            result = await plugin.panel.guard(plugin.panel.plugin_logs)()
        self.assertEqual(result["data"]["items"], [{"seq": 2, "message": "b"}])


class RuntimeHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        panel.RUNTIME.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = {
            "id": "wechat-test",
            "server_url": "http://127.0.0.1:6174",
            "account_id": "account-test",
            "allowed_groups": ["123@chatroom"],
            "state_file": str(Path(self.temp.name) / "state.json"),
            "token_file": str(Path(self.temp.name) / "token"),
        }
        self.adapter = NativeWeChatAdapter(config, {}, asyncio.Queue())

    async def test_send_text_counts_confirmed_sends(self):
        self.adapter.request = AsyncMock(
            return_value={
                "id": "send1",
                "state": "accepted",
                "destination": "123@chatroom",
            }
        )
        await self.adapter.send_text("123@chatroom", "hello", "key")
        snapshot = panel.runtime("wechat-test").snapshot()
        self.assertEqual(snapshot["sentToday"], 1)
        self.assertIsNotNone(snapshot["lastSentAt"])

    async def test_alert_round_records_detail_for_the_panel(self):
        plugin = NativeWeChatPlugin(
            SimpleNamespace(send_message=AsyncMock()),
            {"alert_sessions": ["qq:FriendMessage:1"]},
        )
        plugin.account_status = AsyncMock(return_value=(False, "账号已退出登录"))
        await plugin.check_accounts([{"id": "wechat-test"}], plugin.alert_state, 0)
        entry = plugin.alert_state["wechat-test"]
        self.assertEqual(entry["detail"], "账号已退出登录")
        self.assertEqual(entry["since"], 0)
        self.assertIn("checked_at", entry)


class LogoTests(unittest.TestCase):
    def test_adapter_registers_a_square_png_logo(self):
        from astrbot.core.platform.register import platform_registry

        import main

        metadata = [p for p in platform_registry if p.name == "native_wechat"]
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0].logo_path, "logo.png")
        # AstrBot resolves logo_path next to the adapter's module file.
        logo = Path(main.__file__).with_name("logo.png").read_bytes()
        self.assertTrue(logo.startswith(b"\x89PNG\r\n\x1a\n"))
        width = int.from_bytes(logo[16:20], "big")
        height = int.from_bytes(logo[20:24], "big")
        self.assertEqual(width, height)


if __name__ == "__main__":
    unittest.main()
