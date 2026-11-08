# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 qch7
"""Management panel for the AstrBot WebUI plugin view ``views/panel``.

The view runs in AstrBot's sandboxed iframe and calls these routes through
``AstrBotPluginView.apiGet/apiPost``; AstrBot authenticates every call with
its dashboard login. The bridge token stays on the server: the browser only
sees summarized JSON and the login QR code as a data URL.
"""

import base64
import collections
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

import aiohttp
from astrbot.api import logger

try:  # Real requests inside AstrBot; tests patch this module attribute.
    from astrbot.api.web import request
except ImportError:  # pragma: no cover - older AstrBot without plugin views
    request = None

PLUGIN_NAME = "astrbot_plugin_native_wechat"
LOCAL_BRIDGE = "http://127.0.0.1:6174"
ROUTE_PREFIX = f"/{PLUGIN_NAME}/panel"
LOG_CAPACITY = 500
SEND_TEST_MAX_CHARS = 500
LOGIN_TIMEOUT_SECONDS = 300
QR_MAX_REFRESHES = 3
EVENT_WINDOW = 200
AVATAR_RETRY_SECONDS = 3600
LOGOUT_CONFIRMATION = "退出登录"
# Events shown by default: connection and login changes. Message traffic, push
# notifications, sync bookkeeping and metadata jobs appear only on request.
ALWAYS_IMPORTANT_EVENTS = {
    "account.connection",
    "account.updated",
    "group.refresh_blocked",
}
BRIDGE_ERRORS = {
    "login_challenge_unavailable": "当前没有可用的二维码",
    "login_challenge_superseded": "二维码已更新",
    "not_found": "桥接里找不到该对象",
    "unauthorized": "桥接拒绝了令牌",
    "forbidden": "令牌没有这个权限",
    "conflict": "桥接正在处理其他操作",
}


class PanelError(Exception):
    """An error whose message is safe to show in the panel."""


class BridgeError(PanelError):
    def __init__(self, status, code=None, message=None):
        self.status = status
        self.code = code
        detail = BRIDGE_ERRORS.get(code or "", code or f"HTTP {status}")
        super().__init__(
            f"桥接返回错误：{detail}" + (f"（{message}）" if message else "")
        )


class PlatformRuntime:
    """Live counters one adapter instance reports for the panel."""

    def __init__(self):
        self.adapter = None
        self.started_at = None
        self.received_total = 0
        self.sent_total = 0
        self.day = None
        self.received_today = 0
        self.sent_today = 0
        self.last_received_at = None
        self.last_sent_at = None
        self.last_poll_at = None
        self.last_error = None
        self.last_error_at = None

    def _roll_day(self):
        today = datetime.now().date().isoformat()
        if self.day != today:
            self.day = today
            self.received_today = 0
            self.sent_today = 0

    def snapshot(self):
        self._roll_day()
        return {
            "running": self.adapter is not None,
            "startedAt": self.started_at,
            "receivedTotal": self.received_total,
            "sentTotal": self.sent_total,
            "receivedToday": self.received_today,
            "sentToday": self.sent_today,
            "lastReceivedAt": self.last_received_at,
            "lastSentAt": self.last_sent_at,
            "lastPollAt": self.last_poll_at,
            "lastError": self.last_error,
            "lastErrorAt": self.last_error_at,
        }


RUNTIME = {}


def runtime(platform_id):
    return RUNTIME.setdefault(str(platform_id or ""), PlatformRuntime())


def register_adapter(platform_id, adapter):
    entry = runtime(platform_id)
    entry.adapter = adapter
    entry.started_at = time.time()


def unregister_adapter(platform_id, adapter):
    entry = runtime(platform_id)
    if entry.adapter is adapter:
        entry.adapter = None


def note_poll(platform_id):
    runtime(platform_id).last_poll_at = time.time()


def note_received(platform_id):
    entry = runtime(platform_id)
    entry._roll_day()
    entry.received_total += 1
    entry.received_today += 1
    entry.last_received_at = time.time()


def note_sent(platform_id):
    entry = runtime(platform_id)
    entry._roll_day()
    entry.sent_total += 1
    entry.sent_today += 1
    entry.last_sent_at = time.time()


def note_error(platform_id, text):
    entry = runtime(platform_id)
    entry.last_error = str(text)[:300]
    entry.last_error_at = time.time()


class LogBuffer(logging.Handler):
    """In-memory ring of this plugin's recent log records."""

    def __init__(self, capacity=LOG_CAPACITY):
        super().__init__(level=logging.INFO)
        self.records = collections.deque(maxlen=capacity)
        self.seq = 0
        self.attached = []
        self.addFilter(self._ours)

    @staticmethod
    def _ours(record):
        return record.name.endswith(PLUGIN_NAME) or PLUGIN_NAME in (
            record.pathname or ""
        )

    def emit(self, record):
        try:
            text = record.getMessage()
        except Exception:  # malformed format args must not break logging
            text = str(record.msg)
        self.seq += 1
        self.records.append(
            {
                "seq": self.seq,
                "time": record.created,
                "level": record.levelname,
                "message": text[:2000],
            }
        )

    def since(self, after):
        return [item for item in self.records if item["seq"] > after]

    def attach(self):
        # Per-plugin loggers do not propagate, so watch both the plugin logger
        # and AstrBot's shared one (the filter keeps only this plugin's lines).
        for name in (f"astrbot.plugin.{PLUGIN_NAME}", "astrbot"):
            target = logging.getLogger(name)
            if self not in target.handlers:
                target.addHandler(self)
                self.attached.append(target)

    def detach(self):
        for target in self.attached:
            target.removeHandler(self)
        self.attached = []


def ok(data=None):
    return {"status": "ok", "message": "", "data": data if data is not None else {}}


def fail(message):
    return {"status": "error", "message": str(message), "data": {}}


def plugin_version():
    try:
        for line in (
            (Path(__file__).with_name("metadata.yaml"))
            .read_text(encoding="utf-8")
            .splitlines()
        ):
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def operation_summary(operation):
    operation = operation if isinstance(operation, dict) else {}
    result = operation.get("result")
    return {
        "id": operation.get("id"),
        "kind": operation.get("kind"),
        "state": operation.get("state"),
        "result": result if isinstance(result, dict) else {},
        "createdAt": operation.get("createdAt"),
        "updatedAt": operation.get("updatedAt"),
    }


def event_summary(event):
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    kind = str(event.get("kind") or "")
    keys = {
        "account.connection": ("state", "authentication", "serviceCode", "source"),
        "account.updated": ("connection", "authentication", "autoLoginSuppressed"),
        "account.push": ("state", "transport", "fallback", "reason"),
        "operation.updated": ("kind", "state"),
        "message.committed": ("direction", "origin", "threadId"),
        "contacts.sync": ("state", "complete"),
    }.get(kind)
    if keys:
        parts = [f"{key}={data[key]}" for key in keys if data.get(key) is not None]
    else:
        parts = [f"{key}={value}" for key, value in list(data.items())[:4]]
    return {
        "cursor": event.get("cursor"),
        "kind": kind,
        "createdAt": event.get("createdAt"),
        "summary": " ".join(parts)[:300],
        "data": data,
    }


def is_important(summary):
    kind = summary["kind"]
    data = summary["data"]
    if kind in ALWAYS_IMPORTANT_EVENTS:
        return True
    if kind == "account.push":  # transport changes, not per-command notices
        return data.get("state") not in (None, "notified")
    if kind == "operation.updated":  # login steps, not metadata refresh jobs
        return data.get("kind") == "login"
    return False


class WeChatPanel:
    """Routes behind the plugin view; one instance per plugin instance."""

    def __init__(self, plugin, logs):
        self.plugin = plugin
        self.logs = logs
        self.avatar_cache = {}
        self.name_cache = {}

    # ----- registration -------------------------------------------------

    def routes(self):
        return [
            ("overview", self.overview, ["GET"], "WeChat panel overview"),
            ("avatar", self.avatar, ["GET"], "WeChat bot avatar"),
            ("login", self.login, ["POST"], "Start a WeChat login"),
            ("login/attempt", self.login_attempt, ["GET"], "WeChat login progress"),
            ("login/qr", self.login_qr, ["GET"], "WeChat login QR code"),
            ("login/cancel", self.login_cancel, ["POST"], "Cancel a WeChat login"),
            ("connection", self.connection, ["POST"], "Connect or disconnect"),
            ("logout", self.logout, ["POST"], "Sign the bot account out"),
            ("events", self.events, ["GET"], "Recent bridge events"),
            ("logs", self.plugin_logs, ["GET"], "Recent plugin log lines"),
            ("threads", self.threads, ["GET"], "WeChat chats"),
            ("contacts", self.contacts, ["GET"], "WeChat contact search"),
            ("send-test", self.send_test, ["POST"], "Send a test message"),
        ]

    def register(self, context):
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            return False
        for name, handler, methods, description in self.routes():
            register(
                f"{ROUTE_PREFIX}/{name}", self.guard(handler), methods, description
            )
        return True

    @staticmethod
    def guard(handler):
        async def guarded(**path_values):
            try:
                return ok(await handler())
            except PanelError as exc:
                return fail(exc)
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                return fail(f"连不上微信桥接（{type(exc).__name__}）")
            except Exception as exc:
                logger.error("WeChat panel %s failed: %s", handler.__name__, exc)
                return fail("面板内部错误，详情见 AstrBot 日志")

        guarded.__name__ = f"wechat_panel_{handler.__name__}"
        return guarded

    # ----- request helpers ----------------------------------------------

    @staticmethod
    def query(name, default=""):
        value = request.query.get(name, default) if request is not None else default
        return value if value is not None else default

    @staticmethod
    async def body():
        data = await request.json(default={}) if request is not None else {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def username():
        try:
            return request.username or "?"
        except Exception:
            return "?"

    def platforms(self):
        config = self.plugin.context.astrbot_config_mgr.default_conf
        return [
            p
            for p in config.get("platform", [])
            if p.get("type") == "native_wechat" and p.get("enable")
        ]

    def platform(self, platform_id=None):
        platform_id = platform_id if platform_id is not None else self.query("platform")
        platforms = self.platforms()
        if not platforms:
            raise PanelError("没有启用的本机微信平台")
        if not platform_id and len(platforms) == 1:
            return platforms[0]
        for item in platforms:
            if item.get("id") == platform_id:
                return item
        raise PanelError("找不到这个微信平台")

    async def bridge(self, platform, method, path, *, account=True, raw=False, **kw):
        """Call the local bridge; returns parsed JSON or (bytes, content type)."""
        if str(platform.get("server_url", "")).rstrip("/") != LOCAL_BRIDGE:
            raise PanelError("只允许访问本机桥接 127.0.0.1:6174")
        try:
            token = Path(platform["token_file"]).read_text().strip()
        except (KeyError, OSError) as exc:
            raise PanelError("读不到桥接令牌文件") from exc
        url = LOCAL_BRIDGE
        if account:
            url += "/v1/accounts/" + quote(str(platform.get("account_id", "")), safe="")
        url += path
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=15),
            trust_env=False,
        ) as client:
            async with client.request(
                method, url, allow_redirects=False, **kw
            ) as response:
                payload = await response.read()
                content_type = response.headers.get("content-type", "")
                if response.status >= 400:
                    code = message = None
                    try:
                        error = json.loads(payload).get("error") or {}
                        code, message = error.get("code"), error.get("message")
                    except (ValueError, AttributeError):
                        pass
                    raise BridgeError(response.status, code, message)
                if raw:
                    return payload, content_type
                return json.loads(payload) if payload else {}

    # ----- dashboard ----------------------------------------------------

    async def overview(self):
        platform = self.platform()
        pid = platform.get("id")
        account = None
        bridge_status = None
        error = None
        try:
            account = await self.bridge(platform, "GET", "")
            bridge_status = await self.bridge(
                platform, "GET", "/v1/status", account=False
            )
        except (PanelError, aiohttp.ClientError, TimeoutError, OSError) as exc:
            error = str(exc) if isinstance(exc, PanelError) else type(exc).__name__
        own = (account or {}).get("ownUsername")
        alert_entry = dict((getattr(self.plugin, "alert_state", {}) or {}).get(pid, {}))
        since = alert_entry.get("since")
        offline_for = (
            max(0, int(time.monotonic() - since))
            if isinstance(since, int | float)
            else 0
        )
        watch = getattr(self.plugin, "watch_task", None)
        allowed_groups = list(platform.get("allowed_groups") or [])
        version = (bridge_status or {}).get("version") or {}
        return {
            "platforms": [
                {"id": p.get("id"), "accountId": p.get("account_id")}
                for p in self.platforms()
            ],
            "platformId": pid,
            "pluginVersion": plugin_version(),
            "bridge": {
                "reachable": error is None,
                "error": error,
                "state": (bridge_status or {}).get("state"),
                "version": version.get("version"),
                "target": version.get("target"),
            },
            "account": {
                key: (account or {}).get(key)
                for key in (
                    "id",
                    "alias",
                    "authenticated",
                    "connection",
                    "incomingTransport",
                    "autoConnect",
                    "ownUsername",
                    "contactSyncReady",
                )
            },
            "botName": await self.contact_name(platform, own) if own else None,
            "runtime": runtime(pid).snapshot(),
            "alerts": {
                "sessions": self.plugin.alert_sessions(),
                "afterSeconds": max(
                    30, int(self.plugin.config.get("alert_after_seconds", 120) or 120)
                ),
                "watching": bool(watch and not watch.done()),
                "offline": since is not None,
                "offlineSeconds": offline_for,
                "alerted": bool(alert_entry.get("alerted")),
                "detail": alert_entry.get("detail"),
                "checkedAt": alert_entry.get("checked_at"),
            },
            "policy": {
                "allowedGroups": allowed_groups,
                "privateChats": not platform.get("friend_whitelist")
                or bool(platform.get("allowed_friends")),
                "friendWhitelist": bool(platform.get("friend_whitelist")),
                "allowedFriends": len(platform.get("allowed_friends") or []),
                "proactiveMessages": bool(platform.get("proactive_messages")),
            },
        }

    async def contact_name(self, platform, username):
        cached = self.name_cache.get(username)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        name = None
        try:
            contact = await self.bridge(
                platform, "GET", "/contacts/" + quote(username, safe="")
            )
            for key in ("remark", "displayName", "nickname"):
                value = contact.get(key)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    break
        except (PanelError, aiohttp.ClientError, TimeoutError, OSError):
            pass
        self.name_cache[username] = (name, time.monotonic() + 600)
        return name

    async def avatar(self):
        platform = self.platform()
        account = await self.bridge(platform, "GET", "")
        own = account.get("ownUsername")
        if not own:
            return {"dataUrl": None}
        cached = self.avatar_cache.get(own)
        if cached and cached[0]:
            return {"dataUrl": cached[0]}
        path = "/avatars/" + quote(own, safe="")
        try:
            payload, content_type = await self.bridge(platform, "GET", path, raw=True)
        except BridgeError as exc:
            retry_at = cached[1] if cached else 0
            if exc.status == 404 and time.monotonic() >= retry_at:
                # Not cached yet: ask the bridge to fetch it for next time.
                self.avatar_cache[own] = (None, time.monotonic() + AVATAR_RETRY_SECONDS)
                try:
                    await self.bridge(platform, "POST", path + "/refresh", json={})
                except (PanelError, aiohttp.ClientError, TimeoutError, OSError):
                    pass
            return {"dataUrl": None}
        mime = content_type.split(";", 1)[0].strip()
        if not mime.startswith("image/") or not payload:
            return {"dataUrl": None}
        data_url = f"data:{mime};base64," + base64.b64encode(payload).decode()
        self.avatar_cache[own] = (data_url, 0)
        return {"dataUrl": data_url}

    # ----- login --------------------------------------------------------

    async def login(self):
        body = await self.body()
        platform = self.platform(body.get("platform") or None)
        strategy = body.get("strategy")
        if strategy not in ("qr", "saved-device"):
            raise PanelError("登录方式只能是扫码或设备确认")
        # Same body as `wx auth login --detach`; auto refresh renews expired codes.
        operation = await self.bridge(
            platform,
            "POST",
            "/auth/login",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "allowQr": True,
                "qrRefresh": {"maxRefreshes": QR_MAX_REFRESHES, "mode": "auto"},
                "strategy": strategy,
                "timeoutSeconds": LOGIN_TIMEOUT_SECONDS,
            },
        )
        logger.info(
            "WeChat panel: %s login started for %s by %s",
            strategy,
            platform.get("id"),
            self.username(),
        )
        return operation_summary(operation)

    @staticmethod
    def operation_path(operation_id):
        operation_id = str(operation_id or "").strip()
        if not operation_id or len(operation_id) > 128:
            raise PanelError("缺少登录操作编号")
        return "/operations/" + quote(operation_id, safe="")

    async def login_attempt(self):
        platform = self.platform()
        path = self.operation_path(self.query("id"))
        return operation_summary(await self.bridge(platform, "GET", path))

    async def login_qr(self):
        platform = self.platform()
        path = self.operation_path(self.query("id")) + "/qr"
        generation = str(self.query("generation", "")).strip()
        if generation.isdigit():
            path += "?" + urlencode({"generation": generation})
        try:
            payload, content_type = await self.bridge(platform, "GET", path, raw=True)
        except BridgeError as exc:
            if exc.code in (
                "login_challenge_unavailable",
                "login_challenge_superseded",
            ):
                return {"available": False, "reason": exc.code}
            raise
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise PanelError("桥接返回的二维码不是 PNG 图片")
        return {
            "available": True,
            "generation": generation or None,
            "contentType": content_type,
            "dataUrl": "data:image/png;base64," + base64.b64encode(payload).decode(),
        }

    async def login_cancel(self):
        body = await self.body()
        platform = self.platform(body.get("platform") or None)
        path = self.operation_path(body.get("id"))
        await self.bridge(platform, "DELETE", path)
        logger.info("WeChat panel: login cancelled by %s", self.username())
        return {"cancelled": True}

    async def connection(self):
        body = await self.body()
        platform = self.platform(body.get("platform") or None)
        action = body.get("action")
        if action not in ("connect", "disconnect"):
            raise PanelError("只支持重新连接或断开连接")
        account = await self.bridge(platform, "POST", f"/{action}", json={})
        logger.info(
            "WeChat panel: %s %s by %s", action, platform.get("id"), self.username()
        )
        return {"account": account if isinstance(account, dict) else {}}

    async def logout(self):
        body = await self.body()
        platform = self.platform(body.get("platform") or None)
        if body.get("confirm") != LOGOUT_CONFIRMATION:
            raise PanelError(f"请输入“{LOGOUT_CONFIRMATION}”确认")
        result = await self.bridge(
            platform,
            "POST",
            "/logout",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={},
        )
        logger.warning(
            "WeChat panel: %s signed out by %s", platform.get("id"), self.username()
        )
        return {"result": result if isinstance(result, dict) else {}}

    # ----- events and logs ----------------------------------------------

    def current_cursor(self, platform):
        adapter = runtime(platform.get("id")).adapter
        cursor = getattr(adapter, "cursor", None)
        if cursor is None:
            state_file = platform.get("state_file")
            try:
                cursor = json.loads(Path(state_file).read_text()).get("cursor")
            except (TypeError, OSError, ValueError, AttributeError):
                cursor = None
        try:
            return int(str(cursor))
        except (TypeError, ValueError):
            return None

    async def events(self):
        platform = self.platform()
        show_all = str(self.query("all", "0")) in ("1", "true")
        cursor = self.current_cursor(platform)
        after = max(0, cursor - EVENT_WINDOW) if cursor is not None else 0
        items = []
        for _ in range(3):  # at most 600 events: the window plus a little lag
            page = await self.bridge(
                platform,
                "GET",
                "/events?" + urlencode({"after": after, "limit": EVENT_WINDOW}),
            )
            batch = page.get("items") or []
            items.extend(batch)
            if not page.get("nextCursor") or len(batch) < EVENT_WINDOW:
                break
            after = page["nextCursor"]
        summaries = [event_summary(item) for item in items[-EVENT_WINDOW:]]
        if not show_all:
            summaries = [item for item in summaries if is_important(item)]
        summaries.reverse()
        return {"items": summaries, "cursor": cursor, "all": show_all}

    async def plugin_logs(self):
        try:
            after = int(self.query("after", "0") or 0)
        except ValueError:
            after = 0
        return {"items": self.logs.since(after), "seq": self.logs.seq}

    # ----- chats --------------------------------------------------------

    def permitted(self, platform, thread):
        """Whether the plugin would answer in a thread (mirrors the adapter)."""
        kind = thread.get("kind")
        thread_id = thread.get("id")
        if kind == "group":
            return thread_id in set(platform.get("allowed_groups") or [])
        if kind != "direct":
            return False
        if platform.get("friend_whitelist"):
            return thread_id in set(platform.get("allowed_friends") or [])
        return True

    async def threads(self):
        platform = self.platform()
        params = {"limit": 100}
        cursor = str(self.query("cursor", "")).strip()
        if cursor:
            params["cursor"] = cursor
        page = await self.bridge(platform, "GET", "/threads?" + urlencode(params))
        items = []
        for thread in page.get("items") or []:
            last = thread.get("lastMessage") or {}
            items.append(
                {
                    "id": thread.get("id"),
                    "kind": thread.get("kind"),
                    "name": thread.get("displayName") or thread.get("id"),
                    "unread": thread.get("unreadCount") or 0,
                    "muted": bool(thread.get("muted")),
                    "pinned": bool(thread.get("pinned")),
                    "permitted": self.permitted(platform, thread),
                    "last": {
                        "text": str(last.get("text") or "")[:160],
                        "sender": last.get("senderName") or last.get("senderId"),
                        "direction": last.get("direction"),
                        "createdAt": last.get("createdAt"),
                    }
                    if last
                    else None,
                }
            )
        return {"items": items, "nextCursor": page.get("nextCursor")}

    async def contacts(self):
        platform = self.platform()
        params = {"limit": 50}
        search = str(self.query("search", "")).strip()[:64]
        cursor = str(self.query("cursor", "")).strip()
        if search:
            params["search"] = search
        if cursor:
            params["cursor"] = cursor
        page = await self.bridge(platform, "GET", "/contacts?" + urlencode(params))
        items = [
            {
                "id": item.get("username") or item.get("id"),
                "displayName": item.get("displayName"),
                "nickname": item.get("nickname"),
                "remark": item.get("remark"),
                "alias": item.get("alias"),
                "category": (item.get("classification") or {}).get("category"),
            }
            for item in page.get("items") or []
            if not item.get("deleted")
        ]
        return {"items": items, "nextCursor": page.get("nextCursor")}

    async def send_test(self):
        body = await self.body()
        platform = self.platform(body.get("platform") or None)
        thread = str(body.get("thread_id") or "").strip()
        text = str(body.get("text") or "").strip()
        if not thread or not text:
            raise PanelError("请选择会话并填写内容")
        if len(text) > SEND_TEST_MAX_CHARS:
            raise PanelError(f"测试消息最多 {SEND_TEST_MAX_CHARS} 字")
        adapter = runtime(platform.get("id")).adapter
        if adapter is None:
            raise PanelError("微信适配器没有在运行")
        try:
            # Same rule as replies: approved groups and admitted private chats.
            allowed = await adapter.thread_allowed(thread)
        except RuntimeError as exc:
            raise PanelError(f"查询会话失败：{exc}") from exc
        if not allowed:
            raise PanelError("机器人不在这个会话里回复，不能发测试消息")
        key = hashlib.sha256(f"panel:{thread}:{time.time_ns()}".encode()).hexdigest()
        try:
            await adapter.send_text(thread, text, key)
        except (RuntimeError, ValueError) as exc:
            raise PanelError(f"发送失败：{exc}") from exc
        logger.info(
            "WeChat panel: test message sent to %s by %s", thread, self.username()
        )
        return {"sent": True}
