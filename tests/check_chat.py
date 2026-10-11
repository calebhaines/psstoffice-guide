#!/usr/bin/env python3
"""Exercise the standalone chat in Chromium using synthetic API responses.

Development-only dependencies: ``python -m pip install playwright`` and
``python -m playwright install chromium``. An installed system Chromium can be
selected with PLAYWRIGHT_CHROMIUM_EXECUTABLE. No real rooms or messages are accessed.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from playwright.async_api import async_playwright, expect


ROOT = Path(__file__).resolve().parents[1]
BASE = "https://psstoffice.89b84f87.nip.io"
TOKEN_A = "Aa01" * 16
TOKEN_B = "Bb02" * 16
EMPTY_PAGE = {"messages": [], "next_cursor": None, "has_more": False}


def receipt(message="Synthetic mail", username="test-agent", *, expires=None):
    now = datetime.now(timezone.utc)
    return {
        "id": str(uuid.uuid4()),
        "message": message,
        "username": username,
        "signing": None,
        "created_at": now.isoformat(),
        "expires_at": (expires or now + timedelta(hours=2)).isoformat(),
    }


def page_of(*messages, cursor="synthetic-cursor", has_more=False):
    return {"messages": list(messages), "next_cursor": cursor, "has_more": has_more}


class MailFixture:
    """Hold long polls until a test chooses their outcome."""

    def __init__(self, read_pages=None, send_results=None, publish_appends=True):
        self.read_pages = deque([EMPTY_PAGE.copy()] if read_pages is None else read_pages)
        self.send_results = deque(send_results or [])
        self.publish_appends = publish_appends
        self.append_receipts = []
        self.requests = []
        self.pending_reads = []
        self.closed = False

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
                result = (201, receipt(body["message"], body.get("username")), {})
        else:
            raise AssertionError(f"Unexpected API route: {request.url}")
        if result == "abort":
            await route.abort("failed")
            return
        if isinstance(result, tuple):
            status, value, extra_headers = result
        else:
            status, value, extra_headers = 200, result, {}
        if request.url == BASE + "/v1/messages" and status == 201:
            self.append_receipts.append(value)
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
                body=json.dumps(value),
                content_type="application/json",
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
        return [r for r in self.requests if r["url"].endswith("/read")]

    def sends(self):
        return [r for r in self.requests if r["url"].endswith("/messages")]

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
"""


async def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for browser activity")
        await asyncio.sleep(0.025)


class BrowserCase:
    def __init__(self, browser, fixture=None, viewport=None):
        self.browser = browser
        self.fixture = fixture or MailFixture()
        self.viewport = viewport or {"width": 1280, "height": 900}

    async def __aenter__(self):
        self.context = await self.browser.new_context(viewport=self.viewport)
        self.unexpected_requests = []
        self.context.on("request", lambda request: self.unexpected_requests.append(request.url)
                        if request.url.startswith(("http://", "https://")) and not request.url.startswith(BASE + "/") else None)
        await self.context.add_init_script(INSTRUMENT_FETCH)
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
        assert "signature attached · not verified" in await p.locator("#messages").inner_text()
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


async def test_screenshots(browser):
    fixture = MailFixture([page_of(
        receipt("The lunar cheese inventory is ready. All 47 wheels accounted for. 🧀", "lunar-mailbot"),
        receipt("Excellent. Please deliver one to the secret picnic.", "captain-whisper"),
        receipt("Parcel dispatched. The delivery pigeon has signed the paperwork. 🕊️", "postmaster-bot"),
        receipt("This is a demo conversation. Messages disappear two hours after posting.", None),
    )])
    async with BrowserCase(browser, fixture, viewport={"width": 1280, "height": 1000}) as case:
        await case.join(username="captain-whisper")
        await expect(case.page.locator("article.message")).to_have_count(4)
        await case.page.locator("#message-input").fill("One tiny note, on its way…")
        assert await case.page.locator("#active-token").get_attribute("type") == "password"
        await case.page.wait_for_timeout(250)
        await case.page.screenshot(path="/tmp/psstoffice-chat-desktop.png", full_page=True)
        await case.page.set_viewport_size({"width": 390, "height": 844})
        assert await case.page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await case.page.screenshot(path="/tmp/psstoffice-chat-mobile.png", full_page=True)


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
        test_screenshots,
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
