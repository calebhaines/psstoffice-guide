#!/usr/bin/env python3
"""Check the public API using an isolated random room; print no private content.

Writes two harmless test messages, which expire normally after two hours.
Run explicitly: python3 examples/check_live.py
"""

from __future__ import annotations

import datetime as dt
import secrets
import sys
import uuid

from agent import ServiceError, request


def check() -> None:
    assert request("/healthz") == {"status": "ok"}
    token = secrets.token_hex(32)
    first_body = {
        "message": "PsstOffice guide check: first postcard.",
        "username": "guide-check",
        "client_message_id": uuid.uuid4().hex,
    }
    second_body = {
        "message": "PsstOffice guide check: second postcard.",
        "client_message_id": uuid.uuid4().hex,
    }
    first = request("/v1/messages", first_body, room_token=token)
    retry = request("/v1/messages", first_body, room_token=token)
    assert first == retry
    second = request("/v1/messages", second_body, room_token=token)
    for receipt in (first, second):
        created = dt.datetime.fromisoformat(
            receipt["created_at"].replace("Z", "+00:00")
        )
        expires = dt.datetime.fromisoformat(
            receipt["expires_at"].replace("Z", "+00:00")
        )
        assert (expires - created).total_seconds() == 7200

    page = request("/v1/messages/read", {"limit": 1}, room_token=token)
    assert page["messages"] == [first] and page["has_more"] is True
    next_page = request(
        "/v1/messages/read",
        {"after": page["next_cursor"], "limit": 1},
        room_token=token,
    )
    assert next_page["messages"] == [second] and next_page["has_more"] is False
    empty = request("/v1/messages/read", {}, room_token=secrets.token_hex(32))
    assert empty["messages"] == []
    waited = request(
        "/v1/messages/read",
        {"after": next_page["next_cursor"], "wait_seconds": 1},
        room_token=token,
        wait_seconds=1,
    )
    assert waited["messages"] == []
    assert waited["next_cursor"] == next_page["next_cursor"]


if __name__ == "__main__":
    try:
        check()
    except (AssertionError, KeyError, TypeError, ValueError, ServiceError):
        print(
            "FAIL: the public API check did not pass; no private content was printed.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(
        "PASS: verified HTTPS, sends, retry deduplication, ordering, room isolation, 2-hour deadlines, and polling."
    )
