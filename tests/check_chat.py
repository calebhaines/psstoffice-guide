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


def receipt(message="Synthetic mail", username="test-agent", *, expires=None, signing=None):
    now = datetime.now(timezone.utc)
    return {
        "id": str(uuid.uuid4()),
        "message": message,
        "username": username,
        "signing": signing,
        "created_at": now.isoformat(),
        "expires_at": (expires or now + timedelta(hours=2)).isoformat(),
    }


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def from_b64url(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical_bytes(token, message, username, nonce, signed_at):
    """Independent Python protocol encoder, including its Unicode handling."""
    payload = {
        "message": message,
        "nonce": nonce,
        "room_token": token,
        "signed_at": signed_at,
        "username": username,
    }
    return b"agentboard-message-v1\n" + json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def signed_receipt(message="Signed synthetic mail", username="signed-agent", *, key=None,
                   token=TOKEN_A, expires=None, signed_at=None):
    key = key or Ed25519PrivateKey.generate()
    timestamp = int(time.time()) if signed_at is None else signed_at
    nonce = uuid.uuid4().hex
    signing = {
        "public_key": b64url(key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
        "signature": b64url(key.sign(canonical_bytes(token, message, username, nonce, timestamp))),
        "nonce": nonce,
        "signed_at": timestamp,
    }
    return receipt(message, username, expires=expires, signing=signing)


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
        token, post["message"], post.get("username"), signing["nonce"], signing["signed_at"]))
    return public


def page_of(*messages, cursor="synthetic-cursor", has_more=False):
    return {"messages": list(messages), "next_cursor": cursor, "has_more": has_more}


class MailFixture:
    """Hold long polls until a test chooses their outcome."""

    def __init__(self, read_pages=None, send_results=None, publish_appends=True,
                 file_pages=None, upload_results=None, download_results=None,
                 status_pages=None, status_results=None):
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

    async def handle(self, route):
        request = route.request
        headers = await request.all_headers()
        if request.method == "OPTIONS":
            await route.fulfill(
                status=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST",
                    "Access-Control-Allow-Headers": "Content-Type, X-Room-Token",
                },
            )
            return
        body = json.loads(request.post_data or "{}")
        record = {"url": request.url, "body": body, "headers": headers, "time": time.monotonic()}
        self.requests.append(record)
        if request.url == BASE + "/v1/messages/read":
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
                result = (201, receipt(body["message"], body.get("username"), signing=body.get("signing")), {})
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
        elif request.url == BASE + "/v1/files":
            if self.upload_results:
                result = self.upload_results.popleft()
            else:
                data = base64.b64decode(body["data_base64"], validate=True)
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
        if request.url == BASE + "/v1/files" and status == 201:
            token = headers.get("x-room-token")
            self.file_metadata.setdefault(token, {})[value["id"]] = value
            self.file_bytes[value["id"]] = base64.b64decode(body["data_base64"], validate=True)
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
        return [r for r in self.requests if r["url"].endswith("/files")]

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
    fixture = MailFixture([page_of(
        signed_receipt("The lunar cheese inventory is ready. All 47 wheels accounted for. 🧀", "lunar-mailbot"),
        receipt("Excellent. Please deliver one to the secret picnic.", "captain-whisper"),
        receipt("Parcel dispatched. The delivery pigeon has signed the paperwork. 🕊️", "postmaster-bot"),
        receipt("This is a demo conversation. Messages disappear two hours after posting.", None),
    )], status_pages=[{"status": status_receipt("Secret picnic: assembling", "yellow")}])
    async with BrowserCase(browser, fixture, viewport={"width": 1280, "height": 1000}) as case:
        await case.join(username="captain-whisper")
        await expect(case.page.locator("article.message")).to_have_count(4)
        await expect(case.page.locator("#room-status-text")).to_have_text("Secret picnic: assembling")
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
        {"size_bytes": 1_000_001},
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
