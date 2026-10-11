#!/usr/bin/env python3
"""A tiny PsstOffice client. Python 3.10+, standard library only.

Room tokens come from PSSTOFFICE_ROOM_TOKEN, never a command-line argument.
Connections use verified HTTPS, without redirects. Trusted HTTP CONNECT proxies
can be set with --proxy or PSSTOFFICE_PROXY; otherwise https_proxy/HTTPS_PROXY
and NO_PROXY are respected. Use --direct to bypass all proxy settings.
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
import socket
import ssl
import sys
from typing import Any
from urllib.parse import urlsplit
from urllib.request import proxy_bypass

HOST = "psstoffice.89b84f87.nip.io"
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


def resolve_proxy(
    proxy: str | None = None, *, direct: bool = False
) -> tuple[str, int] | None:
    """Resolve a trusted HTTP CONNECT proxy; an empty explicit value means direct."""
    if direct:
        if proxy is not None:
            raise ValueError("Choose either --direct or --proxy.")
        return None
    if proxy is None:
        if "PSSTOFFICE_PROXY" in os.environ:
            proxy = os.environ["PSSTOFFICE_PROXY"]
        else:
            proxy = os.environ.get("https_proxy", os.environ.get("HTTPS_PROXY", ""))
            if proxy and proxy_bypass(HOST):
                return None
    if not proxy:
        return None
    try:
        if any(ord(character) <= 32 or ord(character) == 127 for character in proxy):
            raise ValueError
        parsed = urlsplit(proxy)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or "?" in proxy
            or "#" in proxy
            or parsed.netloc.endswith(":")
        ):
            raise ValueError
        port = parsed.port if parsed.port is not None else 80
        if not 1 <= port <= 65535:
            raise ValueError
        return parsed.hostname, port
    except ValueError:
        raise ValueError(
            "Proxy must be http://host[:port], without credentials, path, "
            "query, fragment, or whitespace. Use --direct to bypass proxy settings."
        ) from None


def request(
    path: str,
    body: dict[str, Any] | None = None,
    *,
    room_token: str | None = None,
    wait_seconds: float = 0,
    proxy: str | None = None,
    direct: bool = False,
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

    proxy_address = resolve_proxy(proxy, direct=direct)
    connection = http.client.HTTPSConnection(
        proxy_address[0] if proxy_address else HOST,
        port=proxy_address[1] if proxy_address else 443,
        timeout=10,
        context=ssl.create_default_context(),
    )
    if proxy_address:
        # CONNECT carries no room headers; HTTPS verifies the origin after tunneling.
        connection.set_tunnel(HOST, 443)
    request_started = False

    def connection_failure(reason: str) -> ServiceError:
        if not request_started:
            reason += " The API request was not sent."
        elif path == "/v1/messages":
            reason += (
                " A send might already have been accepted; retry with the same "
                "client ID, message, and username."
            )
        return ServiceError(reason)

    try:
        # Connect separately so long polling extends only the response timeout.
        connection.connect()
        request_started = True
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
    except ssl.SSLCertVerificationError:
        raise connection_failure("HTTPS certificate verification failed.") from None
    except ssl.SSLError:
        raise connection_failure("The HTTPS handshake failed.") from None
    except ConnectionRefusedError:
        peer = "configured proxy" if proxy_address else "service"
        raise connection_failure(f"The {peer} refused the connection.") from None
    except socket.gaierror:
        peer = "configured proxy" if proxy_address else "service"
        raise connection_failure(f"DNS lookup failed for the {peer}.") from None
    except TimeoutError:
        route = " through the configured proxy" if proxy_address else ""
        raise connection_failure(f"The HTTPS connection{route} timed out.") from None
    except (OSError, http.client.HTTPException):
        route = (
            "proxy tunnel or HTTPS connection" if proxy_address else "HTTPS connection"
        )
        raise connection_failure(f"The {route} failed.") from None
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    network = parser.add_mutually_exclusive_group()
    network.add_argument(
        "--proxy", help="trusted HTTP CONNECT proxy: http://host[:port]"
    )
    network.add_argument(
        "--direct", action="store_true", help="bypass all proxy settings"
    )
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
    network_options = {"proxy": args.proxy, "direct": args.direct}

    try:
        if args.command == "token":
            print(secrets.token_hex(32))
            return 0
        if args.command == "health":
            result = request("/healthz", **network_options)
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
                result = request(
                    "/v1/messages", body, room_token=token, **network_options
                )
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
                    "/v1/messages/read",
                    body,
                    room_token=token,
                    wait_seconds=args.wait,
                    **network_options,
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
