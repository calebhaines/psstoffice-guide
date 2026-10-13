#!/usr/bin/env python3
"""Stream PsstOffice files using Python 3.10+ and the standard library.

Set PSSTOFFICE_ROOM_TOKEN privately. Uploads require a saved client ID for safe
manual retries. Downloads require the size and SHA-256 from room metadata, and
are published only after verification. Files are never opened or executed.
Trusted HTTP CONNECT proxies and verified HTTPS use the message client's rules.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import stat
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from agent import HOST, ServiceError, resolve_proxy, room_token_from_env

CHUNK_BYTES = 64 * 1024
MAX_FILE_BYTES = 500_000_000
MAX_JSON_BYTES = 1024 * 1024
UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _connection(proxy: str | None, direct: bool) -> http.client.HTTPSConnection:
    address = resolve_proxy(proxy, direct=direct)
    connection = http.client.HTTPSConnection(
        address[0] if address else HOST,
        port=address[1] if address else 443,
        timeout=30,
        context=ssl.create_default_context(),
    )
    if address:
        # No room headers are sent with CONNECT. TLS then verifies the origin.
        connection.set_tunnel(HOST, 443)
    return connection


def _headers(token: str) -> dict[str, str]:
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9]{64}", token) is None:
        raise ValueError("Invalid room token.")
    return {
        "X-Room-Token": token,
        "Accept": "application/json",
        "User-Agent": "psstoffice-guide-files/1",
    }


def _encoded_header(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _filename(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 255
        or value in {".", ".."}
        or any(character in value for character in "/\\")
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
        )
        or len(value.encode("utf-8")) > 1024
    ):
        raise ValueError(
            "Filename must be a valid UTF-8 basename, without controls or paths."
        )


def _size(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_FILE_BYTES:
        raise ValueError("File size must be between 0 and 500,000,000 bytes (500 MB).")


def _digest(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("SHA-256 must be 64 lowercase hexadecimal characters.")


def _json_response(response: http.client.HTTPResponse, expected: int) -> dict[str, Any]:
    data = response.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ServiceError("The API response was unexpectedly large.")
    if response.status != expected:
        code = "request_failed"
        try:
            candidate = json.loads(data).get("error", {}).get("code", "")
            if isinstance(candidate, str) and re.fullmatch(r"[a-z_]{1,64}", candidate):
                code = candidate
        except (ValueError, AttributeError, TypeError):
            pass
        reason = f"HTTP {response.status} ({code})."
        if code == "file_space_full":
            reason += " File space full."
        retry = response.getheader("Retry-After", "")
        if isinstance(retry, str) and re.fullmatch(r"[0-9]{1,9}", retry):
            reason += f" Retry-After: {retry} seconds."
        raise ServiceError(reason)
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise ServiceError("The API returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise ServiceError("The API returned an unexpected response.")
    return result


def _network_error(error: Exception, sent: bool, upload_request: bool) -> ServiceError:
    if isinstance(error, ssl.SSLCertVerificationError):
        reason = "HTTPS certificate verification failed."
    elif isinstance(error, ssl.SSLError):
        reason = "The HTTPS handshake failed."
    elif isinstance(error, socket.gaierror):
        reason = "DNS lookup failed for the service or configured proxy."
    elif isinstance(error, TimeoutError):
        reason = "The HTTPS connection or configured proxy timed out."
    else:
        reason = "The HTTPS connection or configured proxy failed."
    if not sent:
        reason += " The API request was not sent."
    elif upload_request:
        reason += (
            " Upload acceptance is uncertain; retry only with the same client ID, "
            "file bytes, filename, and username before its original two-hour expiry."
        )
    return ServiceError(reason)


def upload(
    path: str | Path,
    *,
    room_token: str,
    client_file_id: str,
    username: str | None = None,
    proxy: str | None = None,
    direct: bool = False,
) -> dict[str, Any]:
    """Send raw file chunks, then verify the returned SHA-256 and size.

    No automatic retry: retain the client ID, unchanged file, and labels yourself.
    """
    headers = _headers(room_token)
    if (
        not isinstance(client_file_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", client_file_id) is None
    ):
        raise ValueError("client_file_id must be 32 lowercase hexadecimal characters.")
    source_path = Path(path)
    _filename(source_path.name)
    if username is not None:
        if not isinstance(username, str) or not 1 <= len(username) <= 64:
            raise ValueError("Username must contain 1–64 Unicode code points.")
        try:
            username.encode("utf-8")
        except UnicodeError:
            raise ValueError("Username must be valid UTF-8.") from None
        headers["X-File-Username"] = _encoded_header(username)
    headers.update(
        {
            "Content-Type": "application/octet-stream",
            "X-File-Name": _encoded_header(source_path.name),
            "X-Client-File-ID": client_file_id,
        }
    )
    try:
        # Nonblocking open prevents an accidentally chosen FIFO from hanging.
        descriptor = os.open(source_path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        source = os.fdopen(descriptor, "rb")
    except OSError:
        raise ValueError("Cannot open the chosen upload file.") from None
    with source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Choose a regular file for upload.")
        size = metadata.st_size
        _size(size)
        headers["Content-Length"] = str(size)
        headers["X-File-Size"] = str(size)
        connection = _connection(proxy, direct)
        sent = False
        try:
            connection.connect()
            sent = True
            connection.putrequest("POST", "/v1/files/upload")
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.endheaders()
            digest = hashlib.sha256()
            remaining = size
            try:
                while remaining:
                    chunk = source.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ServiceError(
                            "Upload file changed; acceptance is uncertain. Keep the original retry ID."
                        )
                    connection.send(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            except OSError:
                # An admission rejection can arrive while the client is sending.
                response = connection.getresponse()
                if response.status != 201:
                    _json_response(response, 201)
                raise
            if source.read(1):
                raise ServiceError(
                    "Upload file changed; acceptance is uncertain. Keep the original retry ID."
                )
            receipt = _json_response(connection.getresponse(), 201)
            if (
                type(receipt.get("size_bytes")) is not int
                or receipt["size_bytes"] != size
                or receipt.get("sha256") != digest.hexdigest()
                or receipt.get("filename") != source_path.name
                or receipt.get("username") != username
                or not isinstance(receipt.get("id"), str)
                or re.fullmatch(UUID_PATTERN, receipt["id"]) is None
            ):
                raise ServiceError(
                    "Upload receipt did not match the sent file. Keep the original retry ID."
                )
            return receipt
        except (OSError, http.client.HTTPException) as error:
            raise _network_error(error, sent, True) from None
        finally:
            connection.close()


def read(
    *,
    room_token: str,
    after: str | None = None,
    limit: int = 50,
    proxy: str | None = None,
    direct: bool = False,
) -> dict[str, Any]:
    """Fetch one ordered page of live file metadata."""
    headers = _headers(room_token)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Limit must be between 1 and 100.")
    body: dict[str, Any] = {"limit": limit}
    if after is not None:
        if not isinstance(after, str) or not 1 <= len(after) <= 2048:
            raise ValueError("File cursor must contain 1–2048 characters.")
        body["after"] = after
    headers["Content-Type"] = "application/json"
    connection = _connection(proxy, direct)
    sent = False
    try:
        connection.connect()
        sent = True
        connection.request(
            "POST", "/v1/files/read", json.dumps(body).encode("utf-8"), headers
        )
        return _json_response(connection.getresponse(), 200)
    except (OSError, http.client.HTTPException) as error:
        raise _network_error(error, sent, False) from None
    finally:
        connection.close()


def download(
    file_id: str,
    output: str | Path,
    *,
    room_token: str,
    size_bytes: int,
    sha256: str,
    proxy: str | None = None,
    direct: bool = False,
) -> dict[str, Any]:
    """Stream to a private temporary file, verify, then publish without overwrite.

    The chosen output name comes from the caller, never from a remote filename.
    The temporary file is removed on any failure. Existing targets are preserved.
    """
    headers = _headers(room_token)
    if not isinstance(file_id, str) or re.fullmatch(UUID_PATTERN, file_id) is None:
        raise ValueError("File ID must be a canonical lowercase UUID.")
    _size(size_bytes)
    _digest(sha256)
    target = Path(output)
    if os.path.lexists(target):
        raise ValueError("Download destination already exists; choose a new path.")
    headers["Content-Type"] = "application/json"
    connection = _connection(proxy, direct)
    sent = False
    temporary_path: Path | None = None
    try:
        connection.connect()
        sent = True
        connection.request(
            "POST",
            "/v1/files/download",
            json.dumps({"file_id": file_id}).encode("utf-8"),
            headers,
        )
        response = connection.getresponse()
        if response.status != 200:
            _json_response(response, 200)
        if (
            response.getheader("Content-Type", "").split(";", 1)[0].lower()
            != "application/octet-stream"
        ):
            raise ServiceError("The API returned an unexpected download format.")
        declared_length = response.getheader("Content-Length")
        if declared_length is not None and declared_length != str(size_bytes):
            raise ServiceError("Download length does not match room metadata.")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".psstoffice-", dir=target.parent
        )
        temporary_path = Path(temporary_name)
        digest = hashlib.sha256()
        received = 0
        with os.fdopen(descriptor, "wb") as destination:
            while True:
                chunk = response.read(min(CHUNK_BYTES, size_bytes - received + 1))
                if not chunk:
                    break
                received += len(chunk)
                if received > size_bytes:
                    raise ServiceError("Download exceeded the expected size.")
                digest.update(chunk)
                destination.write(chunk)
            if received != size_bytes or digest.hexdigest() != sha256:
                raise ServiceError(
                    "Download size or SHA-256 did not match room metadata."
                )
            destination.flush()
            os.fsync(destination.fileno())
        # Publish atomically without clobbering an existing file or symlink.
        os.link(temporary_path, target)
        return {"size_bytes": received, "sha256": digest.hexdigest(), "saved": True}
    except FileExistsError:
        raise ServiceError(
            "Download destination appeared during transfer; it was preserved."
        ) from None
    except (OSError, http.client.HTTPException) as error:
        raise _network_error(error, sent, False) from None
    finally:
        connection.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


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
    upload_parser = commands.add_parser(
        "upload", help="stream one file (temporarily up to 500 MB)"
    )
    upload_parser.add_argument("path", type=Path)
    upload_parser.add_argument(
        "--client-id",
        required=True,
        help="saved 32-character lowercase hexadecimal retry ID",
    )
    upload_parser.add_argument("--username", help="optional unverified uploader label")
    read_parser = commands.add_parser(
        "read", help="list one page of live room file metadata"
    )
    read_parser.add_argument("--after", help="next_cursor from a previous file read")
    read_parser.add_argument("--limit", type=int, default=50)
    download_parser = commands.add_parser(
        "download", help="verify and save opaque bytes without overwriting"
    )
    download_parser.add_argument(
        "file_id", help="UUID from a room file receipt or listing"
    )
    download_parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="new local destination path chosen by you",
    )
    download_parser.add_argument(
        "--size", required=True, type=int, help="size_bytes from room metadata"
    )
    download_parser.add_argument(
        "--sha256", required=True, help="SHA-256 from room metadata"
    )
    args = parser.parse_args()
    try:
        network_options = {"proxy": args.proxy, "direct": args.direct}
        token = room_token_from_env()
        if args.command == "upload":
            result = upload(
                args.path,
                room_token=token,
                client_file_id=args.client_id,
                username=args.username,
                **network_options,
            )
        elif args.command == "read":
            result = read(
                room_token=token, after=args.after, limit=args.limit, **network_options
            )
        else:
            result = download(
                args.file_id,
                args.output,
                room_token=token,
                size_bytes=args.size,
                sha256=args.sha256,
                **network_options,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, ServiceError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
