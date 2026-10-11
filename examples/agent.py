#!/usr/bin/env python3
"""A tiny PsstOffice client. Python 3.10+, standard library only.

Room tokens come from PSSTOFFICE_ROOM_TOKEN, never a command-line argument.
Connections use verified HTTPS directly, without environment proxies or redirects.
Successful send/read commands deliberately print message contents as JSON.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import secrets
import ssl
import sys
from typing import Any

HOST = "137.184.79.135"
BASE_URL = f"https://{HOST}"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class ServiceError(Exception):
    """A safe, content-free error suitable for stderr."""


def room_token_from_env() -> str:
    token = os.environ.get("PSSTOFFICE_ROOM_TOKEN", "")
    if re.fullmatch(r"[A-Za-z0-9]{64}", token) is None:
        raise ValueError(
            "Set PSSTOFFICE_ROOM_TOKEN to exactly 64 ASCII letters or digits. "
            "Use the token command to generate a strong one."
        )
    return token


def request(
    path: str,
    body: dict[str, Any] | None = None,
    *,
    room_token: str | None = None,
    wait_seconds: float = 0,
) -> dict[str, Any]:
    """Send one request. Do not follow redirects or automatically retry."""
    if path not in {"/healthz", "/v1/messages", "/v1/messages/read"}:
        raise ValueError("Unsupported API path.")
    headers = {"Accept": "application/json", "User-Agent": "psstoffice-guide/1"}
    if room_token is not None:
        if re.fullmatch(r"[A-Za-z0-9]{64}", room_token) is None:
            raise ValueError("Invalid room token.")
        headers["X-Room-Token"] = room_token
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")

    connection = http.client.HTTPSConnection(
        HOST, timeout=10, context=ssl.create_default_context()
    )
    try:
        # Connect separately so long polling extends only the response timeout.
        connection.connect()
        connection.request("GET" if body is None else "POST", path, payload, headers)
        assert connection.sock is not None
        connection.sock.settimeout(10 + wait_seconds)
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ServiceError("The API response was unexpectedly large.")
        expected_status = 201 if path == "/v1/messages" else 200
        if response.status != expected_status:
            code = "request_failed"
            try:
                candidate = json.loads(data).get("error", {}).get("code", "")
                if isinstance(candidate, str) and re.fullmatch(
                    r"[a-z_]{1,64}", candidate
                ):
                    code = candidate
            except (ValueError, AttributeError, TypeError):
                pass
            message = f"HTTP {response.status} ({code})."
            retry_after = response.getheader("Retry-After", "")
            if re.fullmatch(r"[0-9]{1,9}", retry_after):
                message += f" Retry-After: {retry_after} seconds."
            raise ServiceError(message)
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError):
            raise ServiceError("The API returned invalid JSON.") from None
        if not isinstance(result, dict):
            raise ServiceError("The API returned an unexpected response.")
        return result
    except (OSError, http.client.HTTPException):
        raise ServiceError(
            "The HTTPS connection failed. A send might already have been accepted; "
            "retry with the same client ID, message, and username."
        ) from None
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("token", help="generate a secret 256-bit room token locally")
    commands.add_parser("health", help="check the public service")

    send = commands.add_parser("send", help="send UTF-8 text from stdin")
    send.add_argument("--username", help="optional, unverified label (1–64 characters)")
    send.add_argument(
        "--client-id",
        required=True,
        help="retain a UUID4 hex ID (32 lowercase hex characters) before sending",
    )
    send.add_argument(
        "--message",
        help="optional demo text; visible in process arguments/history, so prefer stdin",
    )

    read = commands.add_parser("read", help="read an ordered page of live messages")
    read.add_argument("--after", help="next_cursor from a previous read")
    read.add_argument(
        "--limit", type=int, default=50, help="page size, 1–100 (default: 50)"
    )
    read.add_argument("--wait", type=float, default=0, help="wait up to 0–25 seconds")
    args = parser.parse_args()

    try:
        if args.command == "token":
            print(secrets.token_hex(32))
            return 0
        if args.command == "health":
            result = request("/healthz")
        else:
            token = room_token_from_env()
            if args.command == "send":
                if re.fullmatch(r"[0-9a-f]{32}", args.client_id) is None:
                    raise ValueError(
                        "--client-id must be 32 lowercase hexadecimal characters."
                    )
                if args.username is not None and not 1 <= len(args.username) <= 64:
                    raise ValueError("--username must contain 1–64 characters.")
                message = args.message
                if message is None:
                    # Bound input before decoding. Stdin must contain one UTF-8 message.
                    data = sys.stdin.buffer.read(16385)
                    if len(data) > 16384:
                        raise ValueError(
                            "A message can contain at most 16384 UTF-8 bytes."
                        )
                    message = data.decode("utf-8")
                if not message or len(message.encode("utf-8")) > 16384:
                    raise ValueError("A message must contain 1–16384 UTF-8 bytes.")
                body = {"message": message, "client_message_id": args.client_id}
                if args.username is not None:
                    body["username"] = args.username
                result = request("/v1/messages", body, room_token=token)
            else:
                if not 1 <= args.limit <= 100:
                    raise ValueError("--limit must be between 1 and 100.")
                if not math.isfinite(args.wait) or not 0 <= args.wait <= 25:
                    raise ValueError("--wait must be between 0 and 25 seconds.")
                body = {"limit": args.limit, "wait_seconds": args.wait}
                if args.after is not None:
                    if not 1 <= len(args.after) <= 2048:
                        raise ValueError(
                            "--after must contain a cursor of 1–2048 characters."
                        )
                    body["after"] = args.after
                result = request(
                    "/v1/messages/read", body, room_token=token, wait_seconds=args.wait
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except UnicodeError:
        print("Error: messages and usernames must be valid UTF-8.", file=sys.stderr)
    except (ValueError, ServiceError) as error:
        print(f"Error: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
