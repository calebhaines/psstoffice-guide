#!/usr/bin/env python3
"""Exercise the standalone chat in Chromium using synthetic API responses.

Development-only dependencies: ``python -m pip install playwright cryptography`` and
``python -m playwright install chromium``. An installed system Chromium can be
selected with PLAYWRIGHT_CHROMIUM_EXECUTABLE. No real rooms or messages are accessed.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from playwright.async_api import async_playwright, expect
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


ROOT = Path(__file__).resolve().parents[1]
BASE = "https://psstoffice.89b84f87.nip.io"
TOKEN_A = "Aa01" * 16
TOKEN_B = "Bb02" * 16
EMPTY_PAGE = {"messages": [], "next_cursor": None, "has_more": False}
EMPTY_FILES = {"files": [], "next_cursor": None, "has_more": False, "next_upload_at": None}


def status_receipt(text="Secret picnic ready", color="green", *, expires=None):
    expiration = expires or datetime.now(timezone.utc) + timedelta(hours=2)
    return {
        "id": str(uuid.uuid4()),
        "text": text,
        "color": color,
        "updated_at": (expiration - timedelta(hours=2)).isoformat(),
        "expires_at": expiration.isoformat(),
    }


def file_receipt(data=b"Synthetic file", filename="secret.txt", username="test-agent", *, expires=None):
    now = datetime.now(timezone.utc)
    return {
        "id": str(uuid.uuid4()),
        "filename": filename,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "username": username,
        "created_at": now.isoformat(),
        "expires_at": (expires or now + timedelta(hours=2)).isoformat(),
    }


def files_page(*files, cursor=None, has_more=False, next_upload_at=None):
    if files and cursor is None:
        cursor = "synthetic-file-cursor"
    return {"files": list(files), "next_cursor": cursor, "has_more": has_more,
            "next_upload_at": next_upload_at}


def receipt(message="Synthetic mail", username="test-agent", *, expires=None, signing=None,
            message_type=None, reply_to=None):
    now = datetime.now(timezone.utc)
    value = {
        "id": str(uuid.uuid4()),
        "message": message,
        "username": username,
        "signing": signing,
        "created_at": now.isoformat(),
        "expires_at": (expires or now + timedelta(hours=2)).isoformat(),
    }
    if message_type is not None:
        value["message_type"] = message_type
    if reply_to is not None:
        value["reply_to"] = reply_to
    return value


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def from_b64url(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical_bytes(token, message, username, nonce, signed_at, *,
                    message_type=None, reply_to=None):
    """Independent Python protocol encoder, including its Unicode handling."""
    payload = {
        "message": message,
        "nonce": nonce,
        "room_token": token,
        "signed_at": signed_at,
        "username": username,
    }
    domain = b"agentboard-message-v1\n"
    if message_type is not None or reply_to is not None:
        domain = b"psstoffice-message-v2\n"
        payload.update(message_type=message_type, reply_to=reply_to)
    return domain + json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def signed_receipt(message="Signed synthetic mail", username="signed-agent", *, key=None,
                   token=TOKEN_A, expires=None, signed_at=None, message_type=None, reply_to=None):
    key = key or Ed25519PrivateKey.generate()
    timestamp = int(time.time()) if signed_at is None else signed_at
    nonce = uuid.uuid4().hex
    signing = {
        "public_key": b64url(key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
        "signature": b64url(key.sign(canonical_bytes(token, message, username, nonce, timestamp,
                                                      message_type=message_type, reply_to=reply_to))),
        "nonce": nonce,
        "signed_at": timestamp,
    }
    return receipt(message, username, expires=expires, signing=signing,
                   message_type=message_type, reply_to=reply_to)


def verify_post(post, token=TOKEN_A):
    """Browser signatures must work with a separate Ed25519 implementation."""
    signing = post["signing"]
    assert set(signing) == {"public_key", "signature", "nonce", "signed_at"}
    assert re.fullmatch(r"[0-9a-f]{32}", signing["nonce"])
    assert isinstance(signing["signed_at"], int) and not isinstance(signing["signed_at"], bool)
    public = from_b64url(signing["public_key"])
    signature = from_b64url(signing["signature"])
    assert len(public) == 32 and b64url(public) == signing["public_key"]
    assert len(signature) == 64 and b64url(signature) == signing["signature"]
    Ed25519PublicKey.from_public_bytes(public).verify(signature, canonical_bytes(
        token, post["message"], post.get("username"), signing["nonce"], signing["signed_at"],
        message_type=post.get("message_type"), reply_to=post.get("reply_to")))
    return public


def page_of(*messages, cursor="synthetic-cursor", has_more=False):
    return {"messages": list(messages), "next_cursor": cursor, "has_more": has_more}


class MailFixture:
    """Hold long polls until a test chooses their outcome."""

    def __init__(self, read_pages=None, send_results=None, publish_appends=True,
                 file_pages=None, upload_results=None, download_results=None,
                 status_pages=None, status_results=None, max_file_bytes=1_000_000):
        self.read_pages = deque([EMPTY_PAGE.copy()] if read_pages is None else read_pages)
        self.send_results = deque(send_results or [])
        self.publish_appends = publish_appends
        self.append_receipts = []
        self.requests = []
        self.pending_reads = []
        self.closed = False
        self.file_pages = deque(file_pages or [])
        self.upload_results = deque(upload_results or [])
        self.download_results = deque(download_results or [])
        self.file_metadata = {}
        self.file_bytes = {}
        self.status_pages = deque(status_pages or [])
        self.status_results = deque(status_results or [])
        self.room_status = {}
        self.max_file_bytes = max_file_bytes

    async def handle(self, route):
        request = route.request
        headers = await request.all_headers()
        if request.method == "OPTIONS":
            await route.fulfill(
                status=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST",
                    "Access-Control-Allow-Headers": "Content-Type, X-Room-Token, X-File-Name, X-File-Username, X-File-Size, X-Client-File-ID",
                },
            )
            return
        if request.url == BASE + "/v1/files/upload":
            body = {"filename": from_b64url(headers["x-file-name"]).decode("utf-8"),
                    "data": request.post_data_buffer or b"", "size_bytes": int(headers["x-file-size"]),
                    "client_file_id": headers["x-client-file-id"]}
            if "x-file-username" in headers:
                body["username"] = from_b64url(headers["x-file-username"]).decode("utf-8")
        else:
            body = json.loads(request.post_data or "{}")
        record = {"url": request.url, "body": body, "headers": headers, "time": time.monotonic()}
        self.requests.append(record)
        if request.url == BASE + "/":
            assert request.method == "GET" and "x-room-token" not in headers
            result = {"service": "PsstOffice", "max_file_bytes": self.max_file_bytes,
                      "max_file_storage_bytes": 5_000_000_000}
        elif request.url == BASE + "/v1/messages/read":
            if self.read_pages:
                result = self.read_pages.popleft()
            else:
                future = asyncio.get_running_loop().create_future()
                self.pending_reads.append((record, future))
                result = await future
        elif request.url == BASE + "/v1/messages":
            if self.send_results:
                result = self.send_results.popleft()
            else:
                result = (201, receipt(body["message"], body.get("username"), signing=body.get("signing"),
                                       message_type=body.get("message_type"), reply_to=body.get("reply_to")), {})
            if callable(result):
                result = result(body)
            if asyncio.iscoroutine(result):
                result = await result
        elif request.url == BASE + "/v1/files/read":
            if self.file_pages:
                result = self.file_pages.popleft()
            else:
                token = headers.get("x-room-token")
                result = files_page(*self.file_metadata.get(token, {}).values())
        elif request.url in {BASE + "/v1/files", BASE + "/v1/files/upload"}:
            if self.upload_results:
                result = self.upload_results.popleft()
            else:
                data = body["data"] if "data" in body else base64.b64decode(body["data_base64"], validate=True)
                metadata = file_receipt(data, body["filename"], body.get("username"))
                result = (201, metadata, {})
        elif request.url == BASE + "/v1/files/download":
            if self.download_results:
                result = self.download_results.popleft()
            else:
                value = self.file_bytes.get(body["file_id"])
                result = (200, value, {"Content-Disposition": 'attachment; filename="synthetic.bin"'}) if value is not None else (
                    404, {"error": {"code": "file_not_found", "message": "File is unavailable"}}, {})
        elif request.url == BASE + "/v1/status/read":
            result = self.status_pages.popleft() if self.status_pages else {
                "status": self.room_status.get(headers.get("x-room-token"))}
        elif request.url == BASE + "/v1/status":
            result = self.status_results.popleft() if self.status_results else status_receipt(
                body["text"], body["color"])
        else:
            raise AssertionError(f"Unexpected API route: {request.url}")
        if callable(result):
            result = result(body)
        if asyncio.iscoroutine(result):
            result = await result
        if result == "abort":
            await route.abort("failed")
            return
        if isinstance(result, tuple):
            status, value, extra_headers = result
        else:
            status, value, extra_headers = 200, result, {}
        if request.url == BASE + "/v1/messages" and status == 201:
            self.append_receipts.append(value)
        if request.url in {BASE + "/v1/files", BASE + "/v1/files/upload"} and status == 201:
            token = headers.get("x-room-token")
            self.file_metadata.setdefault(token, {})[value["id"]] = value
            self.file_bytes[value["id"]] = body["data"] if "data" in body else base64.b64decode(body["data_base64"], validate=True)
        if request.url == BASE + "/v1/status" and status == 200:
            self.room_status[headers.get("x-room-token")] = value
        if request.url == BASE + "/v1/messages" and status == 201 and self.publish_appends:
            # The UI uses reads to establish insertion order, including its own
            # messages; publishing an append receipt simulates that next read.
            delivery = page_of(value, cursor="receipt-" + value["id"])
            for read_record, future in self.pending_reads:
                if not future.done() and read_record["headers"].get("x-room-token") == headers.get("x-room-token"):
                    future.set_result(delivery)
                    break
            else:
                self.read_pages.append(delivery)
        try:
            await route.fulfill(
                status=status,
                body=value if isinstance(value, bytes) else json.dumps(value),
                content_type="application/octet-stream" if isinstance(value, bytes) else "application/json",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Expose-Headers": "Retry-After",
                    "Cache-Control": "no-store",
                    **extra_headers,
                },
            )
        except Exception:
            if not self.closed:
                raise

    def reads(self):
        return [r for r in self.requests if r["url"].endswith("/messages/read")]

    def sends(self):
        return [r for r in self.requests if r["url"].endswith("/messages")]

    def file_reads(self):
        return [r for r in self.requests if r["url"].endswith("/files/read")]

    def uploads(self):
        return [r for r in self.requests if r["url"].endswith(("/files", "/files/upload"))]

    def downloads(self):
        return [r for r in self.requests if r["url"].endswith("/files/download")]

    def status_reads(self):
        return [r for r in self.requests if r["url"].endswith("/status/read")]

    def status_updates(self):
        return [r for r in self.requests if r["url"].endswith("/status")]

    def release(self, result, token=None):
        for record, future in self.pending_reads:
            if not future.done() and (token is None or record["headers"].get("x-room-token") == token):
                future.set_result(result)
                return
        raise AssertionError("No matching held poll")

    def close(self):
        self.closed = True
        for _, future in self.pending_reads:
            if not future.done():
                future.set_result(EMPTY_PAGE.copy())


INSTRUMENT_FETCH = """
window.__fetch_checks = [];
const originalFetch = window.fetch.bind(window);
window.fetch = (url, options = {}) => {
  window.__fetch_checks.push({url: String(url), credentials: options.credentials,
    redirect: options.redirect, cache: options.cache, referrerPolicy: options.referrerPolicy});
  return originalFetch(url, options);
};
window.__secure_random_calls = [];
const originalRandom = crypto.getRandomValues.bind(crypto);
crypto.getRandomValues = array => {
  window.__secure_random_calls.push(array.byteLength);
  return originalRandom(array);
};
window.__copied_text = [];
if (navigator.clipboard?.writeText) {
  navigator.clipboard.writeText = async value => { window.__copied_text.push(String(value)); };
}
"""


async def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for browser activity")
        await asyncio.sleep(0.025)


class BrowserCase:
    def __init__(self, browser, fixture=None, viewport=None, init_script=None):
        self.browser = browser
        self.fixture = fixture or MailFixture()
        self.viewport = viewport or {"width": 1280, "height": 900}
        self.init_script = init_script

    async def __aenter__(self):
        self.context = await self.browser.new_context(viewport=self.viewport)
        self.unexpected_requests = []
        self.context.on("request", lambda request: self.unexpected_requests.append(request.url)
                        if request.url.startswith(("http://", "https://")) and not request.url.startswith(BASE + "/") else None)
        await self.context.add_init_script(INSTRUMENT_FETCH)
        if self.init_script:
            await self.context.add_init_script(self.init_script)
        await self.context.route(BASE + "/**", self.fixture.handle)
        self.page = await self.context.new_page()
        self.errors = []
        self.failed_requests = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("requestfailed", lambda request: self.failed_requests.append({"url": request.url, "failure": request.failure}))
        await self.page.goto((ROOT / "chat.html").as_uri())
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.fixture.close()
        await self.context.close()
        if exc is None:
            assert not self.errors, f"Browser script errors: {self.errors}"
            assert not self.unexpected_requests, f"Standalone chat loaded external resources: {self.unexpected_requests}"

    async def join(self, token=TOKEN_A, username="human-test"):
        await self.page.locator("#room-token").fill(token)
        await self.page.locator("#username").fill(username)
        await self.page.locator("#join-button").click()
        await expect(self.page.locator("#chat-view")).to_be_visible()
        await wait_until(lambda: bool(self.fixture.reads()))


class RoomFixture:
    """Separate API queues and held requests for every synthetic room token."""

    def __init__(self, rooms):
        self.rooms = rooms

    async def handle(self, route):
        headers = await route.request.all_headers()
        if route.request.method == "OPTIONS" or route.request.url == BASE + "/":
            fixture = next(iter(self.rooms.values()))
        else:
            token = headers.get("x-room-token")
            assert token in self.rooms, "An unknown room token was sent"
            fixture = self.rooms[token]
        await fixture.handle(route)

    @property
    def requests(self):
        return sorted((request for room in self.rooms.values() for request in room.requests),
                      key=lambda request: request["time"])

    def reads(self):
        return [request for request in self.requests if request["url"].endswith("/messages/read")]

    def file_reads(self):
        return [request for request in self.requests if request["url"].endswith("/files/read")]

    def close(self):
        for room in self.rooms.values():
            room.close()


def room_tab(page, number):
    return page.locator('#room-tabs .room-tab[role="tab"]').filter(has_text=f"Room {number}")


async def add_room(case, token=TOKEN_B, username="second-agent"):
    await case.page.locator("#add-room").click()
    await expect(case.page.locator("#join-view")).to_be_visible()
    await case.join(token, username)


async def switch_room(page, number):
    await room_tab(page, number).click()
    await expect(room_tab(page, number)).to_have_attribute("aria-selected", "true")
    await expect(page.locator("#chat-view")).to_be_visible()


async def test_token_and_privacy(browser):
    async with BrowserCase(browser) as case:
        p = case.page
        await p.locator("#room-token").fill("short-token")
        await p.locator("#join-button").click()
        assert not case.fixture.requests, "An invalid token must never be transmitted"
        await p.locator("#generate-token").click()
        generated = await p.locator("#room-token").input_value()
        assert re.fullmatch(r"[0-9a-f]{64}", generated), "Generated token must contain 256 random bits"
        assert 32 in await p.evaluate("window.__secure_random_calls"), "Token generation must use Web Crypto"
        await case.join()
        assert case.fixture.reads()[0]["headers"]["x-room-token"] == TOKEN_A, "Room tokens are case sensitive"
        assert TOKEN_A not in p.url, "Token leaked into browser URL"
        stores = await p.evaluate("({local: localStorage.length, session: sessionStorage.length})")
        assert stores == {"local": 0, "session": 0}, "The browser must not persist room secrets"
        await p.locator("#leave-room").click()
        assert not await p.locator("#room-token").input_value(), "Leaving must clear the token"


async def test_pagination_rendering_and_layout(browser):
    malicious = '<img src=x onerror="window.__xss=true"><script>window.__xss=true</script>'
    first = receipt(malicious, '<svg onload="window.__xss=true">')
    second = receipt("Parcel from the moon: cheese accounted for. 🧀", "lunar-mailbot")
    third = receipt("A tiny secret, delivered with a very large hat.", "hat-inspector")
    third["signing"] = {
        "public_key": "A" * 43,
        "signature": "B" * 86,
        "nonce": "0" * 32,
        "signed_at": int(time.time()),
    }
    fixture = MailFixture([
        page_of(first, cursor="page-one", has_more=True),
        page_of(second, third, cursor="page-two"),
    ])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await expect(p.locator("article.message")).to_have_count(3)
        await wait_until(lambda: len(fixture.reads()) >= 3)
        assert fixture.reads()[1]["body"]["after"] == "page-one"
        assert fixture.reads()[1]["body"].get("wait_seconds", 0) == 0, "Pages must drain before waiting"
        assert fixture.reads()[2]["body"]["after"] == "page-two"
        assert fixture.reads()[2]["body"]["wait_seconds"] == 25, "Idle reads should long poll"
        assert malicious in await p.locator("#messages").inner_text()
        assert await p.locator("#messages img, #messages svg, #messages script").count() == 0
        assert await p.evaluate("window.__xss === undefined")
        await expect(p.locator(".signature")).to_have_attribute("data-state", "invalid")
        for request in await p.evaluate("window.__fetch_checks"):
            assert request["credentials"] == "omit", "Do not send ambient cookies"
            assert request["redirect"] == "error", "Do not forward tokens through redirects"
            assert request["cache"] == "no-store"
            assert request["referrerPolicy"] == "no-referrer"
        await p.set_viewport_size({"width": 390, "height": 844})
        await p.locator("#message-input").fill("One-handed mail delivery ✉️")
        assert await p.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile layout overflows horizontally"


async def test_utf8_and_keyboard(browser):
    async with BrowserCase(browser) as case:
        p, fixture = case.page, case.fixture
        await case.join()
        composer = p.locator("#message-input")
        await composer.fill("😀" * 4097)
        assert "16388" in re.sub(r"[ ,]", "", await p.locator("#byte-count").inner_text())
        if await p.locator("#send-button").is_enabled():
            await p.locator("#send-button").click()
        assert not fixture.sends(), "UTF-8 byte limit must reject oversized messages before network"
        await composer.fill("Hello, IME")
        await composer.dispatch_event("keydown", {"key": "Enter", "code": "Enter", "ctrlKey": True, "isComposing": True})
        await asyncio.sleep(0.1)
        assert not fixture.sends(), "IME confirmation must not submit a message"
        await composer.press("Shift+Enter")
        assert "\n" in await composer.input_value(), "Shift+Enter should create a newline"
        await p.locator("#active-username").fill("🐦" * 65)
        await composer.fill("A small valid message with an oversized name.")
        await p.locator("#send-button").click()
        assert not fixture.sends(), "Username limit must count Unicode characters"
        await p.locator("#active-username").fill("🐦" * 64)
        await composer.fill("😀" * 4096)
        await composer.press("Control+Enter")
        await wait_until(lambda: len(fixture.sends()) == 1)
        assert fixture.sends()[0]["body"]["message"] == "😀" * 4096
        assert fixture.sends()[0]["body"]["username"] == "🐦" * 64
        assert re.fullmatch(r"[0-9a-f]{32}", fixture.sends()[0]["body"]["client_message_id"])
        await expect(p.locator("article.message")).to_have_count(1)


async def test_uncertain_send_retry(browser):
    fixture = MailFixture(send_results=["abort"])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await p.locator("#message-input").fill("Synthetic uncertain parcel 🦆")
        await p.locator("#send-button").click()
        await expect(p.locator("#retry-send")).to_be_visible()
        assert len(fixture.sends()) == 1
        first = fixture.sends()[0]
        await p.locator("#retry-send").click()
        await wait_until(lambda: len(fixture.sends()) == 2)
        assert fixture.sends()[1]["body"] == first["body"], "Retry changed payload or deduplication ID"
        assert fixture.sends()[1]["headers"]["x-room-token"] == first["headers"]["x-room-token"]
        await expect(p.locator("article.message")).to_have_count(1)
        await expect(p.locator("#pending-notice")).not_to_be_visible()


async def test_ack_before_earlier_read(browser):
    first, second = receipt("First in server order."), receipt("Second in server order.")
    fixture = MailFixture([], publish_appends=False)
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await p.locator("#message-input").fill("Third in server order: my note.")
        await p.locator("#send-button").click()
        await wait_until(lambda: bool(fixture.append_receipts))
        await expect(p.locator("#message-input")).to_have_value("")
        assert await p.locator("article.message").count() == 0, "Append acknowledgement raced earlier history"
        mine = fixture.append_receipts[0]
        fixture.read_pages.append(page_of(mine, cursor="newest-cursor"))
        fixture.release(page_of(first, second, cursor="earlier-page", has_more=True))
        await expect(p.locator("article.message")).to_have_count(3)
        rendered = await p.locator("article.message").evaluate_all("rows => rows.map(row => row.dataset.messageId)")
        assert rendered == [first["id"], second["id"], mine["id"]], "Conversation lost server insertion order"
        await expect(p.locator(f'article.message[data-message-id="{mine["id"]}"]')).to_have_class(re.compile(r"\bown\b"))


async def test_retry_lifetime(browser):
    fixture = MailFixture(send_results=["abort"])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        draft = "Keep this synthetic draft beyond its retry window."
        await p.locator("#message-input").fill(draft)
        await p.locator("#send-button").click()
        await expect(p.locator("#retry-send")).to_be_visible()
        original = fixture.sends()[0]["body"]
        await p.clock.fast_forward(7_200_001)
        await expect(p.locator("#retry-send")).to_be_disabled()
        await expect(p.locator("#new-attempt")).to_be_visible()
        assert len(fixture.sends()) == 1, "Expired retry must never resend automatically"
        await p.locator("#new-attempt").click()
        await expect(p.locator("#message-input")).to_have_value(draft)
        assert len(fixture.sends()) == 1, "Clearing retry must not send a fresh note"
        await p.locator("#send-button").click()
        await wait_until(lambda: len(fixture.sends()) == 2)
        assert fixture.sends()[1]["body"]["message"] == draft
        assert fixture.sends()[1]["body"]["client_message_id"] != original["client_message_id"]


async def test_expiry_and_stale_room(browser):
    soon = receipt("This synthetic message expires shortly.", expires=datetime.now(timezone.utc) + timedelta(seconds=2))
    fixture = MailFixture([page_of(soon, cursor="room-a-cursor")])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await expect(p.locator("article.message")).to_have_count(1)
        await expect(p.locator("article.message")).to_have_count(0, timeout=6000)
        await wait_until(lambda: bool(fixture.pending_reads))
        await p.locator("#leave-room").click()
        await wait_until(lambda: any(r["url"].endswith("/read") for r in case.failed_requests))
        fixture.read_pages.append(page_of(receipt("Only room B belongs here."), cursor="room-b-cursor"))
        await case.join(TOKEN_B)
        fixture.release(page_of(receipt("STALE ROOM A RESPONSE"), cursor="old-room-cursor"), TOKEN_A)
        await expect(p.locator("article.message")).to_have_count(1)
        assert "Only room B belongs here." in await p.locator("#messages").inner_text()
        assert "STALE ROOM A RESPONSE" not in await p.locator("#messages").inner_text()
        b_reads = [r for r in fixture.reads() if r["headers"].get("x-room-token") == TOKEN_B]
        assert "after" not in b_reads[0]["body"] or b_reads[0]["body"]["after"] is None, "Room switch reused another room cursor"
        await p.locator("#leave-room").click()
        count = len(fixture.reads())
        await asyncio.sleep(0.25)
        assert len(fixture.reads()) == count, "Leaving should stop further polling"
        assert await p.locator("article.message").count() == 0, "Leaving should clear message memory/UI"


async def test_retry_after(browser):
    limited = (429, {"error": {"code": "client_busy", "message": "Synthetic quota reached"}}, {"Retry-After": "2"})
    fixture = MailFixture([limited, EMPTY_PAGE.copy()])
    async with BrowserCase(browser, fixture) as case:
        await case.join()
        await wait_until(lambda: len(fixture.reads()) >= 2, timeout=8)
        delay = fixture.reads()[1]["time"] - fixture.reads()[0]["time"]
        assert delay >= 1.9, f"Ignored Retry-After: retried after {delay:.3f}s"
        await expect(case.page.locator("#connection-status")).to_have_attribute("data-state", "connected")


async def make_key(page):
    await page.locator("#manage-key").click()
    await expect(page.locator("#key-dialog")).to_be_visible()
    await page.locator("#generate-key").click()
    await expect(page.locator("#public-key")).to_have_value(re.compile(r"^[A-Za-z0-9_-]{43}$"))
    public = await page.locator("#public-key").input_value()
    await expect(page.locator("#signing-toggle")).to_be_checked()
    return public


async def export_key(page):
    async with page.expect_download() as download_info:
        await page.locator("#export-key").click()
    download = await download_info.value
    assert download.suggested_filename == "psstoffice-signing-key.pem"
    path = await download.path()
    pem = Path(path).read_text()
    assert pem.startswith("-----BEGIN PRIVATE KEY-----")
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return pem, key


async def signed_send(case, message):
    await case.page.locator("#message-input").fill(message)
    await case.page.locator("#send-button").click()
    await expect(case.page.locator("#message-input")).to_have_value("")
    return case.fixture.sends()[-1]["body"]


async def test_key_generation_export_import_and_interop(browser):
    async with BrowserCase(browser) as case:
        p = case.page
        await case.join(username='snowman ☃️ "\\\t')
        public = await make_key(p)
        pem, private = await export_key(p)
        assert b64url(private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)) == public
        await p.locator("#copy-public-key").click()
        assert await p.evaluate("window.__copied_text") == [public], "Copy public key must never copy private bytes"
        await p.locator("#close-key").click()
        post = await signed_send(case, 'Quotes " and slash \\; controls \b\f\n\t\x00; emoji 🧀; lines \u2028\u2029.')
        assert b64url(verify_post(post)) == public
        private.public_key().verify(from_b64url(post["signing"]["signature"]), canonical_bytes(
            TOKEN_A, post["message"], post["username"], post["signing"]["nonce"], post["signing"]["signed_at"]))
        await expect(p.locator(".signature")).to_have_attribute("data-state", "verified")
        await p.locator("#signing-toggle").uncheck()
        unsigned = await signed_send(case, "Optional signing is really optional.")
        assert unsigned.get("signing") is None
        await p.locator("#manage-key").click()
        await p.locator("#key-import-text").fill("not a private key")
        await p.locator("#import-key").click()
        await expect(p.locator("#key-error")).not_to_have_text("")
        await expect(p.locator("#public-key")).to_have_value(public)
        wrong_type = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode("ascii")
        await p.locator("#key-import-text").fill(wrong_type)
        await p.locator("#import-key").click()
        await expect(p.locator("#key-error")).not_to_have_text("")
        await expect(p.locator("#public-key")).to_have_value(public)
        await p.locator("#clear-key").click()
        await expect(p.locator("#public-key")).to_have_value("")
        await p.locator("#key-import-file").set_input_files({
            "name": "synthetic-signing-key.pem", "mimeType": "application/x-pem-file",
            "buffer": pem.encode("ascii"),
        })
        await p.locator("#import-key").click()
        await expect(p.locator("#public-key")).to_have_value(public)
        assert await p.locator("#key-import-file").evaluate("input => input.files.length") == 0
        await p.locator("#clear-key").click()
        await expect(p.locator("#public-key")).to_have_value("")
        await p.locator("#key-import-text").fill(pem)
        await p.locator("#import-key").click()
        await expect(p.locator("#public-key")).to_have_value(public)
        await expect(p.locator("#key-import-text")).to_have_value("")
        await p.locator("#close-key").click()
        await p.locator("#active-username").fill("")
        anonymous_post = await signed_send(case, "The same signing key, without a display name.")
        assert anonymous_post.get("username") is None
        assert b64url(verify_post(anonymous_post)) == public
        encoded_secret = "".join(pem.splitlines()[1:-1])
        raw_secret = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                           serialization.NoEncryption())
        for request in case.fixture.requests:
            transmitted = json.dumps(request)
            assert "PRIVATE KEY" not in transmitted and encoded_secret not in transmitted
            assert b64url(raw_secret) not in transmitted and raw_secret.hex() not in transmitted
        stores = await p.evaluate("({local: localStorage.length, session: sessionStorage.length})")
        assert stores == {"local": 0, "session": 0}
        assert await p.evaluate("indexedDB.databases()") == []
        assert TOKEN_A not in p.url and encoded_secret not in p.url
        await p.locator("#leave-room").click()
        await case.join()
        await p.locator("#manage-key").click()
        await expect(p.locator("#public-key")).to_have_value("")
        await expect(p.locator("#signing-toggle")).not_to_be_checked()


async def test_signed_retry_refresh_and_original_receipt(browser):
    original = {}

    def lose_original_response(body):
        original["receipt"] = receipt(body["message"], body.get("username"), signing=body["signing"])
        return "abort"

    def replay_original_response(body):
        return (201, original["receipt"], {})

    fixture = MailFixture(send_results=[lose_original_response, replay_original_response], publish_appends=False)
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        public = await make_key(p)
        await p.locator("#close-key").click()
        await p.locator("#message-input").fill("A signed retry keeps its original author and parcel.")
        await p.locator("#send-button").click()
        await expect(p.locator("#retry-send")).to_be_visible()
        first = fixture.sends()[0]["body"]
        verify_post(first)
        await expect(p.locator("#signing-toggle")).to_be_disabled()
        await expect(p.locator("#active-username")).to_be_disabled()
        await expect(p.locator("#message-input")).to_have_attribute("readonly", "")
        await p.clock.fast_forward(310_000)
        await p.locator("#retry-send").click()
        await wait_until(lambda: len(fixture.sends()) == 2)
        second = fixture.sends()[1]["body"]
        verify_post(second)
        assert second["message"] == first["message"] and second["username"] == first["username"]
        assert second["client_message_id"] == first["client_message_id"]
        assert second["signing"]["public_key"] == first["signing"]["public_key"] == public
        assert second["signing"]["signed_at"] >= first["signing"]["signed_at"] + 300
        assert second["signing"]["nonce"] != first["signing"]["nonce"]
        await expect(p.locator("#pending-notice")).not_to_be_visible()
        await expect(p.locator("#message-input")).to_have_value("")
        await expect(p.locator("#signing-toggle")).to_be_enabled()


async def test_wrong_signer_acknowledgement(browser):
    wrong_key = Ed25519PrivateKey.generate()

    def wrong_receipt(body):
        return (201, signed_receipt(body["message"], body.get("username"), key=wrong_key), {})

    fixture = MailFixture(send_results=[wrong_receipt], publish_appends=False)
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await make_key(p)
        await p.locator("#close-key").click()
        draft = "Do not accept an acknowledgement signed by a different key."
        await p.locator("#message-input").fill(draft)
        await p.locator("#send-button").click()
        await expect(p.locator("#pending-notice")).to_be_visible()
        await expect(p.locator("#message-input")).to_have_value(draft)
        assert await p.locator("article.message.own").count() == 0


async def test_signature_verification_and_chatter_filter(browser):
    key_a, key_b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    first = signed_receipt("First key, first name.", "same-name", key=key_a)
    renamed = signed_receipt("First key, new name.", "renamed-agent", key=key_a,
                             signed_at=int(time.time()) - 1800)
    second = signed_receipt("Second key uses the same display name.", "same-name", key=key_b)
    unsigned = receipt("Unsigned name is a separate chatter.", "same-name")
    anonymous = signed_receipt("A signed anonymous author.", None, key=key_b)
    tampered = signed_receipt("This signature was for other text.", "same-name", key=key_a)
    tampered["message"] = "TAMPERED message must not join the trusted first key."
    wrong_room = signed_receipt("Signature belongs to a different room.", "same-name", key=key_a, token=TOKEN_B)
    small_order = receipt("Identity-point forgery must never be trusted.", "same-name", signing={
        "public_key": b64url(bytes([1]) + bytes(31)),
        "signature": b64url(bytes([1]) + bytes(63)),
        "nonce": "0" * 32,
        "signed_at": int(time.time()),
    })
    noncanonical_point = receipt("Noncanonical curve encoding must not be trusted.", "same-name", signing={
        **small_order["signing"],
        "public_key": b64url((2**255 - 19).to_bytes(32, "little")),
    })
    noncanonical_base64 = receipt("Extra base64 padding bits must not be accepted.", "same-name", signing={
        **small_order["signing"],
        "public_key": "A" * 42 + "B",
    })
    invalid_r = receipt("Small-order signature R must not be trusted.", "same-name", signing={
        **small_order["signing"], "public_key": first["signing"]["public_key"],
    })
    rows = [first, renamed, second, unsigned, anonymous, tampered, wrong_room, small_order,
            noncanonical_point, noncanonical_base64, invalid_r]
    fixture = MailFixture([page_of(*rows)])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await expect(p.locator("article.message")).to_have_count(len(rows))
        article = lambda value: p.locator(f'article.message[data-message-id="{value["id"]}"]')
        for value in [first, renamed, second, anonymous]:
            await expect(article(value).locator(".signature")).to_have_attribute("data-state", "verified")
        for value in [tampered, wrong_room, small_order, noncanonical_point, noncanonical_base64, invalid_r]:
            await expect(article(value).locator(".signature")).to_have_attribute("data-state", "invalid")
        first_group = await article(first).get_attribute("data-chatter-id")
        assert first_group and first_group == await article(renamed).get_attribute("data-chatter-id")
        assert first_group != await article(second).get_attribute("data-chatter-id")
        assert first_group != await article(unsigned).get_attribute("data-chatter-id")
        assert first_group != await article(tampered).get_attribute("data-chatter-id")
        assert await article(second).get_attribute("data-chatter-id") == await article(anonymous).get_attribute("data-chatter-id")
        button = p.locator(f'.chatter-button[data-chatter-id="{first_group}"]')
        await button.click()
        await expect(p.locator("article.message:visible")).to_have_count(2)
        await expect(button).to_have_attribute("aria-pressed", "true")
        await expect(button).to_be_focused()
        await wait_until(lambda: any(not future.done() for _, future in fixture.pending_reads))
        next_a = signed_receipt("A new note from the selected key.", "third-name", key=key_a)
        next_b = signed_receipt("A new note from the other key stays hidden.", "same-name", key=key_b)
        fixture.release(page_of(next_a, next_b, cursor="next-chatter-page"))
        await expect(p.locator("article.message")).to_have_count(len(rows) + 2)
        await expect(p.locator("article.message:visible")).to_have_count(3)
        await expect(article(next_b)).not_to_be_visible()
        await expect(button).to_be_focused()
        await p.locator('.chatter-button[data-chatter-id="all"]').click()
        await expect(p.locator("article.message:visible")).to_have_count(len(rows) + 2)
        await p.locator("#leave-room").click()
        fixture.read_pages.append(page_of(receipt("Fresh room, fresh chatter.", "fresh-agent"), cursor="fresh-room"))
        await case.join(TOKEN_B)
        await expect(p.locator("article.message:visible")).to_have_count(1)
        assert first_group not in await p.locator("#chatter-list").inner_html()


async def test_chatter_expiry_and_history_trim(browser):
    soon = receipt("This chatter will expire.", "short-lived", expires=datetime.now(timezone.utc) + timedelta(seconds=20))
    older = [receipt(f"Synthetic retained-history note {index}.", "long-lived") for index in range(300)]
    fixture = MailFixture([
        page_of(soon, *older[:99], cursor="trim-one", has_more=True),
        page_of(*older[99:199], cursor="trim-two", has_more=True),
        page_of(*older[199:299], cursor="trim-three"),
    ])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        await expect(p.locator("article.message")).to_have_count(300)
        first_group = await p.locator(f'article.message[data-message-id="{soon["id"]}"]').get_attribute("data-chatter-id")
        await p.locator(f'.chatter-button[data-chatter-id={json.dumps(first_group)}]').click()
        await expect(p.locator("article.message:visible")).to_have_count(1)
        await p.clock.fast_forward(21_000)
        await expect(p.locator("article.message")).to_have_count(299)
        await expect(p.locator("article.message:visible")).to_have_count(0)
        await expect(p.locator("#messages")).to_contain_text("No live notes from this chatter")
        await p.locator('.chatter-button[data-chatter-id="all"]').click()
        await expect(p.locator("article.message:visible")).to_have_count(299)
        await wait_until(lambda: any(not future.done() for _, future in fixture.pending_reads))
        fixture.release(page_of(*[receipt(f"New retained-history note {index}.", "new-agent") for index in range(3)], cursor="trim-four"))
        await expect(p.locator("article.message")).to_have_count(300)
        assert "latest 300" in await p.locator("#message-count").inner_text()
        assert "short-lived" not in await p.locator("#chatter-list").inner_text()


async def test_unsupported_signing_keeps_unsigned_chat(browser):
    unsupported = """
    for (const name of ['generateKey', 'importKey', 'sign', 'verify']) {
      const original = crypto.subtle[name].bind(crypto.subtle);
      crypto.subtle[name] = (...args) => {
        const algorithm = name === 'importKey' ? args[2] : args[0];
        if (algorithm === 'Ed25519' || algorithm?.name === 'Ed25519')
          return Promise.reject(new DOMException('Synthetic unsupported Ed25519', 'NotSupportedError'));
        return original(...args);
      };
    }
    """
    fixture = MailFixture([page_of(signed_receipt("Unsupported browser must not call this signature verified."))])
    async with BrowserCase(browser, fixture, init_script=unsupported) as case:
        p = case.page
        await case.join()
        await expect(p.locator(".signature")).to_have_attribute("data-state", "unverified")
        await p.locator("#manage-key").click()
        await expect(p.locator("#generate-key")).to_be_disabled()
        await expect(p.locator("#signing-status")).to_contain_text("unavailable")
        await expect(p.locator("#signing-toggle")).not_to_be_checked()
        await p.locator("#close-key").click()
        post = await signed_send(case, "Ordinary chat still works without Ed25519 support.")
        assert post.get("signing") is None


HOLD_CRYPTO = """
window.__hold_crypto = '';
window.__held_crypto = false;
for (const operation of ['generateKey', 'importKey', 'verify', 'sign']) {
  const original = crypto.subtle[operation].bind(crypto.subtle);
  crypto.subtle[operation] = (...args) => {
    const result = original(...args);
    if (window.__hold_crypto === operation) {
      window.__held_crypto = true;
      return result.then(value => new Promise(resolve => {
        window.__release_crypto = () => resolve(value);
      }));
    }
    return result;
  };
}
"""


async def test_leaving_during_crypto_does_not_revive_secrets(browser):
    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode("ascii")
    for operation in ["generateKey", "importKey", "verify", "sign"]:
        signed = signed_receipt("A held verification belongs only to the old room.")
        fixture = MailFixture([page_of(signed)] if operation == "verify" else None)
        async with BrowserCase(browser, fixture, init_script=HOLD_CRYPTO) as case:
            p = case.page
            if operation == "verify":
                await p.evaluate("window.__hold_crypto = 'verify'")
                await case.join()
            else:
                await case.join()
                if operation == "sign":
                    await make_key(p)
                    await p.locator("#close-key").click()
                    await p.evaluate("window.__hold_crypto = 'sign'")
                    await p.locator("#message-input").fill("Leave while signing this draft.")
                    await p.locator("#send-button").click()
                else:
                    await p.locator("#manage-key").click()
                    await p.evaluate("operation => window.__hold_crypto = operation", operation)
                    if operation == "generateKey":
                        await p.locator("#generate-key").click()
                    else:
                        await p.locator("#key-import-text").fill(pem)
                        await p.locator("#import-key").click()
            await p.wait_for_function("() => window.__held_crypto && typeof window.__release_crypto === 'function'")
            if operation in ["generateKey", "importKey"]:
                await p.locator("#close-key").click()
            await p.locator("#leave-room").click()
            fixture.read_pages.append(page_of(receipt("Only the new room should survive."), cursor="new-crypto-room"))
            await case.join(TOKEN_B)
            await p.evaluate("window.__hold_crypto = ''; window.__release_crypto()")
            await expect(p.locator("article.message")).to_have_count(1)
            assert "old room" not in await p.locator("#messages").inner_text()
            await p.locator("#manage-key").click()
            await expect(p.locator("#public-key")).to_have_value("")
            await expect(p.locator("#key-import-text")).to_have_value("")
            await expect(p.locator("#signing-toggle")).not_to_be_checked()
            assert not fixture.sends(), f"A stale {operation} operation sent to another room"


async def assert_layout(page):
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Chat overflows horizontally"
    viewport = page.viewport_size
    composer = await page.locator("#message-input").bounding_box()
    assert composer and composer["y"] >= 0 and composer["y"] + composer["height"] <= viewport["height"], "Composer escaped viewport"
    message_area = await page.locator("#messages").bounding_box()
    assert message_area and message_area["height"] >= 60, "Controls left no usable message area"


async def test_screenshots(browser):
    inventory = signed_receipt("The lunar cheese inventory is ready. All 47 wheels accounted for. 🧀",
                               "lunar-mailbot", message_type="result")
    dispatch = receipt("Excellent. Please deliver one to the secret picnic.", "captain-whisper",
                       message_type="task", reply_to=inventory["id"])
    fixture = MailFixture([page_of(
        inventory,
        dispatch,
        receipt("Parcel dispatched. The delivery pigeon has signed the paperwork. 🕊️", "postmaster-bot",
                message_type="result", reply_to=dispatch["id"]),
        receipt("This is a demo conversation. Messages disappear two hours after posting.", None),
    )], status_pages=[{"status": status_receipt("Secret picnic: assembling", "yellow")}])
    async with BrowserCase(browser, fixture, viewport={"width": 1280, "height": 1000}) as case:
        await case.join(username="captain-whisper")
        await expect(case.page.locator("article.message")).to_have_count(4)
        await expect(case.page.locator("#room-status-text")).to_have_text("Secret picnic: assembling")
        await choose_reply(case.page, dispatch)
        await case.page.locator("#message-type").select_option("question")
        await case.page.locator("#message-input").fill("One tiny note, on its way…")
        assert await case.page.locator("#active-token").get_attribute("type") == "password"
        await expect(case.page.locator(".signature")).to_have_attribute("data-state", "verified")
        await make_key(case.page)
        await case.page.locator("#close-key").click()
        await expect(case.page.locator("#toast")).not_to_be_visible()
        await case.page.wait_for_timeout(250)
        await assert_layout(case.page)
        await case.page.screenshot(path="/tmp/psstoffice-chat-desktop.png", full_page=True)
        await case.page.set_viewport_size({"width": 390, "height": 844})
        await assert_layout(case.page)
        await case.page.screenshot(path="/tmp/psstoffice-chat-mobile.png", full_page=True)
        await case.page.locator("#manage-key").click()
        await expect(case.page.locator("#key-dialog")).to_be_visible()
        assert await case.page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await case.page.screenshot(path="/tmp/psstoffice-chat-key-mobile.png", full_page=True)
        await case.page.locator("#close-key").click()
        await case.page.set_viewport_size({"width": 844, "height": 390})
        await assert_layout(case.page)
        await case.page.screenshot(path="/tmp/psstoffice-chat-landscape.png", full_page=True)
        await case.page.set_viewport_size({"width": 390, "height": 320})
        await assert_layout(case.page)
        await case.page.screenshot(path="/tmp/psstoffice-chat-short-mobile.png", full_page=True)


async def open_files(case):
    await case.page.locator("#open-files").click()
    await expect(case.page.locator("#files-dialog")).to_be_visible()
    await wait_until(lambda: bool(case.fixture.file_reads()))


async def choose_file(page, data, name="secret.bin", mime="application/octet-stream"):
    await page.locator("#file-input").set_input_files({
        "name": name, "mimeType": mime, "buffer": data,
    })


FILE_BLOBS = """
window.__file_blobs = [];
const originalObjectURL = URL.createObjectURL.bind(URL);
URL.createObjectURL = blob => {
  window.__file_blobs.push({type: blob.type, size: blob.size});
  return originalObjectURL(blob);
};
"""


CHUNKED_DOWNLOAD = """
const originalChunkFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const response = await originalChunkFetch(...args);
  if (String(args[0]).endsWith('/v1/files/download')) {
    const headers = new Headers(response.headers);
    headers.delete('Content-Length');
    return new Response(response.body, {status: response.status, headers});
  }
  return response;
};
"""


async def test_files_hidden_pagination_and_polling(browser):
    first = file_receipt(b"one", "first.txt")
    second = file_receipt(b"two", "second.txt")
    fixture = MailFixture(file_pages=[
        files_page(first, cursor="first-file-page", has_more=True),
        files_page(second, cursor="second-file-page"),
    ])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        await expect(p.locator("#files-dialog")).not_to_be_visible()
        await p.clock.fast_forward(45_000)
        assert not fixture.file_reads(), "Closed file browser must not fetch or poll file metadata"
        await open_files(case)
        await expect(p.locator("article.file-item")).to_have_count(2)
        assert fixture.file_reads()[0]["body"].get("after") is None
        assert fixture.file_reads()[1]["body"]["after"] == "first-file-page"
        assert all(r["body"]["limit"] <= 100 for r in fixture.file_reads())
        assert not fixture.downloads(), "Listing files must not fetch file contents"
        fixture.file_metadata[TOKEN_A] = {first["id"]: first, second["id"]: second}
        before = len(fixture.file_reads())
        await p.clock.fast_forward(14_000)
        assert len(fixture.file_reads()) == before, "File browser polled faster than its 15-second interval"
        await p.clock.fast_forward(1_100)
        await wait_until(lambda: len(fixture.file_reads()) == before + 1)
        await p.locator("#close-files").click()
        count = len(fixture.file_reads())
        await p.clock.fast_forward(60_000)
        assert len(fixture.file_reads()) == count, "Closing file browser must stop polling"


async def test_file_upload_receipt_survives_older_list_response(browser):
    held = asyncio.get_running_loop().create_future()
    async def old_empty_page(body):
        return await held
    fixture = MailFixture(file_pages=[old_empty_page])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await open_files(case)
        await choose_file(p, b"A newer upload beats an earlier empty listing", "newer.txt")
        await p.locator("#upload-file").click()
        await expect(p.locator("article.file-item")).to_have_count(1)
        held.set_result(EMPTY_FILES.copy())
        await expect(p.locator("#files-status")).to_contain_text("live file")
        await expect(p.locator("article.file-item")).to_have_count(1)
        assert "newer.txt" in await p.locator("#file-list").inner_text(), "An older file listing hid a newly acknowledged upload"
        await choose_file(p, b"The room still waits for its next slot", "second.txt")
        await expect(p.locator("#upload-file")).to_be_disabled()
        assert len(fixture.uploads()) == 1


async def test_file_upload_bounds_any_type_and_local_privacy(browser):
    # Active file formats are transferred as inert bytes, not parsed by the UI.
    samples = [
        (bytes(range(256)) * 3906 + bytes(range(64)), "million.bin", "application/octet-stream"),
        (b"", "empty.txt", "text/plain"),
        (b'<script>window.__file_executed=true</script>\x00\xff', "postcard.html", "text/html"),
    ]
    assert len(samples[0][0]) == 1_000_000
    for data, filename, mime in samples:
        async with BrowserCase(browser) as case:
            p, fixture = case.page, case.fixture
            await case.join(username="sender \u2603\ufe0f")
            await open_files(case)
            assert await p.locator("#file-input").get_attribute("accept") is None, "Any file type should be supported"
            await choose_file(p, b"x" * 1_000_001, "oversize.bin")
            if await p.locator("#upload-file").is_enabled():
                await p.locator("#upload-file").click()
            assert not fixture.uploads(), "Oversized files must be rejected before a network upload"
            await choose_file(p, data, filename, mime)
            await p.locator("#upload-file").click()
            await wait_until(lambda: len(fixture.uploads()) == 1)
            await expect(p.locator("#file-pending")).not_to_be_visible()
            post = fixture.uploads()[0]
            assert set(post["body"]) == {"filename", "data_base64", "username", "client_file_id"}
            assert post["body"]["filename"] == filename and post["body"]["username"] == "sender \u2603\ufe0f"
            assert base64.b64decode(post["body"]["data_base64"], validate=True) == data
            assert post["body"]["data_base64"] == base64.b64encode(data).decode("ascii")
            assert re.fullmatch(r"[0-9a-f]{32}", post["body"]["client_file_id"])
            assert post["headers"]["x-room-token"] == TOKEN_A
            assert await p.evaluate("window.__file_executed === undefined")
            assert await p.locator("#file-list iframe, #file-list img, #file-list object, #file-list script").count() == 0
            assert await p.evaluate("({local: localStorage.length, session: sessionStorage.length})") == {"local": 0, "session": 0}
            assert await p.evaluate("indexedDB.databases()") == []
            assert TOKEN_A not in p.url and filename not in p.url
            for request in await p.evaluate("window.__fetch_checks"):
                assert request["credentials"] == "omit" and request["redirect"] == "error"
                assert request["cache"] == "no-store" and request["referrerPolicy"] == "no-referrer"
            await p.locator("#close-files").click()
            await p.locator("#message-input").fill("File uploads leave ordinary chat usable.")
            await p.locator("#send-button").click()
            await expect(p.locator("#message-input")).to_have_value("")


async def test_file_names_render_as_text_and_invalid_names(browser):
    malicious = '<img src=x onerror="window.__file_xss=true">.html'
    one = file_receipt(b"one", malicious, '<svg onload="window.__file_xss=true">')
    two = file_receipt(b"two", "\u90ae\u4fbf \U0001f426.txt", None)
    fixture = MailFixture(file_pages=[files_page(one, two)])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await open_files(case)
        await expect(p.locator("article.file-item")).to_have_count(2)
        assert malicious in await p.locator("#file-list").inner_text()
        assert "\u90ae\u4fbf \U0001f426.txt" in await p.locator("#file-list").inner_text()
        assert await p.locator("#file-list img, #file-list svg, #file-list script").count() == 0
        assert await p.evaluate("window.__file_xss === undefined")
        await choose_file(p, b"name check", "subfolder\\unsafe\x01.txt")
        if await p.locator("#upload-file").is_enabled():
            await p.locator("#upload-file").click()
            await wait_until(lambda: bool(fixture.uploads()))
            transmitted = fixture.uploads()[0]["body"]["filename"]
            assert not re.search(r"[\\/\x00-\x1f\x7f]", transmitted), "File names must be sanitized before upload"
        else:
            assert not fixture.uploads(), "Invalid local names should not be uploaded unchanged"


async def test_file_cooldown_is_per_room_and_retry_after(browser):
    cooldown = datetime.now(timezone.utc) + timedelta(seconds=30)
    limited = (429, {"error": {"code": "file_upload_limited", "message": TOKEN_A + " private bytes"}}, {"Retry-After": "30"})
    fixture = MailFixture(upload_results=[limited])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        await open_files(case)
        await choose_file(p, b"room A parcel")
        await p.locator("#upload-file").click()
        await wait_until(lambda: len(fixture.uploads()) == 1)
        await expect(p.locator("#retry-file")).to_be_visible()
        await expect(p.locator("#retry-file")).to_be_disabled()
        assert TOKEN_A not in await p.locator("#files-dialog").inner_text(), "Errors must not echo server-provided secret text"
        await p.clock.fast_forward(29_000)
        assert len(fixture.uploads()) == 1, "A quota failure must not auto-retry"
        await expect(p.locator("#retry-file")).to_be_disabled()
        await p.clock.fast_forward(1_100)
        await expect(p.locator("#retry-file")).to_be_enabled()
        await p.locator("#close-files").click()
        await p.locator("#leave-room").click()
        fixture.file_pages.append(files_page(next_upload_at=None))
        await case.join(TOKEN_B)
        await open_files(case)
        await choose_file(p, b"room B does not share room A quota")
        await expect(p.locator("#upload-file")).to_be_enabled()
        await p.locator("#upload-file").click()
        await wait_until(lambda: len(fixture.uploads()) == 2)
        assert fixture.uploads()[1]["headers"]["x-room-token"] == TOKEN_B
        assert fixture.uploads()[1]["body"]["client_file_id"] != fixture.uploads()[0]["body"]["client_file_id"]
    # An advertised room cooldown also prevents wasting another upload.
    fixture = MailFixture(file_pages=[files_page(next_upload_at=cooldown.isoformat())])
    async with BrowserCase(browser, fixture) as case:
        await case.join()
        await open_files(case)
        await choose_file(case.page, b"Wait for this room")
        await expect(case.page.locator("#upload-file")).to_be_disabled()
        assert not fixture.uploads()


async def test_file_ambiguous_retry_preserves_payload_and_chat(browser):
    fixture = MailFixture(upload_results=["abort"])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await open_files(case)
        data = b"An uncertain synthetic file \x00\xff"
        await choose_file(p, data, "uncertain.bin")
        await p.locator("#upload-file").click()
        await expect(p.locator("#retry-file")).to_be_visible()
        first = fixture.uploads()[0]
        await expect(p.locator("#file-input")).to_be_disabled()
        await expect(p.locator("#upload-file")).to_be_disabled()
        await p.locator("#close-files").click()
        await expect(p.locator("#message-input")).to_be_editable()
        await p.locator("#active-username").fill("new-chat-name")
        await p.locator("#message-input").fill("Keep chatting while a file receipt is uncertain.")
        await p.locator("#send-button").click()
        await expect(p.locator("#message-input")).to_have_value("")
        await p.locator("#open-files").click()
        await p.locator("#retry-file").click()
        await wait_until(lambda: len(fixture.uploads()) == 2)
        assert fixture.uploads()[1]["body"] == first["body"], "File retry must preserve name, bytes, original username and id"
        await expect(p.locator("#file-pending")).not_to_be_visible()


async def test_file_wrong_receipt_and_retry_lifetime(browser):
    def wrong_receipt(body):
        value = file_receipt(b"different content", body["filename"], body.get("username"))
        return (201, value, {})
    fixture = MailFixture(upload_results=[wrong_receipt])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        await open_files(case)
        await choose_file(p, b"Expected content", "expected.bin")
        await p.locator("#upload-file").click()
        await expect(p.locator("#retry-file")).to_be_visible()
        await expect(p.locator("#file-pending")).to_be_visible()
        await expect(p.locator("#file-input")).to_be_disabled()
        await p.clock.fast_forward(7_200_001)
        await expect(p.locator("#retry-file")).to_be_disabled()
        await expect(p.locator("#discard-file")).to_be_visible()
        assert len(fixture.uploads()) == 1, "Expired upload retry must not be sent automatically"
        await p.locator("#discard-file").click()
        await expect(p.locator("#file-pending")).not_to_be_visible()
        await expect(p.locator("#file-input")).to_be_enabled()


async def test_file_download_is_explicit_inert_and_verified(browser):
    data = b'<html><script>window.__download_executed=true</script></html>\x00\xff'
    metadata = file_receipt(data, "letter.html", "mailbot")
    fixture = MailFixture(file_pages=[files_page(metadata)], download_results=[
        (200, data, {"Content-Disposition": 'inline; filename="attacker.js"'})])
    async with BrowserCase(browser, fixture, init_script=FILE_BLOBS) as case:
        p = case.page
        await case.join()
        await open_files(case)
        await expect(p.locator("article.file-item")).to_have_count(1)
        assert not fixture.downloads(), "File content must only be fetched on an explicit download"
        async with p.expect_download() as download_info:
            await p.locator(".file-download").click()
        download = await download_info.value
        assert download.suggested_filename == "letter.html"
        saved = Path(await download.path()).read_bytes()
        assert saved == data and hashlib.sha256(saved).hexdigest() == metadata["sha256"]
        assert fixture.downloads()[0]["body"] == {"file_id": metadata["id"]}
        blobs = await p.evaluate("window.__file_blobs")
        assert blobs[-1] == {"type": "application/octet-stream", "size": len(data)}, "Save every file as inert binary, regardless of original type"
        assert await p.evaluate("window.__download_executed === undefined")
        assert TOKEN_A not in p.url and p.url.startswith("file://")
        assert await p.locator("iframe, object, embed").count() == 0


async def test_file_download_rejects_tampered_oversized_or_wrong_type(browser):
    metadata = file_receipt(b"verified", "trusted.bin")
    responses = [
        (200, b"tampered", {}),
        (200, b"x" * 1_000_001, {}),
        (200, {"error": {"message": TOKEN_A + " secret file data"}}, {}),
    ]
    for response in responses:
        fixture = MailFixture(file_pages=[files_page(metadata)], download_results=[response])
        async with BrowserCase(browser, fixture, init_script=FILE_BLOBS + CHUNKED_DOWNLOAD) as case:
            p = case.page
            downloads = []
            p.on("download", lambda value: downloads.append(value))
            await case.join()
            await open_files(case)
            await p.locator(".file-download").click()
            await expect(p.locator("#files-error")).not_to_have_text("")
            await asyncio.sleep(0.1)
            assert not downloads and not await p.evaluate("window.__file_blobs"), "Invalid bytes must never be saved"
            assert TOKEN_A not in await p.locator("#files-dialog").inner_text()


async def test_file_browser_rejects_malformed_metadata(browser):
    cases = [
        {"filename": "../unsafe.txt"},
        {"filename": "hidden\u202eexe.txt"},
        {"size_bytes": 500_000_001},
        {"size_bytes": True},
        {"sha256": "A" * 64},
        {"extra_field": "untrusted"},
        {"expires_at": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()},
    ]
    for changes in cases:
        metadata = {**file_receipt(b"metadata"), **changes}
        fixture = MailFixture(file_pages=[files_page(metadata)])
        async with BrowserCase(browser, fixture) as case:
            await case.join()
            await open_files(case)
            await expect(case.page.locator("#files-status")).to_contain_text("unexpected")
            assert await case.page.locator("article.file-item").count() == 0
            assert not fixture.downloads(), "Malformed file metadata must never create a download control"


async def test_file_expiry_is_local_and_prevents_download(browser):
    metadata = file_receipt(b"short-lived", "soon.txt", expires=datetime.now(timezone.utc) + timedelta(seconds=2))
    fixture = MailFixture(file_pages=[files_page(metadata)])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        await open_files(case)
        await expect(p.locator("article.file-item")).to_have_count(1)
        await p.clock.fast_forward(2_100)
        await expect(p.locator("article.file-item")).to_have_count(0)
        assert not fixture.downloads()
        await p.clock.fast_forward(15_000)
        await expect(p.locator("article.file-item")).to_have_count(0)


HOLD_FILE_CRYPTO = """
window.__hold_file = '';
window.__held_file = false;
for (const [owner, operation] of [[File.prototype, 'arrayBuffer'], [crypto.subtle, 'digest']]) {
  const original = owner[operation];
  owner[operation] = function (...args) {
    const result = original.apply(this, args);
    if (window.__hold_file === operation) {
      window.__held_file = true;
      return result.then(value => new Promise(resolve => {
        window.__release_file = () => resolve(value);
      }));
    }
    return result;
  };
}
"""


async def test_leaving_during_file_preparation_does_not_send(browser):
    for operation in ["arrayBuffer", "digest"]:
        async with BrowserCase(browser, init_script=HOLD_FILE_CRYPTO) as case:
            p = case.page
            await case.join()
            await open_files(case)
            await choose_file(p, b"Only old room owns these bytes", "old-room.bin")
            await p.evaluate("operation => window.__hold_file = operation", operation)
            await p.locator("#upload-file").click()
            await p.wait_for_function("() => window.__held_file && typeof window.__release_file === 'function'")
            await p.locator("#close-files").click()
            await p.locator("#leave-room").click()
            await case.join(TOKEN_B)
            await p.evaluate("window.__hold_file = ''; window.__release_file()")
            await open_files(case)
            await expect(p.locator("#file-pending")).not_to_be_visible()
            assert await p.locator("#file-input").evaluate("input => input.files.length") == 0
            assert "old-room.bin" not in await p.locator("#files-dialog").inner_text()
            assert not case.fixture.uploads(), f"Stale {operation} preparation sent old file data into the new room"


async def test_leaving_during_download_verification_does_not_save(browser):
    data = b"Old room file held at its hash check"
    metadata = file_receipt(data, "held-hash.bin")
    fixture = MailFixture(file_pages=[files_page(metadata)], download_results=[(200, data, {})])
    async with BrowserCase(browser, fixture, init_script=HOLD_FILE_CRYPTO + FILE_BLOBS) as case:
        p = case.page
        downloads = []
        p.on("download", lambda value: downloads.append(value))
        await case.join()
        await open_files(case)
        await p.evaluate("window.__hold_file = 'digest'")
        await p.locator(".file-download").click()
        await p.wait_for_function("() => window.__held_file && typeof window.__release_file === 'function'")
        await p.locator("#close-files").click()
        await p.locator("#leave-room").click()
        await case.join(TOKEN_B)
        await p.evaluate("window.__hold_file = ''; window.__release_file()")
        await asyncio.sleep(0.15)
        assert not downloads and not await p.evaluate("window.__file_blobs"), "Stale file hash verification saved old room bytes"


async def test_leaving_during_file_network_does_not_revive_data(browser):
    for operation in ["upload", "list", "download"]:
        metadata = file_receipt(b"old secret bytes", "old-file.bin")
        held = asyncio.get_running_loop().create_future()
        async def hold_result(body):
            return await held
        fixture = MailFixture(
            file_pages=[hold_result] if operation == "list" else [files_page(metadata)],
            upload_results=[hold_result] if operation == "upload" else None,
            download_results=[hold_result] if operation == "download" else None,
        )
        async with BrowserCase(browser, fixture, init_script=FILE_BLOBS) as case:
            p = case.page
            downloads = []
            p.on("download", lambda value: downloads.append(value))
            await case.join()
            await open_files(case)
            if operation == "upload":
                await choose_file(p, b"old secret bytes", "old-file.bin")
                await p.locator("#upload-file").click()
                await wait_until(lambda: bool(fixture.uploads()))
            elif operation == "download":
                await expect(p.locator("article.file-item")).to_have_count(1)
                await p.locator(".file-download").click()
                await wait_until(lambda: bool(fixture.downloads()))
            await p.locator("#close-files").click()
            await p.locator("#leave-room").click()
            await case.join(TOKEN_B)
            new_metadata = file_receipt(b"new room", "new-room.txt")
            fixture.file_pages.append(files_page(new_metadata))
            await p.locator("#open-files").click()
            await expect(p.locator("article.file-item")).to_have_count(1)
            held.set_result((201, metadata, {}) if operation == "upload" else
                            files_page(metadata) if operation == "list" else
                            (200, b"old secret bytes", {}))
            await asyncio.sleep(0.15)
            assert "old-file.bin" not in await p.locator("#files-dialog").inner_text()
            assert "new-room.txt" in await p.locator("#files-dialog").inner_text()
            assert not downloads and not await p.evaluate("window.__file_blobs"), "Old room response triggered a file download"
            await expect(p.locator("#file-pending")).not_to_be_visible()


async def test_file_dialog_keyboard_and_responsive_layout(browser):
    metadata = file_receipt(b"A file for the secret picnic.", "picnic-instructions.txt", "captain-whisper")
    fixture = MailFixture(file_pages=[files_page(metadata)])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await open_files(case)
        assert await p.evaluate("document.querySelector('#files-dialog').contains(document.activeElement)"), "Dialog should receive keyboard focus"
        for viewport in [{"width": 1280, "height": 1000}, {"width": 390, "height": 844},
                         {"width": 844, "height": 390}, {"width": 390, "height": 320}]:
            await p.set_viewport_size(viewport)
            assert await p.evaluate("document.documentElement.scrollWidth <= innerWidth")
            dialog = await p.locator("#files-dialog").bounding_box()
            close = await p.locator("#close-files").bounding_box()
            assert dialog and dialog["x"] >= 0 and dialog["y"] >= 0
            assert dialog["x"] + dialog["width"] <= viewport["width"]
            assert dialog["y"] + dialog["height"] <= viewport["height"]
            assert close and 0 <= close["y"] and close["y"] + close["height"] <= viewport["height"]
        await p.set_viewport_size({"width": 1280, "height": 1000})
        await p.screenshot(path="/tmp/psstoffice-chat-files.png", full_page=True)
        await p.keyboard.press("Escape")
        await expect(p.locator("#files-dialog")).not_to_be_visible()
        await expect(p.locator("#open-files")).to_be_focused()
        await assert_layout(p)


async def open_status(case):
    await case.page.locator("#room-status").click()
    await expect(case.page.locator("#status-dialog")).to_be_visible()


async def set_status(case, text, color):
    await open_status(case)
    await case.page.locator("#status-input").fill(text)
    await case.page.locator(f'input[name="status-color"][value="{color}"]').check()
    await case.page.locator("#save-status").click()
    await expect(case.page.locator("#status-dialog")).not_to_be_visible()
    await expect(case.page.locator("#room-status-text")).to_have_text(text)
    await expect(case.page.locator("#room-status")).to_have_attribute("data-color", color)


async def test_room_status_unicode_colors_validation_and_privacy(browser):
    async with BrowserCase(browser) as case:
        p, fixture = case.page, case.fixture
        await case.join()
        await wait_until(lambda: bool(fixture.status_reads()))
        await expect(p.locator("#room-status")).to_have_attribute("data-color", "unset")
        assert not fixture.status_updates(), "Joining must not create or refresh a room status"
        await open_status(case)
        await p.locator("#status-input").fill("🦉" * 50)
        assert "50" in await p.locator("#status-count").inner_text()
        await expect(p.locator("#save-status")).to_be_enabled()
        for invalid in ["🦉" * 51, "   ", "A deceptive \u202estatus"]:
            await p.locator("#status-input").fill(invalid)
            if await p.locator("#save-status").is_enabled():
                await p.locator("#save-status").click()
            assert not fixture.status_updates(), "Invalid status reached the API"
            await expect(p.locator("#status-dialog")).to_be_visible()
        await p.locator("#close-status").click()
        colors = {}
        for color in ["green", "red", "yellow", "blue"]:
            text = "🦉" * 50 if color == "green" else f"Picnic status: {color}"
            await set_status(case, text, color)
            post = fixture.status_updates()[-1]
            assert post["body"] == {"text": text, "color": color}
            assert post["headers"]["x-room-token"] == TOKEN_A
            colors[color] = await p.locator("#room-status").evaluate("el => getComputedStyle(el).backgroundColor")
        assert len(set(colors.values())) == 4, "Status colors must be visually distinct"
        assert all(request["body"] == {} for request in fixture.status_reads()), "Reading status must not write or leak the token in JSON"
        stores = await p.evaluate("({local: localStorage.length, session: sessionStorage.length})")
        assert stores == {"local": 0, "session": 0}, "Room status must not persist secrets locally"
        assert TOKEN_A not in p.url
        for request in await p.evaluate("window.__fetch_checks"):
            if "/v1/status" in request["url"]:
                assert request["credentials"] == "omit" and request["redirect"] == "error"
                assert request["cache"] == "no-store" and request["referrerPolicy"] == "no-referrer"


async def test_room_status_plain_text_top_right_and_responsive_dialog(browser):
    malicious = '<img src=x onerror="window.__status_xss=true">'
    fixture = MailFixture(status_pages=[{"status": status_receipt(malicious, "red")}])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await expect(p.locator("#room-status-text")).to_have_text(malicious)
        assert await p.locator("#room-status img, #room-status script").count() == 0
        assert await p.evaluate("window.__status_xss === undefined")
        await set_status(case, "W" * 50, "blue")
        for viewport in [{"width": 1280, "height": 1000}, {"width": 390, "height": 844},
                         {"width": 844, "height": 390}, {"width": 390, "height": 320}]:
            await p.set_viewport_size(viewport)
            await assert_layout(p)
            indicator = await p.locator("#room-status").bounding_box()
            header = await p.locator(".chat-header").bounding_box()
            assert indicator and header and indicator["x"] + indicator["width"] / 2 >= header["x"] + header["width"] / 2
            assert header["x"] + header["width"] - indicator["x"] - indicator["width"] <= 25, "Room status must sit at the right edge of the chat header"
            assert indicator["x"] + indicator["width"] <= viewport["width"]
            assert indicator["y"] >= header["y"] and indicator["y"] + indicator["height"] <= header["y"] + header["height"]
            await open_status(case)
            assert await p.evaluate("document.querySelector('#status-dialog').contains(document.activeElement)")
            dialog = await p.locator("#status-dialog").bounding_box()
            close = await p.locator("#close-status").bounding_box()
            assert dialog and 0 <= dialog["x"] and 0 <= dialog["y"]
            assert dialog["x"] + dialog["width"] <= viewport["width"]
            assert dialog["y"] + dialog["height"] <= viewport["height"]
            assert close and 0 <= close["y"] and close["y"] + close["height"] <= viewport["height"]
            await p.keyboard.press("Escape")
            await expect(p.locator("#status-dialog")).not_to_be_visible()
            await expect(p.locator("#room-status")).to_be_focused()


async def test_room_status_local_expiry_and_reads_do_not_renew(browser):
    expiration = datetime.now(timezone.utc) + timedelta(seconds=2)
    current = status_receipt("This flag expires locally", "yellow", expires=expiration)
    fixture = MailFixture(status_pages=[{"status": current}])
    fixture.room_status[TOKEN_A] = current
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        await expect(p.locator("#room-status-text")).to_have_text(current["text"])
        await p.clock.fast_forward(2_100)
        await expect(p.locator("#room-status")).to_have_attribute("data-color", "unset")
        await p.clock.fast_forward(15_000)
        await wait_until(lambda: len(fixture.status_reads()) >= 2)
        await expect(p.locator("#room-status")).to_have_attribute("data-color", "unset")
        assert current["text"] not in await p.locator("#room-status-text").inner_text(), "Expired response revived the status"
        assert not fixture.status_updates(), "Reading or expiring a status renewed its deadline"


async def test_room_status_older_read_does_not_erase_own_write(browser):
    held = asyncio.get_running_loop().create_future()
    async def older_read(body):
        return await held
    fixture = MailFixture(status_pages=[older_read])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        await wait_until(lambda: bool(fixture.status_reads()))
        await set_status(case, "Newer confirmed room status", "green")
        held.set_result({"status": status_receipt("OLDER RESPONSE", "red")})
        await asyncio.sleep(0.15)
        await expect(p.locator("#room-status-text")).to_have_text("Newer confirmed room status")
        await expect(p.locator("#room-status")).to_have_attribute("data-color", "green")
        fixture.room_status[TOKEN_A] = status_receipt("Another agent changed the flag", "blue")
        await p.clock.fast_forward(15_100)
        await wait_until(lambda: len(fixture.status_reads()) >= 2)
        await expect(p.locator("#room-status-text")).to_have_text("Another agent changed the flag")
        assert len(fixture.status_updates()) == 1


async def test_room_status_stale_read_and_write_after_leaving(browser):
    for operation in ["read", "write"]:
        held = asyncio.get_running_loop().create_future()
        async def held_result(body):
            return await held
        old = status_receipt("Old room secret status", "red")
        new = status_receipt("New room independent status", "blue")
        fixture = MailFixture(
            status_pages=[held_result] if operation == "read" else [{"status": old}],
            status_results=[held_result] if operation == "write" else None)
        fixture.room_status[TOKEN_B] = new
        async with BrowserCase(browser, fixture) as case:
            p = case.page
            await case.join()
            await wait_until(lambda: bool(fixture.status_reads()))
            if operation == "write":
                await open_status(case)
                await p.locator("#status-input").fill(old["text"])
                await p.locator('input[name="status-color"][value="red"]').check()
                await p.locator("#save-status").click()
                await wait_until(lambda: bool(fixture.status_updates()))
                await p.locator("#close-status").click()
            await p.locator("#leave-room").click()
            assert await p.locator("#status-input").input_value() == "", "Leaving retained the old status draft"
            await case.join(TOKEN_B)
            await expect(p.locator("#room-status-text")).to_have_text(new["text"])
            held.set_result({"status": old} if operation == "read" else old)
            await asyncio.sleep(0.15)
            await expect(p.locator("#room-status-text")).to_have_text(new["text"])
            await expect(p.locator("#room-status")).to_have_attribute("data-color", "blue")
            assert "Old room secret status" not in await p.locator("#chat-view").inner_text()
            assert all(r["headers"]["x-room-token"] == TOKEN_B for r in fixture.status_reads()[1:])
            await p.locator("#leave-room").click()
            count = len(fixture.status_reads())
            await asyncio.sleep(0.2)
            assert len(fixture.status_reads()) == count, "Leaving did not stop status polling"


async def test_room_status_backoff_and_unconfirmed_write(browser):
    limited = (429, {"error": {"code": "client_busy", "message": "Synthetic quota"}}, {"Retry-After": "2"})
    initial = status_receipt("Initial room flag", "green")
    fixture = MailFixture(status_pages=[limited, {"status": initial}])
    def commit_then_abort(body):
        fixture.room_status[TOKEN_A] = status_receipt(body["text"], body["color"])
        return "abort"
    fixture.status_results.append(commit_then_abort)
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await wait_until(lambda: len(fixture.status_reads()) >= 2, timeout=8)
        delay = fixture.status_reads()[1]["time"] - fixture.status_reads()[0]["time"]
        assert delay >= 1.9, f"Status ignored Retry-After: {delay:.3f}s"
        await expect(p.locator("#room-status-text")).to_have_text(initial["text"])
        await open_status(case)
        await p.locator("#status-input").fill("Written but acknowledgement was lost")
        await p.locator('input[name="status-color"][value="yellow"]').check()
        await p.locator("#save-status").click()
        await expect(p.locator("#status-check")).to_be_visible()
        await expect(p.locator("#save-status")).to_be_disabled()
        await asyncio.sleep(0.25)
        assert len(fixture.status_updates()) == 1, "Ambiguous status write was automatically retried"
        await p.locator("#status-check").click()
        await expect(p.locator("#room-status-text")).to_have_text("Written but acknowledgement was lost")
        assert len(fixture.status_updates()) == 1, "Checking latest status rewrote it or extended expiry"


async def test_room_status_rejects_malformed_server_receipts(browser):
    valid = status_receipt("Safe room flag", "green")
    malformed = [
        {**valid, "text": "x" * 51},
        {**valid, "color": "url(javascript:alert(1))"},
        {**valid, "text": "Misleading \u202eflag"},
        {**valid, "id": "not-a-uuid"},
        {**valid, "expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()},
    ]
    for invalid in malformed:
        fixture = MailFixture(status_pages=[{"status": invalid}])
        async with BrowserCase(browser, fixture) as case:
            await case.join()
            await wait_until(lambda: bool(fixture.status_reads()))
            await asyncio.sleep(0.15)
            await expect(case.page.locator("#room-status")).to_have_attribute("data-color", "unset")
            assert invalid["text"] not in await case.page.locator("#room-status-text").inner_text()
            assert not fixture.status_updates()


async def test_room_status_write_and_manual_check_backoff_blocks_polling(browser):
    limited = (429, {"error": {"code": "client_busy", "message": "Synthetic quota"}}, {"Retry-After": "20"})
    for operation in ["write", "check"]:
        initial = status_receipt("Existing live status", "green")
        fixture = MailFixture(status_pages=[{"status": initial}],
                              status_results=[limited] if operation == "write" else None)
        fixture.room_status[TOKEN_A] = initial
        async with BrowserCase(browser, fixture) as case:
            p = case.page
            await p.clock.install()
            await case.join()
            await expect(p.locator("#room-status-text")).to_have_text(initial["text"])
            await open_status(case)
            if operation == "write":
                await p.locator("#status-input").fill("A paced replacement")
                await p.locator("#save-status").click()
                await wait_until(lambda: bool(fixture.status_updates()))
            else:
                fixture.status_pages.append(limited)
                await p.locator("#status-check").click()
                await wait_until(lambda: len(fixture.status_reads()) == 2)
            await expect(p.locator("#status-error")).to_contain_text("Wait")
            await expect(p.locator("#status-check")).to_be_disabled()
            count = len(fixture.status_reads())
            await p.clock.fast_forward(15_100)
            assert len(fixture.status_reads()) == count, "The periodic read bypassed a write/manual-check Retry-After deadline"
            await expect(p.locator("#save-status")).to_be_disabled()
            await p.clock.fast_forward(5_100)
            await wait_until(lambda: len(fixture.status_reads()) > count)
            assert len(fixture.status_updates()) == (1 if operation == "write" else 0), "Backoff retried a status write automatically"


def message_row(page, value):
    return page.locator(f'article.message[data-message-id="{value["id"]}"]')


async def choose_reply(page, value):
    await message_row(page, value).locator(".message-reply").click()
    await expect(page.locator("#reply-target")).to_be_visible()


async def test_reply_types_plain_text_and_parent_navigation(browser):
    malicious = '<img src=x onerror="window.__reply_xss=true"><script>bad()</script>'
    parent = receipt(malicious, '<svg onload="window.__reply_xss=true">', message_type="question")
    child = receipt("The safe answer is cheese.", "answer-agent", message_type="result", reply_to=parent["id"])
    fixture = MailFixture([page_of(parent, child)])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await expect(p.locator("article.message")).to_have_count(2)
        await expect(message_row(p, parent).locator(".message-type")).to_have_attribute("data-type", "question")
        await expect(message_row(p, child).locator(".message-type")).to_have_attribute("data-type", "result")
        reference = message_row(p, child).locator(".reply-reference")
        await expect(reference).to_have_attribute("data-reply-to", parent["id"])
        child_group = await message_row(p, child).get_attribute("data-chatter-id")
        await p.locator(f'.chatter-button[data-chatter-id={json.dumps(child_group)}]').click()
        await expect(message_row(p, parent)).not_to_be_visible()
        await reference.click()
        await expect(message_row(p, parent)).to_be_visible()
        await expect(message_row(p, parent)).to_be_focused()
        await expect(p.locator('.chatter-button[data-chatter-id="all"]')).to_have_attribute("aria-pressed", "true")
        await choose_reply(p, parent)
        assert malicious[:40] in await p.locator("#reply-target-text").inner_text()
        assert await p.locator("#reply-target img, #reply-target svg, #reply-target script").count() == 0
        assert await p.evaluate("window.__reply_xss === undefined")
        await p.locator("#message-type").select_option("task")
        await signed_send(case, "A task explicitly answering this note.")
        first = fixture.sends()[0]["body"]
        assert first["message_type"] == "task" and first["reply_to"] == parent["id"]
        await expect(p.locator("#reply-target")).not_to_be_visible()
        await expect(p.locator("#message-type")).to_have_value("")
        for kind in ("result", "question", "error"):
            await p.locator("#message-type").select_option(kind)
            await signed_send(case, f"A typed {kind} note.")
            assert fixture.sends()[-1]["body"]["message_type"] == kind
            assert fixture.sends()[-1]["body"].get("reply_to") is None
        await signed_send(case, "A plain note preserves the original API shape.")
        plain = fixture.sends()[-1]["body"]
        assert "message_type" not in plain and "reply_to" not in plain
        await expect(p.locator("article.message")).to_have_count(7)
        assert "message_type" not in fixture.append_receipts[-1] and "reply_to" not in fixture.append_receipts[-1]
        assert await p.locator("#messages img, #messages script").count() == 0


async def test_reply_parent_expiry_preserves_child_and_blocks_new_draft(browser):
    parent = receipt("A briefly available parent quote.", "short-lived-parent",
                     expires=datetime.now(timezone.utc) + timedelta(seconds=12), message_type="task")
    child = receipt("This child keeps its own two-hour deadline.", "long-lived-child",
                    message_type="result", reply_to=parent["id"])
    fixture = MailFixture([page_of(parent, child)])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        await expect(p.locator("article.message")).to_have_count(2)
        await choose_reply(p, parent)
        await p.locator("#message-input").fill("This draft must not silently lose its reply target.")
        await p.locator("#message-type").select_option("question")
        await expect(p.locator("#send-button")).to_be_enabled()
        await p.clock.fast_forward(13_000)
        await expect(message_row(p, parent)).not_to_be_attached()
        await expect(message_row(p, child)).to_be_visible()
        reference = message_row(p, child).locator(".reply-reference")
        await expect(reference).to_be_disabled()
        assert parent["id"] in await reference.inner_text()
        assert parent["message"] not in await reference.inner_text(), "An expired parent's quoted text survived"
        await expect(p.locator("#reply-target")).to_be_visible()
        await expect(p.locator("#send-button")).to_be_disabled()
        assert parent["message"] not in await p.locator("#reply-target-text").inner_text()
        assert parent["id"] in await p.locator("#reply-target-text").inner_text()
        assert not fixture.sends()
        await p.locator("#cancel-reply").click()
        await expect(p.locator("#reply-target")).not_to_be_visible()
        await expect(p.locator("#send-button")).to_be_enabled()
        await expect(message_row(p, child).locator(".message-expiry")).to_contain_text("2h")
        await p.clock.fast_forward(7_200_001)
        await expect(message_row(p, child)).not_to_be_attached()


async def test_reply_trimmed_parent_has_honest_reference(browser):
    parent = receipt("Trimmed parent text must not remain quoted.", "older-agent")
    middle = [receipt(f"History filler {number}.", "filler-agent") for number in range(298)]
    child = receipt("This reply survives the local history trim.", "child-agent", reply_to=parent["id"])
    fixture = MailFixture([
        page_of(parent, *middle[:99], cursor="reply-trim-one", has_more=True),
        page_of(*middle[99:199], cursor="reply-trim-two", has_more=True),
        page_of(*middle[199:], child, cursor="reply-trim-three"),
    ])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await expect(p.locator("article.message")).to_have_count(300)
        await choose_reply(p, parent)
        await p.locator("#message-input").fill("Keep the target ID after local trim.")
        await wait_until(lambda: any(not future.done() for _, future in fixture.pending_reads))
        fixture.release(page_of(receipt("One new note trims the oldest."), cursor="reply-trim-four"))
        await expect(message_row(p, parent)).not_to_be_attached()
        reference = message_row(p, child).locator(".reply-reference")
        await expect(reference).to_be_disabled()
        assert parent["id"] in await reference.inner_text()
        assert parent["message"] not in await p.locator("#messages").inner_text()
        await expect(p.locator("#send-button")).to_be_disabled()
        assert parent["message"] not in await p.locator("#reply-target-text").inner_text()
        await p.locator("#cancel-reply").click()
        await expect(p.locator("#send-button")).to_be_enabled()


async def test_signed_structured_reply_unicode_interop(browser):
    parent = receipt("Which Unicode parcel should be delivered?", "request-agent", message_type="question")
    fixture = MailFixture([page_of(parent)])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join(username='snowman ☃️ "\\\t')
        public = await make_key(p)
        await p.locator("#close-key").click()
        await choose_reply(p, parent)
        await p.locator("#message-type").select_option("result")
        enriched = await signed_send(case, 'Answer "yes"; slash \\; controls \b\f\n\t\x00; emoji 🧀; lines \u2028\u2029.')
        assert enriched["message_type"] == "result" and enriched["reply_to"] == parent["id"]
        assert b64url(verify_post(enriched)) == public
        encoded = canonical_bytes(TOKEN_A, enriched["message"], enriched.get("username"),
                                  enriched["signing"]["nonce"], enriched["signing"]["signed_at"],
                                  message_type="result", reply_to=parent["id"])
        assert encoded.startswith(b"psstoffice-message-v2\n")
        assert json.loads(encoded.split(b"\n", 1)[1])["reply_to"] == parent["id"]
        await expect(p.locator("article.message.own .signature")).to_have_attribute("data-state", "verified")
        await p.locator("#message-type").select_option("task")
        typed = await signed_send(case, "A type alone also authenticates both metadata fields.")
        verify_post(typed)
        typed_json = json.loads(canonical_bytes(TOKEN_A, typed["message"], typed.get("username"),
                               typed["signing"]["nonce"], typed["signing"]["signed_at"],
                               message_type="task").split(b"\n", 1)[1])
        assert typed_json["message_type"] == "task" and typed_json["reply_to"] is None
        await choose_reply(p, parent)
        replied = await signed_send(case, "A reply alone remains authenticated.")
        verify_post(replied)
        assert replied.get("message_type") is None and replied["reply_to"] == parent["id"]
        plain = await signed_send(case, "A legacy plain signed note uses the unchanged v1 domain.")
        verify_post(plain)
        assert canonical_bytes(TOKEN_A, plain["message"], plain.get("username"),
                               plain["signing"]["nonce"], plain["signing"]["signed_at"]).startswith(
                                   b"agentboard-message-v1\n")


async def test_reply_signature_metadata_tampering_and_downgrade(browser):
    key = Ed25519PrivateKey.generate()
    parent = receipt("Parent for authentication checks.", "parent-agent")
    legacy = signed_receipt("Legacy signed plain text.", "trusted-agent", key=key)
    typed = signed_receipt("Authenticated type.", "trusted-agent", key=key, message_type="task")
    replied = signed_receipt("Authenticated reply.", "trusted-agent", key=key, reply_to=parent["id"])
    both = signed_receipt("Authenticated type and reply.", "trusted-agent", key=key,
                          message_type="result", reply_to=parent["id"])
    wrong_type = signed_receipt("Tampered type must be untrusted.", "trusted-agent", key=key,
                                message_type="task", reply_to=parent["id"])
    wrong_type["message_type"] = "error"
    wrong_reply = signed_receipt("Tampered reply must be untrusted.", "trusted-agent", key=key,
                                 message_type="result", reply_to=parent["id"])
    wrong_reply["reply_to"] = str(uuid.uuid4())
    stripped = signed_receipt("Stripped metadata must not downgrade to v1.", "trusted-agent", key=key,
                              message_type="result", reply_to=parent["id"])
    del stripped["message_type"]
    del stripped["reply_to"]
    added_type = signed_receipt("A v1 signature cannot acquire a type.", "trusted-agent", key=key)
    added_type["message_type"] = "question"
    added_reply = signed_receipt("A v1 signature cannot acquire a reply.", "trusted-agent", key=key)
    added_reply["reply_to"] = parent["id"]
    valid = [legacy, typed, replied, both]
    forged = [wrong_type, wrong_reply, stripped, added_type, added_reply]
    fixture = MailFixture([page_of(parent, *valid, *forged)])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await expect(p.locator("article.message")).to_have_count(10)
        for value in valid:
            await expect(message_row(p, value).locator(".signature")).to_have_attribute("data-state", "verified")
        trusted_group = await message_row(p, legacy).get_attribute("data-chatter-id")
        for value in forged:
            await expect(message_row(p, value).locator(".signature")).to_have_attribute("data-state", "invalid")
            assert trusted_group != await message_row(p, value).get_attribute("data-chatter-id")
        await p.locator(f'.chatter-button[data-chatter-id={json.dumps(trusted_group)}]').click()
        await expect(p.locator("article.message:visible")).to_have_count(4)


async def test_reply_retry_freezes_metadata_after_parent_expiry(browser):
    parent = receipt("A parent that expires while acknowledgement is lost.", "parent-agent",
                     expires=datetime.now(timezone.utc) + timedelta(seconds=12))
    original = {}

    def lose_reply(body):
        original["receipt"] = receipt(body["message"], body.get("username"), signing=body["signing"],
                                     message_type=body["message_type"], reply_to=body["reply_to"])
        return "abort"

    fixture = MailFixture([page_of(parent)], send_results=[lose_reply, lambda body: (201, original["receipt"], {})],
                          publish_appends=False)
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        public = await make_key(p)
        await p.locator("#close-key").click()
        await choose_reply(p, parent)
        await p.locator("#message-type").select_option("question")
        await p.locator("#message-input").fill("The immutable typed reply must survive an uncertain delivery.")
        await p.locator("#send-button").click()
        await expect(p.locator("#retry-send")).to_be_visible()
        first = fixture.sends()[0]["body"]
        verify_post(first)
        await expect(p.locator("#message-type")).to_be_disabled()
        await expect(p.locator("#cancel-reply")).to_be_disabled()
        await expect(p.locator("#message-input")).to_have_attribute("readonly", "")
        await p.clock.fast_forward(310_000)
        await expect(message_row(p, parent)).not_to_be_attached()
        await expect(p.locator("#retry-send")).to_be_enabled()
        await p.locator("#retry-send").click()
        await wait_until(lambda: len(fixture.sends()) == 2)
        second = fixture.sends()[1]["body"]
        assert b64url(verify_post(second)) == public
        for field in ("message", "username", "client_message_id", "message_type", "reply_to"):
            assert second[field] == first[field], f"Retry changed {field}"
        assert second["signing"]["nonce"] != first["signing"]["nonce"]
        assert second["signing"]["signed_at"] >= first["signing"]["signed_at"] + 300
        await expect(p.locator("#pending-notice")).not_to_be_visible()
        await expect(p.locator("#message-type")).to_have_value("")
        await expect(p.locator("#reply-target")).not_to_be_visible()
        # Advancing five minutes can time out older held long polls. Deliver
        # to every held synthetic request and queue the same server page for
        # the reader's next request; duplicate receipt IDs are harmless.
        delivery = page_of(original["receipt"], cursor="retry-reply-delivery")
        fixture.read_pages.append(delivery)
        for _, future in fixture.pending_reads:
            if not future.done():
                future.set_result(delivery)
        await p.clock.fast_forward(5_000)
        await expect(message_row(p, original["receipt"]).locator(".signature")).to_have_attribute("data-state", "verified")
        await expect(message_row(p, original["receipt"]).locator(".reply-reference")).to_be_disabled()
        assert original["receipt"]["expires_at"] != parent["expires_at"]


async def test_reply_acknowledgement_must_match_metadata(browser):
    for change in ("type", "target", "strip", "plain-added"):
        parent = receipt("A parent for exact acknowledgements.")

        def mismatch(body):
            value = receipt(body["message"], body.get("username"),
                            message_type=body.get("message_type"), reply_to=body.get("reply_to"))
            if change == "type":
                value["message_type"] = "error"
            elif change == "target":
                value["reply_to"] = str(uuid.uuid4())
            elif change == "strip":
                value.pop("message_type")
                value.pop("reply_to")
            else:
                value["message_type"] = "task"
            return (201, value, {})

        fixture = MailFixture([page_of(parent)], send_results=[mismatch], publish_appends=False)
        async with BrowserCase(browser, fixture) as case:
            p = case.page
            await case.join()
            if change != "plain-added":
                await choose_reply(p, parent)
                await p.locator("#message-type").select_option("result")
            draft = "Only the exact metadata can acknowledge this note."
            await p.locator("#message-input").fill(draft)
            await p.locator("#send-button").click()
            await expect(p.locator("#pending-notice")).to_be_visible()
            await expect(p.locator("#message-input")).to_have_value(draft)
            assert await p.locator("article.message.own").count() == 0
            assert len(fixture.sends()) == 1


async def test_reply_rejects_malformed_metadata_and_missing_parent_ack(browser):
    for field, invalid in (("message_type", "urgent"), ("message_type", 1),
                           ("message_type", ["task"]), ("reply_to", "not-a-uuid"),
                           ("reply_to", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"), ("reply_to", {"id": str(uuid.uuid4())})):
        bad = receipt("Invalid metadata must not be rendered.")
        bad[field] = invalid
        fixture = MailFixture([page_of(bad)])
        async with BrowserCase(browser, fixture) as case:
            await case.join()
            await expect(case.page.locator("#read-notice")).to_be_visible()
            assert await case.page.locator("article.message").count() == 0
            assert bad["message"] not in await case.page.locator("#messages").inner_text()
    parent = receipt("Server-side parent expiry can race the composer.")
    fixture = MailFixture([page_of(parent)], send_results=[(
        404, {"error": {"code": "reply_unavailable", "message": "The referenced message is unavailable."}}, {})])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await choose_reply(p, parent)
        await p.locator("#message-type").select_option("question")
        draft = "Keep my draft when its target disappears on the server."
        await p.locator("#message-input").fill(draft)
        await p.locator("#send-button").click()
        await expect(p.locator("#composer-error")).not_to_have_text("")
        await expect(p.locator("#pending-notice")).not_to_be_visible()
        await expect(p.locator("#message-input")).to_have_value(draft)
        await expect(p.locator("#cancel-reply")).to_be_enabled()
        assert len(fixture.sends()) == 1


async def test_reply_composer_responsive_and_leave_clears_metadata(browser):
    parent = receipt("A very long synthetic parent " + "🧀" * 120, "parent-agent" * 5, message_type="task")
    fixture = MailFixture([page_of(parent)], send_results=["abort"])
    async with BrowserCase(browser, fixture, viewport={"width": 390, "height": 844}) as case:
        p = case.page
        await case.join()
        await choose_reply(p, parent)
        await p.locator("#message-type").select_option("result")
        await p.locator("#message-input").fill("This typed reply stays reachable on a small screen.")
        for viewport in ({"width": 390, "height": 844}, {"width": 844, "height": 390}, {"width": 390, "height": 320}):
            await p.set_viewport_size(viewport)
            await assert_layout(p)
            assert await p.evaluate("document.documentElement.scrollWidth <= innerWidth")
            target = await p.locator("#reply-target").bounding_box()
            assert target and target["y"] + target["height"] <= viewport["height"]
            for selector in ("#send-button", "#message-type"):
                control = await p.locator(selector).bounding_box()
                assert control and control["y"] >= 0 and control["y"] + control["height"] <= viewport["height"], \
                    f"The active reply hid {selector} below the viewport"
        await p.locator("#send-button").click()
        await expect(p.locator("#pending-notice")).to_be_visible()
        await assert_layout(p)
        await p.locator("#leave-room").click()
        fixture.read_pages.append(page_of(receipt("A fresh room starts plain.", "fresh-agent"), cursor="fresh-reply-room"))
        await case.join(TOKEN_B)
        await expect(p.locator("#message-type")).to_have_value("")
        await expect(p.locator("#reply-target")).not_to_be_visible()
        await expect(p.locator("#pending-notice")).not_to_be_visible()
        assert parent["id"] not in await p.locator("#chat-view").inner_html()
        assert parent["message"] not in await p.locator("#chat-view").inner_text()
        await signed_send(case, "Plain delivery in the new room.")
        assert fixture.sends()[-1]["body"].get("reply_to") is None
        assert fixture.sends()[-1]["body"].get("message_type") is None
        assert fixture.sends()[-1]["headers"]["x-room-token"] == TOKEN_B


async def test_leaving_during_enriched_signing_does_not_revive_reply(browser):
    parent = receipt("An old room's reply target.", "old-room-agent")
    fixture = MailFixture([page_of(parent)])
    async with BrowserCase(browser, fixture, init_script=HOLD_CRYPTO) as case:
        p = case.page
        await case.join()
        await make_key(p)
        await p.locator("#close-key").click()
        await choose_reply(p, parent)
        await p.locator("#message-type").select_option("task")
        await p.locator("#message-input").fill("Never finish this old-room structured reply.")
        await p.evaluate("window.__hold_crypto = 'sign'")
        await p.locator("#send-button").click()
        await p.wait_for_function("() => window.__held_crypto && typeof window.__release_crypto === 'function'")
        await p.locator("#leave-room").click()
        fixture.read_pages.append(page_of(receipt("Only new-room notes survive."), cursor="new-enriched-room"))
        await case.join(TOKEN_B)
        await p.evaluate("window.__hold_crypto = ''; window.__release_crypto()")
        await expect(p.locator("article.message")).to_have_count(1)
        await expect(p.locator("#reply-target")).not_to_be_visible()
        await expect(p.locator("#message-type")).to_have_value("")
        assert not fixture.sends(), "An old-room sign completion sent a note after leaving"
        assert parent["id"] not in await p.locator("#chat-view").inner_html()
        assert parent["message"] not in await p.locator("#messages").inner_text()


async def test_room_tabs_add_duplicate_cancel_close_and_privacy(browser):
    note_a, note_b = receipt("Room A secret"), receipt("Room B secret")
    fixture = RoomFixture({TOKEN_A: MailFixture([page_of(note_a)]),
                           TOKEN_B: MailFixture([page_of(note_b)])})
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await expect(room_tab(p, 1)).to_have_attribute("aria-selected", "true")
        await p.locator("#message-input").fill("An unfinished Room A note")
        await p.locator("#add-room").click()
        await p.locator("#room-token").fill(TOKEN_B)
        await p.locator("#cancel-add-room").click()
        await expect(p.locator("#message-input")).to_have_value("An unfinished Room A note")
        await expect(p.locator('#room-tabs [role="tab"]')).to_have_count(1)
        await p.locator("#add-room").click()
        await p.keyboard.press("Escape")
        await expect(p.locator("#chat-view")).to_be_visible()
        await add_room(case)
        await expect(p.locator("#messages")).to_contain_text(note_b["message"])
        assert note_a["message"] not in await p.locator("#messages").inner_text()
        await add_room(case, TOKEN_A, username="Do not overwrite existing identity")
        await expect(p.locator('#room-tabs [role="tab"]')).to_have_count(2)
        await expect(room_tab(p, 1)).to_have_attribute("aria-selected", "true")
        await expect(p.locator("#active-username")).to_have_value("human-test")
        await expect(p.locator("#message-input")).to_have_value("An unfinished Room A note")
        assert TOKEN_A not in await p.locator("#room-tabs").inner_text()
        assert TOKEN_B not in await p.locator("#room-tabs").inner_text()
        assert TOKEN_A not in p.url and TOKEN_B not in p.url
        assert await p.evaluate("({local:localStorage.length,session:sessionStorage.length})") == {"local": 0, "session": 0}
        await p.get_by_role("button", name="Close Room 2", exact=True).click()
        await expect(p.locator('#room-tabs [role="tab"]')).to_have_count(1)
        await expect(p.locator("#message-input")).to_have_value("An unfinished Room A note")
        await p.get_by_role("button", name="Close Room 1", exact=True).click()
        await expect(p.locator("#join-view")).to_be_visible()
        await expect(p.locator("#room-token")).to_have_value("")
        assert await p.locator("article.message").count() == 0
        count = len(fixture.reads())
        await asyncio.sleep(0.2)
        assert len(fixture.reads()) == count, "Closing the last tab kept polling"
        fixture.rooms[TOKEN_A].read_pages.append(EMPTY_PAGE.copy())
        await case.join()
        await expect(p.locator("#message-input")).to_have_value("")
        assert note_a["message"] not in await p.locator("#messages").inner_text(), "Closed room history survived reopening"


async def test_room_tabs_independent_composer_identity_reply_and_chatter(browser):
    parent = receipt("A question only in A", "alpha", message_type="question")
    neighbor = receipt("A second chatter", "other-alpha")
    note_b = receipt("B is its own conversation", "bravo")
    fixture = RoomFixture({TOKEN_A: MailFixture([page_of(parent, neighbor)]),
                           TOKEN_B: MailFixture([page_of(note_b)])})
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join(username="alpha-self")
        await expect(p.locator("article.message")).to_have_count(2)
        group = await message_row(p, parent).get_attribute("data-chatter-id")
        await p.locator(f'.chatter-button[data-chatter-id={json.dumps(group)}]').click()
        await choose_reply(p, parent)
        await p.locator("#message-type").select_option("result")
        await p.locator("#message-input").fill("A draft with its own reply and type")
        public_a = await make_key(p)
        await p.locator("#close-key").click()
        await add_room(case, username="bravo-self")
        await expect(p.locator("article.message")).to_have_count(1)
        await expect(p.locator("#message-input")).to_have_value("")
        await expect(p.locator("#message-type")).to_have_value("")
        await expect(p.locator("#reply-target")).not_to_be_visible()
        await expect(p.locator('.chatter-button[data-chatter-id="all"]')).to_have_attribute("aria-pressed", "true")
        await expect(p.locator("#signing-toggle")).not_to_be_checked()
        await p.locator("#manage-key").click()
        await expect(p.locator("#public-key")).to_have_value("")
        await p.locator("#close-key").click()
        await p.locator("#message-input").fill("B draft stays in B")
        await p.locator("#message-type").select_option("task")
        await switch_room(p, 1)
        await expect(p.locator("#active-username")).to_have_value("alpha-self")
        await expect(p.locator("#message-input")).to_have_value("A draft with its own reply and type")
        await expect(p.locator("#message-type")).to_have_value("result")
        await expect(p.locator("#reply-target-text")).to_contain_text(parent["message"])
        await expect(p.locator(f'.chatter-button[data-chatter-id={json.dumps(group)}]')).to_have_attribute("aria-pressed", "true")
        await expect(p.locator("#signing-toggle")).to_be_checked()
        await p.locator("#manage-key").click()
        await expect(p.locator("#public-key")).to_have_value(public_a)
        await p.locator("#close-key").click()
        await p.locator("#send-button").click()
        await wait_until(lambda: len(fixture.rooms[TOKEN_A].sends()) == 1)
        post = fixture.rooms[TOKEN_A].sends()[0]["body"]
        verify_post(post, TOKEN_A)
        assert post["reply_to"] == parent["id"] and post["message_type"] == "result"
        assert post["username"] == "alpha-self"
        assert not fixture.rooms[TOKEN_B].sends(), "Switching sent an inactive room's draft"
        await switch_room(p, 2)
        await expect(p.locator("#message-input")).to_have_value("B draft stays in B")
        await expect(p.locator("#message-type")).to_have_value("task")
        await expect(p.locator("#active-username")).to_have_value("bravo-self")


async def test_room_tabs_restore_scroll_and_only_active_polls(browser):
    notes = [receipt(f"Long A history {index}: " + "letterbox " * 20) for index in range(45)]
    fixture = RoomFixture({TOKEN_A: MailFixture([page_of(*notes, cursor="a-position")]),
                           TOKEN_B: MailFixture([page_of(receipt("B history"), cursor="b-position")])})
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await expect(p.locator("article.message")).to_have_count(len(notes))
        await p.locator("#messages").evaluate("element => { element.scrollTop = 173; }")
        before = await p.locator("#messages").evaluate("element => element.scrollTop")
        assert before > 0
        await wait_until(lambda: bool(fixture.rooms[TOKEN_A].pending_reads))
        await add_room(case)
        await wait_until(lambda: bool(fixture.rooms[TOKEN_B].pending_reads))
        a_count = len(fixture.rooms[TOKEN_A].reads())
        fixture.rooms[TOKEN_A].release(page_of(receipt("A stale poll must not become B history"), cursor="stale-a"))
        await asyncio.sleep(0.15)
        assert len(fixture.rooms[TOKEN_A].reads()) == a_count, "An inactive room restarted its poll"
        assert "A stale poll" not in await p.locator("#messages").inner_text()
        await switch_room(p, 1)
        await expect(p.locator("article.message")).to_have_count(len(notes))
        restored = await p.locator("#messages").evaluate("element => element.scrollTop")
        assert abs(restored - before) <= 2, f"Room scroll position changed from {before} to {restored}"
        await wait_until(lambda: len(fixture.rooms[TOKEN_A].reads()) > a_count)
        assert fixture.rooms[TOKEN_A].reads()[-1]["body"].get("after") == "a-position", "Returning to a room lost its cursor"


async def test_room_tabs_uncertain_send_keeps_exact_room_retry(browser):
    fixture = RoomFixture({TOKEN_A: MailFixture(send_results=["abort"]), TOKEN_B: MailFixture()})
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await p.locator("#message-type").select_option("question")
        await p.locator("#message-input").fill("An uncertain A parcel")
        await p.locator("#send-button").click()
        await expect(p.locator("#retry-send")).to_be_visible()
        original = fixture.rooms[TOKEN_A].sends()[0]["body"]
        await add_room(case)
        await expect(p.locator("#pending-notice")).not_to_be_visible()
        await p.locator("#message-input").fill("B sends independently")
        await p.locator("#send-button").click()
        await expect(p.locator("#message-input")).to_have_value("")
        await switch_room(p, 1)
        await expect(p.locator("#retry-send")).to_be_visible()
        await p.locator("#retry-send").click()
        await wait_until(lambda: len(fixture.rooms[TOKEN_A].sends()) == 2)
        retried = fixture.rooms[TOKEN_A].sends()[1]
        assert retried["body"] == original, "Tab switch changed a retry's exact payload or ID"
        assert retried["headers"]["x-room-token"] == TOKEN_A
        assert len(fixture.rooms[TOKEN_B].sends()) == 1
        await expect(p.locator("#pending-notice")).not_to_be_visible()


async def test_room_tabs_crypto_completions_stay_stale_after_return(browser):
    for operation in ["generateKey", "sign"]:
        fixture = RoomFixture({TOKEN_A: MailFixture(), TOKEN_B: MailFixture()})
        async with BrowserCase(browser, fixture, init_script=HOLD_CRYPTO) as case:
            p = case.page
            await case.join()
            public = ""
            if operation == "sign":
                public = await make_key(p)
                await p.locator("#close-key").click()
                await p.locator("#message-input").fill("A signing operation interrupted by tabs")
                await p.evaluate("window.__hold_crypto = 'sign'")
                await p.locator("#send-button").click()
            else:
                await p.locator("#manage-key").click()
                await p.evaluate("window.__hold_crypto = 'generateKey'")
                await p.locator("#generate-key").click()
            await p.wait_for_function("() => window.__held_crypto && typeof window.__release_crypto === 'function'")
            if operation == "generateKey":
                await p.locator("#close-key").click()
            await add_room(case)
            await switch_room(p, 1)
            await p.evaluate("window.__hold_crypto = ''; window.__release_crypto()")
            await asyncio.sleep(0.15)
            assert not fixture.rooms[TOKEN_A].sends() and not fixture.rooms[TOKEN_B].sends(), "A stale crypto completion sent after A→B→A"
            if operation == "sign":
                await expect(p.locator("#retry-send")).to_be_visible()
                await p.locator("#retry-send").click()
                await wait_until(lambda: len(fixture.rooms[TOKEN_A].sends()) == 1)
                verify_post(fixture.rooms[TOKEN_A].sends()[0]["body"], TOKEN_A)
                await expect(p.locator("#pending-notice")).not_to_be_visible()
            await p.locator("#manage-key").click()
            await expect(p.locator("#public-key")).to_have_value(public)
            await p.locator("#close-key").click()


async def test_room_tabs_signed_retry_refresh_does_not_mutate_resumed_attempt(browser):
    fixture = RoomFixture({TOKEN_A: MailFixture(send_results=["abort"]), TOKEN_B: MailFixture()})
    async with BrowserCase(browser, fixture, init_script=HOLD_CRYPTO) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        public = await make_key(p)
        await p.locator("#close-key").click()
        await p.locator("#message-type").select_option("task")
        await p.locator("#message-input").fill("A retry remains immutable across suspended signing")
        await p.locator("#send-button").click()
        await expect(p.locator("#retry-send")).to_be_visible()
        original = fixture.rooms[TOKEN_A].sends()[0]["body"]
        await p.clock.fast_forward(310_000)
        await p.evaluate("window.__hold_crypto = 'sign'")
        await p.locator("#retry-send").click()
        await p.wait_for_function("() => window.__held_crypto && typeof window.__release_crypto === 'function'")
        await add_room(case)
        await switch_room(p, 1)
        await p.evaluate("window.__hold_crypto = ''; window.__release_crypto()")
        await asyncio.sleep(0.15)
        assert len(fixture.rooms[TOKEN_A].sends()) == 1, "Old retry signing published after A→B→A"
        await expect(p.locator("#retry-send")).to_be_visible()
        await p.locator("#retry-send").click()
        await wait_until(lambda: len(fixture.rooms[TOKEN_A].sends()) == 2)
        resumed = fixture.rooms[TOKEN_A].sends()[1]["body"]
        for field in ["message", "username", "client_message_id", "message_type"]:
            assert resumed[field] == original[field], "Held retry completion mutated the resumed attempt"
        assert resumed["signing"]["public_key"] == public
        assert resumed["signing"]["signed_at"] >= original["signing"]["signed_at"] + 300
        verify_post(resumed, TOKEN_A)


async def test_room_tabs_old_read_is_stale_even_after_return(browser):
    old = receipt("Old A read result should never render")
    fresh = receipt("Fresh A activation read")
    fixture = RoomFixture({TOKEN_A: MailFixture([]), TOKEN_B: MailFixture()})
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await wait_until(lambda: bool(fixture.rooms[TOKEN_A].pending_reads))
        await add_room(case)
        fixture.rooms[TOKEN_A].read_pages.append(page_of(fresh, cursor="fresh-activation"))
        await switch_room(p, 1)
        await expect(p.locator("#messages")).to_contain_text(fresh["message"])
        fixture.rooms[TOKEN_A].release(page_of(old, cursor="old-activation"))
        await asyncio.sleep(0.15)
        assert old["message"] not in await p.locator("#messages").inner_text(), "A→B→A revived an old read activation"
        await expect(p.locator("article.message")).to_have_count(1)


async def test_room_tabs_files_status_and_upload_retry_are_independent(browser):
    data_a, data_b = b"A file bytes", b"B file bytes"
    fixture = RoomFixture({TOKEN_A: MailFixture(upload_results=["abort"]), TOKEN_B: MailFixture()})
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        await set_status(case, "A secret picnic", "green")
        await open_files(case)
        await choose_file(p, data_a, "alpha.bin")
        await p.locator("#upload-file").click()
        await expect(p.locator("#retry-file")).to_be_visible()
        original = fixture.rooms[TOKEN_A].uploads()[0]["body"]
        await p.locator("#close-files").click()
        await add_room(case)
        await expect(p.locator("#room-status-text")).to_have_text("Set room status")
        await set_status(case, "B secret picnic", "blue")
        await open_files(case)
        await expect(p.locator("#file-pending")).not_to_be_visible()
        assert "alpha.bin" not in await p.locator("#files-dialog").inner_text()
        await choose_file(p, data_b, "bravo.bin")
        await p.locator("#upload-file").click()
        await expect(p.locator("article.file-item")).to_have_count(1)
        await choose_file(p, b"second B bytes", "bravo-second.bin")
        await expect(p.locator("#upload-file")).to_be_disabled()
        await p.locator("#close-files").click()
        await switch_room(p, 1)
        await expect(p.locator("#room-status-text")).to_have_text("A secret picnic")
        await expect(p.locator("#room-status")).to_have_attribute("data-color", "green")
        await open_files(case)
        await expect(p.locator("#retry-file")).to_be_visible()
        await p.locator("#retry-file").click()
        await wait_until(lambda: len(fixture.rooms[TOKEN_A].uploads()) == 2)
        assert fixture.rooms[TOKEN_A].uploads()[1]["body"] == original, "Tab switch changed file retry bytes or ID"
        await expect(p.locator("article.file-item")).to_have_count(1)
        assert "alpha.bin" in await p.locator("#file-list").inner_text()
        assert "bravo.bin" not in await p.locator("#file-list").inner_text()
        await p.locator("#close-files").click()
        await switch_room(p, 2)
        await expect(p.locator("#room-status-text")).to_have_text("B secret picnic")
        await expect(p.locator("#room-status")).to_have_attribute("data-color", "blue")
        await open_files(case)
        await expect(p.locator("#file-selection-name")).to_contain_text("bravo-second.bin")
        await expect(p.locator("#upload-file")).to_be_disabled()
        assert len(fixture.rooms[TOKEN_B].uploads()) == 1, "Room A retry bypassed or reset room B cooldown"


async def test_room_tabs_file_preparation_stays_stale_after_return(browser):
    for operation in ["arrayBuffer", "digest"]:
        fixture = RoomFixture({TOKEN_A: MailFixture(), TOKEN_B: MailFixture()})
        async with BrowserCase(browser, fixture, init_script=HOLD_FILE_CRYPTO) as case:
            p = case.page
            await case.join()
            await open_files(case)
            await choose_file(p, b"File prepared in an old activation", "old-activation.bin")
            await p.evaluate("operation => window.__hold_file = operation", operation)
            await p.locator("#upload-file").click()
            await p.wait_for_function("() => window.__held_file && typeof window.__release_file === 'function'")
            await p.locator("#close-files").click()
            await add_room(case)
            await switch_room(p, 1)
            await p.evaluate("window.__hold_file = ''; window.__release_file()")
            await asyncio.sleep(0.15)
            assert not fixture.rooms[TOKEN_A].uploads() and not fixture.rooms[TOKEN_B].uploads(), "A→B→A revived old file preparation"
            await open_files(case)
            await expect(p.locator("#upload-file")).to_be_enabled()


async def test_room_tabs_old_file_network_results_stay_stale_after_return(browser):
    data = b"An old activation's file bytes"
    old_metadata = file_receipt(data, "old-network.bin")
    fresh_metadata = file_receipt(b"Fresh file list", "fresh-network.bin")
    for operation in ["upload", "list", "download"]:
        held = asyncio.get_running_loop().create_future()

        async def hold_result(body):
            return await held

        room_a = MailFixture(file_pages=[hold_result] if operation == "list" else [files_page(old_metadata)],
            upload_results=[hold_result] if operation == "upload" else None,
            download_results=[hold_result] if operation == "download" else None)
        fixture = RoomFixture({TOKEN_A: room_a, TOKEN_B: MailFixture()})
        async with BrowserCase(browser, fixture, init_script=FILE_BLOBS) as case:
            p = case.page
            downloads = []
            p.on("download", lambda value: downloads.append(value))
            await case.join()
            await open_files(case)
            if operation == "upload":
                await choose_file(p, data, "old-network.bin")
                await p.locator("#upload-file").click()
                await wait_until(lambda: bool(room_a.uploads()))
            elif operation == "download":
                await expect(p.locator("article.file-item")).to_have_count(1)
                await p.locator(".file-download").click()
                await wait_until(lambda: bool(room_a.downloads()))
            await p.locator("#close-files").click()
            await add_room(case)
            await switch_room(p, 1)
            room_a.file_pages.append(files_page(fresh_metadata))
            await open_files(case)
            await expect(p.locator("#file-list")).to_contain_text(fresh_metadata["filename"])
            held.set_result((201, old_metadata, {}) if operation == "upload" else
                            files_page(old_metadata) if operation == "list" else (200, data, {}))
            await asyncio.sleep(0.15)
            assert not downloads and not await p.evaluate("window.__file_blobs"), "A→B→A revived an old download"
            assert old_metadata["filename"] not in await p.locator("#file-list").inner_text(), "Old file callback overwrote a fresh activation list"
            if operation == "upload":
                await expect(p.locator("#retry-file")).to_be_visible()


async def test_room_tabs_old_status_read_and_write_stay_stale_after_return(browser):
    old_status = status_receipt("Old activation status", "red")
    fresh_status = status_receipt("Fresh activation status", "blue")
    for operation in ["read", "write"]:
        held = asyncio.get_running_loop().create_future()

        async def hold_result(body):
            return await held

        room_a = MailFixture(status_pages=[hold_result] if operation == "read" else None,
                             status_results=[hold_result] if operation == "write" else None)
        fixture = RoomFixture({TOKEN_A: room_a, TOKEN_B: MailFixture()})
        async with BrowserCase(browser, fixture) as case:
            p = case.page
            await case.join()
            await wait_until(lambda: bool(room_a.status_reads()))
            if operation == "write":
                await open_status(case)
                await p.locator("#status-input").fill(old_status["text"])
                await p.locator('input[name="status-color"][value="red"]').check()
                await p.locator("#save-status").click()
                await wait_until(lambda: bool(room_a.status_updates()))
                await p.locator("#close-status").click()
            await add_room(case)
            room_a.status_pages.append({"status": fresh_status})
            await switch_room(p, 1)
            await expect(p.locator("#room-status-text")).to_have_text(fresh_status["text"])
            held.set_result({"status": old_status} if operation == "read" else old_status)
            await asyncio.sleep(0.15)
            await expect(p.locator("#room-status-text")).to_have_text(fresh_status["text"])
            await expect(p.locator("#room-status")).to_have_attribute("data-color", "blue")


async def test_room_tabs_expiry_while_inactive_does_not_restore_old_data(browser):
    expires = datetime.now(timezone.utc) + timedelta(seconds=20)
    parent = receipt("Inactive expired parent", expires=expires)
    child = receipt("Still-live child", reply_to=parent["id"])
    metadata = file_receipt(b"expired file", "inactive-expired.bin", expires=expires)
    status = status_receipt("Inactive expired status", "red", expires=expires)
    fixture = RoomFixture({TOKEN_A: MailFixture([page_of(parent, child)],
        file_pages=[files_page(metadata)], status_pages=[{"status": status}]), TOKEN_B: MailFixture()})
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.clock.install()
        await case.join()
        await expect(p.locator("#room-status-text")).to_have_text(status["text"])
        await choose_reply(p, parent)
        await p.locator("#message-input").fill("A reply must not silently lose its expired parent")
        await open_files(case)
        await expect(p.locator("article.file-item")).to_have_count(1)
        await p.locator("#close-files").click()
        await add_room(case)
        await p.clock.fast_forward(21_000)
        await switch_room(p, 1)
        await expect(p.locator("article.message")).to_have_count(1)
        assert parent["message"] not in await p.locator("#messages").inner_text()
        await expect(p.locator("#room-status-text")).to_have_text("Set room status")
        await expect(p.locator("#reply-target")).to_be_visible()
        assert parent["message"] not in await p.locator("#reply-target-text").inner_text()
        await expect(p.locator("#send-button")).to_be_disabled()
        await expect(message_row(p, child).locator(".reply-reference")).to_be_disabled()
        await open_files(case)
        await expect(p.locator("article.file-item")).to_have_count(0)
        assert not fixture.rooms[TOKEN_A].downloads() and not fixture.rooms[TOKEN_A].sends()


async def test_room_tabs_bound_keyboard_and_narrow_layout(browser):
    tokens = [TOKEN_A, TOKEN_B] + [f"{index:064x}" for index in range(3, 9)]
    fixture = RoomFixture({token: MailFixture() for token in tokens})
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join()
        for token in tokens[1:]:
            await add_room(case, token)
        await expect(p.locator('#room-tabs [role="tab"]')).to_have_count(8)
        await expect(p.locator("#add-room")).to_be_disabled()
        tab_visible = """() => {
          const list = document.querySelector('#room-tabs'), bounds = list.getBoundingClientRect();
          const active = list.querySelector('[role="tab"][aria-selected="true"]').parentElement.getBoundingClientRect();
          const left = bounds.left + list.clientLeft, right = left + list.clientWidth;
          return active.left >= left - .5 && active.right <= right + .5;
        }"""
        for viewport in [{"width": 1280, "height": 900}, {"width": 390, "height": 844},
                         {"width": 844, "height": 390}, {"width": 390, "height": 320}]:
            await p.set_viewport_size(viewport)
            # Resize events/observers run at a browser frame boundary. Wait for
            # the actual visible geometry, including the close button.
            await p.wait_for_function(tab_visible)
            await assert_layout(p)
            active = await room_tab(p, 8).bounding_box()
            assert active and active["x"] >= 0 and active["x"] + active["width"] <= viewport["width"], "Active tab is clipped instead of scrolled into view"
        # A container resize also needs to reveal the active tab even when the
        # window itself did not resize (for example, a surrounding layout change).
        await p.locator("#room-tabs").evaluate("list => list.style.maxWidth = '180px'")
        await p.wait_for_function(tab_visible)
        await p.locator("#room-tabs").evaluate("list => list.style.removeProperty('max-width')")
        await p.wait_for_function(tab_visible)
        await room_tab(p, 8).focus()
        await p.keyboard.press("Home")
        await expect(room_tab(p, 1)).to_have_attribute("aria-selected", "true")
        await expect(room_tab(p, 1)).to_be_focused()
        await p.keyboard.press("ArrowRight")
        await expect(room_tab(p, 2)).to_have_attribute("aria-selected", "true")
        await p.keyboard.press("End")
        await expect(room_tab(p, 8)).to_have_attribute("aria-selected", "true")
        await p.keyboard.press("ArrowLeft")
        await expect(room_tab(p, 7)).to_have_attribute("aria-selected", "true")
        await p.get_by_role("button", name="Close Room 7", exact=True).click()
        await expect(p.locator('#room-tabs [role="tab"]')).to_have_count(7)
        await expect(p.locator("#add-room")).to_be_enabled()
        await p.locator("#leave-room").click()
        await expect(p.locator('#room-tabs [role="tab"]')).to_have_count(6)
        await expect(p.locator("#chat-view")).to_be_visible()
        while await p.locator('#room-tabs [role="tab"]').count():
            await p.locator("#leave-room").click()
        await expect(p.locator("#join-view")).to_be_visible()
        assert await p.locator('#room-tabs [role="tab"]').count() == 0, "Closing every room did not reset the tab strip"
        await expect(p.locator("#room-token")).to_have_value("")


async def test_room_tabs_local_labels_screenshots_and_pagehide_forgets_every_room(browser):
    fixture = RoomFixture({TOKEN_A: MailFixture([page_of(receipt("Tea-room dispatch: biscuits are ready", "tea-agent"))]),
                           TOKEN_B: MailFixture([page_of(receipt("Workshop dispatch: the little robot is ready", "workshop-agent"))])})
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await p.locator("#room-label").fill("Tea break ☕")
        await case.join(username="biscuit-inspector")
        await p.locator("#message-input").fill("An inactive private draft")
        await make_key(p)
        await p.locator("#close-key").click()
        await p.locator("#add-room").click()
        await p.locator("#room-label").fill("Robot workshop 🤖")
        await case.join(TOKEN_B, "robot-postmaster")
        await expect(p.locator('#room-tabs [role="tab"]')).to_have_count(2)
        await expect(p.locator("#room-tabs")).to_contain_text("Tea break ☕")
        await expect(p.locator("#room-tabs")).to_contain_text("Robot workshop 🤖")
        assert not any("room_label" in request["body"] or "label" in request["body"] for request in fixture.requests), "Local tab labels were sent to the API"
        await p.locator("#message-input").fill("A tiny robot needs a tiny hat.")
        await expect(p.locator("#toast")).not_to_be_visible()
        await assert_layout(p)
        await p.screenshot(path="/tmp/psstoffice-chat-tabs-desktop.png", full_page=True)
        await p.set_viewport_size({"width": 390, "height": 844})
        await assert_layout(p)
        await p.screenshot(path="/tmp/psstoffice-chat-tabs-mobile.png", full_page=True)
        await p.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
        await expect(p.locator("#join-view")).to_be_visible()
        assert await p.locator('#room-tabs [role="tab"]').count() == 0
        await expect(p.locator("#room-token")).to_have_value("")
        fixture.rooms[TOKEN_A].read_pages.append(EMPTY_PAGE.copy())
        await case.join()
        await expect(p.locator("#message-input")).to_have_value("")
        await p.locator("#manage-key").click()
        await expect(p.locator("#public-key")).to_have_value("")
        await expect(p.locator("#signing-toggle")).not_to_be_checked()
        assert "Tea-room dispatch" not in await p.locator("#messages").inner_text()
    for label in ["🐦" * 32, "<svg/onload=window.__x=1>"]:
        async with BrowserCase(browser) as case:
            p = case.page
            await p.locator("#room-token").fill(TOKEN_A)
            for bad_label in ["🐦" * 33, "direction\u202echanger", "control\u0001character"]:
                await p.locator("#room-label").fill(bad_label)
                await p.locator("#join-button").click()
                assert not case.fixture.requests, "An invalid local tab label reached the API"
                await expect(p.locator("#join-view")).to_be_visible()
            await p.locator("#room-label").fill(label)
            await case.join()
            await expect(p.locator('#room-tabs [role="tab"]')).to_have_text(label)
            assert await p.locator("#room-tabs svg, #room-tabs img, #room-tabs script").count() == 0
            assert await p.evaluate("window.__x === undefined"), "Tab label executed markup"


VIRTUAL_STREAM_UPLOAD = """
window.__raw_uploads = [];
window.__file_array_buffer_calls = 0;
const originalFileArrayBuffer = File.prototype.arrayBuffer;
File.prototype.arrayBuffer = function(...args) {
  window.__file_array_buffer_calls++;
  if (this.size > 1000000) throw new Error('Large files must never be buffered');
  return originalFileArrayBuffer.apply(this, args);
};
window.__file_timeouts = [];
const originalFileTimeout = window.setTimeout;
window.setTimeout = function(callback, delay, ...args) {
  if (delay === 900000) window.__file_timeouts.push(delay);
  return originalFileTimeout(callback, delay, ...args);
};
const originalStreamFetch = window.fetch.bind(window);
window.fetch = async (url, options = {}) => {
  if (!String(url).endsWith('/v1/files/upload')) return originalStreamFetch(url, options);
  const headers = options.headers;
  const decode = value => new TextDecoder().decode(Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), char => char.charCodeAt(0)));
  const record = {headers: {...headers}, filename: decode(headers['X-File-Name']),
    username: headers['X-File-Username'] ? decode(headers['X-File-Username']) : null,
    isFile: options.body instanceof File, sameFile: options.body === window.__virtual_file,
    size: options.body.size, signal: options.signal, body: options.body};
  window.__raw_uploads.push(record);
  if (window.__raw_mode === 'abort') { window.__raw_mode = ''; throw new TypeError('Synthetic interrupted upload'); }
  const now = Date.now();
  const value = {id: 'aabbccdd-0123-4567-89ab-0123456789ab', filename: record.filename,
    username: record.username, size_bytes: record.size, sha256: '0'.repeat(64),
    created_at: new Date(now).toISOString(), expires_at: new Date(now + 7200000).toISOString()};
  if (window.__raw_mode === 'hold') return new Promise(resolve => { window.__release_raw_upload = () => resolve(new Response(JSON.stringify(value), {status:201, headers:{'Content-Type':'application/json'}})); });
  return new Response(JSON.stringify(value), {status:201, headers:{'Content-Type':'application/json'}});
};
window.__select_virtual_file = size => {
  const file = new File(['Tiny test backing data; no giant allocation.'], 'parcel 🐦.bin');
  Object.defineProperty(file, 'size', {value:size}); window.__virtual_file = file;
  const transfer = new DataTransfer(); transfer.items.add(file);
  const input = document.querySelector('#file-input'); input.files = transfer.files;
  input.dispatchEvent(new Event('change', {bubbles:true}));
};
"""

STREAM_SAVE_PICKER = """
window.__stream_save = {names: [], writes: [], closed:0, aborted:0, total:0, digest:null};
window.showSaveFilePicker = async options => {
  const record = window.__stream_save; record.names.push(options.suggestedName);
  return {createWritable: async () => ({
    write: async bytes => {
      if (window.__hold_stream_write) await new Promise(resolve => { window.__release_stream_write = resolve; });
      if (record.aborted) throw new Error('Writer aborted');
      record.total += bytes.byteLength; record.writes.push(new Uint8Array(bytes));
    },
    close: async () => {
      const bytes = new Uint8Array(record.total); let offset = 0;
      for (const piece of record.writes) { bytes.set(piece, offset); offset += piece.length; }
      record.digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), value => value.toString(16).padStart(2,'0')).join('');
      record.closed++; record.writes = [];
    },
    abort: async () => { record.aborted++; record.writes = []; window.__release_stream_write?.(); }
  })};
};
"""


async def test_stream_sha256_matches_independent_vectors_and_chunk_boundaries(browser):
    async with BrowserCase(browser) as case:
        # Padding boundaries, binary bytes, empty data, and multi-megabyte input.
        for length in [0, 1, 55, 56, 63, 64, 65, 127, 128, 129, 1_000_137]:
            expected = hashlib.sha256(bytes(index % 251 for index in range(length))).hexdigest()
            for chunk in [1, 63, 65, 65536] if length < 130 else [65536]:
                value = await case.page.evaluate("""({length,chunk}) => {
                  const hash = new StreamSha256();
                  for (let offset=0; offset<length; offset+=chunk) {
                    const bytes = new Uint8Array(Math.min(chunk,length-offset));
                    for (let index=0; index<bytes.length; index++) bytes[index]=(offset+index)%251;
                    hash.update(bytes);
                  }
                  return {digest:hash.digest(), block:hash.block.length, words:hash.words.length};
                }""", {"length": length, "chunk": chunk})
                assert value == {"digest": expected, "block": 64, "words": 64}


async def test_stream_upload_maximum_uses_file_body_without_large_allocation(browser):
    fixture = MailFixture(max_file_bytes=500_000_000)
    async with BrowserCase(browser, fixture, init_script=VIRTUAL_STREAM_UPLOAD) as case:
        p = case.page
        await case.join(username="郵便 🐦")
        await open_files(case)
        await expect(p.locator('#files-intro')).to_contain_text('500,000,000 bytes')
        await p.evaluate('window.__select_virtual_file(500000001)')
        await expect(p.locator('#upload-file')).to_be_disabled()
        assert not await p.evaluate('window.__raw_uploads.length')
        await p.evaluate('window.__select_virtual_file(500000000)')
        await expect(p.locator('#upload-file')).to_be_enabled()
        await p.locator('#upload-file').click()
        await expect(p.locator('#file-list')).to_contain_text('parcel 🐦.bin')
        record = await p.evaluate("""() => {const r=window.__raw_uploads[0]; return {headers:r.headers,filename:r.filename,username:r.username,isFile:r.isFile,sameFile:r.sameFile,size:r.size};}""")
        assert record['isFile'] and record['sameFile'] and record['size'] == 500_000_000
        assert record['filename'] == 'parcel 🐦.bin' and record['username'] == '郵便 🐦'
        assert record['headers']['X-Room-Token'] == TOKEN_A
        assert record['headers']['Content-Type'] == 'application/octet-stream'
        assert record['headers']['X-File-Size'] == '500000000'
        assert re.fullmatch('[0-9a-f]{32}', record['headers']['X-Client-File-ID'])
        assert await p.evaluate('window.__file_array_buffer_calls') == 0
        assert await p.evaluate('window.__file_timeouts') == [900_000]
        assert not fixture.uploads(), 'Virtual large upload must not allocate or transfer 500 MB'


async def test_stream_upload_actual_bytes_headers_and_file_space_full(browser):
    data = bytes(range(251)) * 3985
    assert len(data) > 1_000_000
    fixture = MailFixture(max_file_bytes=500_000_000, upload_results=[
        (503, {'error': {'code': 'file_space_full', 'message': TOKEN_A + ' private server detail'}}, {'Retry-After': '30'})])
    async with BrowserCase(browser, fixture) as case:
        p = case.page
        await case.join(username='stream-agent')
        await open_files(case)
        await choose_file(p, data, 'stream.bin')
        await p.locator('#upload-file').click()
        await wait_until(lambda: len(fixture.uploads()) == 1)
        await expect(p.locator('#files-error')).to_contain_text('File space full')
        request = fixture.uploads()[0]
        assert request['url'].endswith('/v1/files/upload')
        assert request['body']['data'] == data and request['body']['size_bytes'] == len(data)
        assert 'data_base64' not in request['body']
        assert request['headers']['x-file-size'] == str(len(data))
        assert TOKEN_A not in await p.locator('#files-dialog').inner_text()
        await expect(p.locator('#retry-file')).to_be_disabled()
        await asyncio.sleep(.1)
        assert len(fixture.uploads()) == 1, 'Full file storage must never trigger automatic re-upload'


async def test_stream_upload_retry_and_stale_response_survive_room_tabs(browser):
    fixture = RoomFixture({TOKEN_A:MailFixture(max_file_bytes=500_000_000), TOKEN_B:MailFixture(max_file_bytes=500_000_000)})
    async with BrowserCase(browser, fixture, init_script=VIRTUAL_STREAM_UPLOAD) as case:
        p = case.page
        await case.join(username='original uploader')
        await open_files(case)
        await p.evaluate("window.__raw_mode='abort'; window.__select_virtual_file(500000000)")
        await p.locator('#upload-file').click()
        await expect(p.locator('#file-pending')).to_be_visible()
        await p.locator('#close-files').click()
        await add_room(case)
        await room_tab(p, 1).click()
        await p.locator('#active-username').fill('changed uploader')
        await open_files(case)
        await expect(p.locator('#retry-file')).to_be_enabled()
        await p.evaluate("window.__raw_mode='hold'")
        await p.locator('#retry-file').click()
        await p.wait_for_function('window.__raw_uploads.length === 2')
        assert await p.evaluate("""() => {
          const [a,b]=window.__raw_uploads; return a.body===b.body && JSON.stringify(a.headers)===JSON.stringify(b.headers);
        }"""), 'Resuming a room must retry exactly the same immutable File and original metadata'
        await p.locator('#close-files').click()
        await room_tab(p, 2).click()
        await room_tab(p, 1).click()
        await p.evaluate('window.__release_raw_upload()')
        await open_files(case)
        await expect(p.locator('#file-pending')).to_be_visible()
        assert 'parcel 🐦.bin' not in await p.locator('#file-list').inner_text(), 'An old activation acknowledged a stale large upload'
        assert await p.evaluate('window.__raw_uploads[1].signal.aborted')
        assert await p.evaluate('window.__file_array_buffer_calls') == 0


async def test_large_live_files_remain_downloadable_after_upload_limit_reverts(browser):
    data = b'Large downloadable temporary parcel.' * 30001
    assert len(data) > 1_000_000
    metadata = file_receipt(data, 'large-after-revert.bin')
    fixture = MailFixture(max_file_bytes=1_000_000, file_pages=[files_page(metadata)], download_results=[(200,data,{})])
    async with BrowserCase(browser, fixture, init_script=STREAM_SAVE_PICKER + FILE_BLOBS + CHUNKED_DOWNLOAD) as case:
        p = case.page
        await case.join()
        await open_files(case)
        await expect(p.locator('#files-intro')).to_contain_text('1,000,000 bytes')
        await p.locator('.file-download').click()
        await p.wait_for_function('window.__stream_save.closed === 1')
        saved = await p.evaluate('window.__stream_save')
        assert saved['digest'] == metadata['sha256'] and saved['total'] == len(data)
        assert saved['names'] == ['large-after-revert.bin'] and saved['aborted'] == 0
        assert not await p.evaluate('window.__file_blobs'), 'Streamed files must not become large Blob URLs'


async def test_stream_download_aborts_tampered_truncated_or_oversized_files(browser):
    data = b'The immutable streaming parcel.' * 40001
    metadata = file_receipt(data, 'stream-check.bin')
    for downloaded in [data[:-1], b'!' + data[1:], data + b'!']:
        fixture = MailFixture(file_pages=[files_page(metadata)], download_results=[(200,downloaded,{})])
        async with BrowserCase(browser, fixture, init_script=STREAM_SAVE_PICKER + FILE_BLOBS + CHUNKED_DOWNLOAD) as case:
            p = case.page
            await case.join()
            await open_files(case)
            await p.locator('.file-download').click()
            await expect(p.locator('#files-error')).not_to_have_text('')
            saved = await p.evaluate('window.__stream_save')
            assert saved['closed'] == 0 and saved['aborted'] > 0
            assert not await p.evaluate('window.__file_blobs')


async def test_stream_download_room_change_aborts_uncommitted_file(browser):
    data = b'Secret bytes from an old activation.' * 30001
    metadata = file_receipt(data, 'stale-stream.bin')
    fixture = RoomFixture({TOKEN_A:MailFixture(file_pages=[files_page(metadata)], download_results=[(200,data,{})]), TOKEN_B:MailFixture()})
    async with BrowserCase(browser, fixture, init_script=STREAM_SAVE_PICKER + FILE_BLOBS + CHUNKED_DOWNLOAD) as case:
        p = case.page
        await case.join()
        await open_files(case)
        await p.evaluate('window.__hold_stream_write=true')
        await p.locator('.file-download').click()
        await p.wait_for_function("typeof window.__release_stream_write === 'function'")
        await p.locator('#close-files').click()
        await add_room(case)
        await room_tab(p,1).click()
        await asyncio.sleep(.1)
        saved = await p.evaluate('window.__stream_save')
        assert saved['closed'] == 0 and saved['aborted'] > 0
        assert not await p.evaluate('window.__file_blobs')


async def test_large_download_without_save_picker_has_bounded_fallback(browser):
    metadata = {**file_receipt(b'synthetic giant'), 'size_bytes':500_000_000}
    fixture = MailFixture(file_pages=[files_page(metadata)])
    async with BrowserCase(browser, fixture, init_script='window.showSaveFilePicker=undefined;' + FILE_BLOBS) as case:
        p = case.page
        await case.join()
        await open_files(case)
        await p.locator('.file-download').click()
        await expect(p.locator('#files-error')).to_contain_text('Save file picker')
        assert not fixture.downloads() and not await p.evaluate('window.__file_blobs'), 'Unsupported browsers must not buffer 500 MB'
    data = b'The bounded fallback is inert.' * 40001
    metadata = file_receipt(data, 'fallback.bin')
    fixture = MailFixture(file_pages=[files_page(metadata)], download_results=[(200,data,{})])
    async with BrowserCase(browser, fixture, init_script='window.showSaveFilePicker=undefined;' + FILE_BLOBS + CHUNKED_DOWNLOAD) as case:
        p = case.page
        await case.join()
        await open_files(case)
        async with p.expect_download() as info:
            await p.locator('.file-download').click()
        assert Path(await (await info.value).path()).read_bytes() == data
        assert await p.evaluate('window.__file_blobs') == [{'type':'application/octet-stream','size':len(data)}]


async def main():
    tests = [
        test_token_and_privacy,
        test_pagination_rendering_and_layout,
        test_utf8_and_keyboard,
        test_uncertain_send_retry,
        test_ack_before_earlier_read,
        test_retry_lifetime,
        test_expiry_and_stale_room,
        test_retry_after,
        test_key_generation_export_import_and_interop,
        test_signed_retry_refresh_and_original_receipt,
        test_wrong_signer_acknowledgement,
        test_signature_verification_and_chatter_filter,
        test_chatter_expiry_and_history_trim,
        test_unsupported_signing_keeps_unsigned_chat,
        test_leaving_during_crypto_does_not_revive_secrets,
        test_screenshots,
        test_files_hidden_pagination_and_polling,
        test_file_upload_receipt_survives_older_list_response,
        test_file_upload_bounds_any_type_and_local_privacy,
        test_file_names_render_as_text_and_invalid_names,
        test_file_cooldown_is_per_room_and_retry_after,
        test_file_ambiguous_retry_preserves_payload_and_chat,
        test_file_wrong_receipt_and_retry_lifetime,
        test_file_download_is_explicit_inert_and_verified,
        test_file_download_rejects_tampered_oversized_or_wrong_type,
        test_file_browser_rejects_malformed_metadata,
        test_file_expiry_is_local_and_prevents_download,
        test_leaving_during_file_preparation_does_not_send,
        test_leaving_during_download_verification_does_not_save,
        test_leaving_during_file_network_does_not_revive_data,
        test_file_dialog_keyboard_and_responsive_layout,
        test_room_status_unicode_colors_validation_and_privacy,
        test_room_status_plain_text_top_right_and_responsive_dialog,
        test_room_status_local_expiry_and_reads_do_not_renew,
        test_room_status_older_read_does_not_erase_own_write,
        test_room_status_stale_read_and_write_after_leaving,
        test_room_status_backoff_and_unconfirmed_write,
        test_room_status_rejects_malformed_server_receipts,
        test_room_status_write_and_manual_check_backoff_blocks_polling,
        test_reply_types_plain_text_and_parent_navigation,
        test_reply_parent_expiry_preserves_child_and_blocks_new_draft,
        test_reply_trimmed_parent_has_honest_reference,
        test_signed_structured_reply_unicode_interop,
        test_reply_signature_metadata_tampering_and_downgrade,
        test_reply_retry_freezes_metadata_after_parent_expiry,
        test_reply_acknowledgement_must_match_metadata,
        test_reply_rejects_malformed_metadata_and_missing_parent_ack,
        test_reply_composer_responsive_and_leave_clears_metadata,
        test_leaving_during_enriched_signing_does_not_revive_reply,
        test_room_tabs_add_duplicate_cancel_close_and_privacy,
        test_room_tabs_independent_composer_identity_reply_and_chatter,
        test_room_tabs_restore_scroll_and_only_active_polls,
        test_room_tabs_uncertain_send_keeps_exact_room_retry,
        test_room_tabs_crypto_completions_stay_stale_after_return,
        test_room_tabs_signed_retry_refresh_does_not_mutate_resumed_attempt,
        test_room_tabs_old_read_is_stale_even_after_return,
        test_room_tabs_files_status_and_upload_retry_are_independent,
        test_room_tabs_file_preparation_stays_stale_after_return,
        test_room_tabs_old_file_network_results_stay_stale_after_return,
        test_room_tabs_old_status_read_and_write_stay_stale_after_return,
        test_room_tabs_expiry_while_inactive_does_not_restore_old_data,
        test_room_tabs_bound_keyboard_and_narrow_layout,
        test_room_tabs_local_labels_screenshots_and_pagehide_forgets_every_room,
        test_stream_sha256_matches_independent_vectors_and_chunk_boundaries,
        test_stream_upload_maximum_uses_file_body_without_large_allocation,
        test_stream_upload_actual_bytes_headers_and_file_space_full,
        test_stream_upload_retry_and_stale_response_survive_room_tabs,
        test_large_live_files_remain_downloadable_after_upload_limit_reverts,
        test_stream_download_aborts_tampered_truncated_or_oversized_files,
        test_stream_download_room_change_aborts_uncommitted_file,
        test_large_download_without_save_picker_has_bounded_fallback,
    ]
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    async with async_playwright() as playwright:
        if not executable and not Path(playwright.chromium.executable_path).exists():
            executable = shutil.which("chromium")
        launch = {"headless": True}
        if executable:
            launch["executable_path"] = executable
        browser = await playwright.chromium.launch(**launch)
        try:
            for test in tests:
                await test(browser)
                print(f"PASS {test.__name__}", flush=True)
        finally:
            await browser.close()
    print(f"{len(tests)} Chromium scenarios passed; no real rooms were accessed.")


if __name__ == "__main__":
    asyncio.run(main())
