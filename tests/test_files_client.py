"""Offline streaming and integrity checks; never contact the live service."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import files

TOKEN = "b" * 64
FILE_ID = "00000000-0000-4000-8000-000000000001"
CLIENT_ID = "c" * 32


class Response:
    def __init__(self, data: bytes, *, status: int = 200, headers=None):
        self.status = status
        self.stream = io.BytesIO(data)
        self.headers = headers or {}
        self.read_sizes = []

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            raise AssertionError("Unbounded response read")
        return self.stream.read(size)

    def getheader(self, name: str, default=None):
        return self.headers.get(name, default)


class FileClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def connection(self, response):
        connection = MagicMock()
        connection.getresponse.return_value = response
        return connection

    def receipt(self, content, filename, username=None):
        return {
            "id": FILE_ID,
            "filename": filename,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "username": username,
            "created_at": "2026-10-13T10:00:00Z",
            "expires_at": "2026-10-13T12:00:00Z",
        }

    def upload(self, content, *, filename="parcel.bin", username=None, receipt=None):
        path = self.root / filename
        path.write_bytes(content)
        expected = receipt or self.receipt(content, filename, username)
        connection = self.connection(
            Response(json.dumps(expected).encode(), status=201)
        )
        with patch("files.http.client.HTTPSConnection", return_value=connection):
            actual = files.upload(
                path,
                room_token=TOKEN,
                client_file_id=CLIENT_ID,
                username=username,
                direct=True,
            )
        return actual, connection

    def test_upload_sends_raw_bounded_chunks_with_encoded_unicode_headers(self):
        content = b"a" * (3 * files.CHUNK_BYTES + 13)
        result, connection = self.upload(
            content, filename="moon-🌒.bin", username="agent 🌒"
        )
        self.assertEqual(result["sha256"], hashlib.sha256(content).hexdigest())
        connection.putrequest.assert_called_once_with("POST", "/v1/files/upload")
        headers = dict(call.args for call in connection.putheader.call_args_list)
        self.assertEqual(headers["Content-Length"], str(len(content)))
        self.assertEqual(headers["X-File-Size"], str(len(content)))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(headers["X-Room-Token"], TOKEN)
        self.assertEqual(headers["X-Client-File-ID"], CLIENT_ID)
        self.assertEqual(
            headers["X-File-Name"],
            base64.urlsafe_b64encode("moon-🌒.bin".encode()).decode().rstrip("="),
        )
        self.assertEqual(
            headers["X-File-Username"],
            base64.urlsafe_b64encode("agent 🌒".encode()).decode().rstrip("="),
        )
        sent = [call.args[0] for call in connection.send.call_args_list]
        self.assertEqual(b"".join(sent), content)
        self.assertTrue(all(0 < len(chunk) <= files.CHUNK_BYTES for chunk in sent))
        connection.close.assert_called_once()

    def test_empty_file_does_not_send_a_body_chunk(self):
        receipt, connection = self.upload(b"")
        self.assertEqual(receipt["size_bytes"], 0)
        connection.send.assert_not_called()

    def test_upload_mismatched_receipt_preserves_retry_advice(self):
        content = b"private-file-sentinel"
        receipt = self.receipt(content, "parcel.bin")
        receipt["sha256"] = "0" * 64
        with self.assertRaises(files.ServiceError) as error:
            self.upload(content, receipt=receipt)
        self.assertIn("original retry ID", str(error.exception))
        self.assertNotIn("private-file-sentinel", str(error.exception))

    def test_file_space_full_is_clear_and_does_not_auto_retry(self):
        path = self.root / "parcel.bin"
        path.write_bytes(b"a")
        response = Response(
            b'{"error":{"code":"file_space_full","message":"private-server-detail"}}',
            status=503,
            headers={"Retry-After": "30"},
        )
        connection = self.connection(response)
        with (
            patch("files.http.client.HTTPSConnection", return_value=connection),
            self.assertRaises(files.ServiceError) as error,
        ):
            files.upload(path, room_token=TOKEN, client_file_id=CLIENT_ID, direct=True)
        self.assertEqual(
            str(error.exception),
            "HTTP 503 (file_space_full). File space full. Retry-After: 30 seconds.",
        )
        self.assertNotIn("private-server-detail", str(error.exception))
        connection.getresponse.assert_called_once()

    def test_early_admission_rejection_is_read_after_send_breaks(self):
        path = self.root / "parcel.bin"
        path.write_bytes(b"a" * files.CHUNK_BYTES)
        response = Response(
            b'{"error":{"code":"file_space_full"}}',
            status=503,
            headers={"Retry-After": "30"},
        )
        connection = self.connection(response)
        connection.send.side_effect = BrokenPipeError("private-detail")
        with (
            patch("files.http.client.HTTPSConnection", return_value=connection),
            self.assertRaisesRegex(files.ServiceError, "File space full"),
        ):
            files.upload(path, room_token=TOKEN, client_file_id=CLIENT_ID, direct=True)
        connection.getresponse.assert_called_once()

    def test_certificate_failure_occurs_before_headers_or_file_are_sent(self):
        path = self.root / "parcel.bin"
        path.write_bytes(b"private-file-sentinel")
        connection = self.connection(Response(b""))
        connection.connect.side_effect = ssl.SSLCertVerificationError("private-detail")
        with (
            patch("files.http.client.HTTPSConnection", return_value=connection),
            self.assertRaises(files.ServiceError) as error,
        ):
            files.upload(path, room_token=TOKEN, client_file_id=CLIENT_ID, direct=True)
        self.assertIn("request was not sent", str(error.exception))
        self.assertNotIn("private-detail", str(error.exception))
        connection.putrequest.assert_not_called()
        connection.putheader.assert_not_called()
        connection.send.assert_not_called()

    def test_proxy_tunnel_has_no_token_and_verified_origin_tls(self):
        connection = self.connection(Response(b'{"files":[]}'))
        with patch(
            "files.http.client.HTTPSConnection", return_value=connection
        ) as factory:
            files.read(room_token=TOKEN, proxy="http://proxy.invalid:8080")
        connection.set_tunnel.assert_called_once_with(files.HOST, 443)
        self.assertEqual(factory.call_args.args, ("proxy.invalid",))
        context = factory.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(connection.request.call_args.args[3]["X-Room-Token"], TOKEN)

    def test_redirect_is_rejected_without_following(self):
        connection = self.connection(
            Response(b"", status=307, headers={"Location": "https://evil.invalid"})
        )
        with (
            patch(
                "files.http.client.HTTPSConnection", return_value=connection
            ) as factory,
            self.assertRaisesRegex(files.ServiceError, "HTTP 307"),
        ):
            files.read(room_token=TOKEN, direct=True)
        self.assertEqual(factory.call_count, 1)
        connection.request.assert_called_once()

    def test_upload_failure_after_transmission_is_uncertain_and_never_retries(self):
        path = self.root / "parcel.bin"
        path.write_bytes(b"a")
        connection = self.connection(Response(b""))
        connection.getresponse.side_effect = ConnectionResetError("private-detail")
        with (
            patch("files.http.client.HTTPSConnection", return_value=connection),
            self.assertRaises(files.ServiceError) as error,
        ):
            files.upload(path, room_token=TOKEN, client_file_id=CLIENT_ID, direct=True)
        self.assertIn("same client ID", str(error.exception))
        self.assertIn("two-hour expiry", str(error.exception))
        self.assertNotIn("private-detail", str(error.exception))
        connection.putrequest.assert_called_once()

    def download(self, response, *, size, sha256, target=None):
        target = target or self.root / "chosen-output.bin"
        connection = self.connection(response)
        with patch("files.http.client.HTTPSConnection", return_value=connection):
            result = files.download(
                FILE_ID,
                target,
                room_token=TOKEN,
                size_bytes=size,
                sha256=sha256,
                direct=True,
            )
        return result, target, connection

    def test_download_is_bounded_verified_private_and_uses_chosen_destination(self):
        content = b"x" * (2 * files.CHUNK_BYTES + 1)
        response = Response(
            content,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(content)),
                "Content-Disposition": 'attachment; filename="../../evil"',
            },
        )
        result, path, connection = self.download(
            response, size=len(content), sha256=hashlib.sha256(content).hexdigest()
        )
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(
            result,
            {
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "saved": True,
            },
        )
        self.assertTrue(
            all(0 < size <= files.CHUNK_BYTES for size in response.read_sizes)
        )
        self.assertEqual(list(self.root.glob(".psstoffice-*")), [])
        self.assertEqual(connection.request.call_args.args[1], "/v1/files/download")

    def test_empty_download_verifies_digest(self):
        response = Response(
            b"",
            headers={"Content-Type": "application/octet-stream", "Content-Length": "0"},
        )
        result, path, _ = self.download(
            response, size=0, sha256=hashlib.sha256(b"").hexdigest()
        )
        self.assertEqual(result["size_bytes"], 0)
        self.assertEqual(path.read_bytes(), b"")

    def test_hash_mismatch_removes_temporary_file_and_publishes_nothing(self):
        response = Response(
            b"malicious", headers={"Content-Type": "application/octet-stream"}
        )
        with self.assertRaisesRegex(files.ServiceError, "SHA-256"):
            self.download(response, size=9, sha256="0" * 64)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_oversized_and_truncated_downloads_publish_nothing(self):
        for content, size in ((b"toolong", 2), (b"short", 10)):
            with self.subTest(length=len(content)):
                response = Response(
                    content, headers={"Content-Type": "application/octet-stream"}
                )
                with self.assertRaises(files.ServiceError):
                    self.download(response, size=size, sha256="0" * 64)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_mismatched_declared_length_rejects_before_writing(self):
        response = Response(
            b"a",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": "500000000",
            },
        )
        with self.assertRaisesRegex(files.ServiceError, "length"):
            self.download(response, size=1, sha256=hashlib.sha256(b"a").hexdigest())
        self.assertEqual(response.read_sizes, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_destination_is_preserved_without_network(self):
        target = self.root / "existing.bin"
        target.write_bytes(b"original")
        with (
            patch("files.http.client.HTTPSConnection") as factory,
            self.assertRaisesRegex(ValueError, "already exists"),
        ):
            files.download(
                FILE_ID,
                target,
                room_token=TOKEN,
                size_bytes=1,
                sha256="0" * 64,
                direct=True,
            )
        factory.assert_not_called()
        self.assertEqual(target.read_bytes(), b"original")

    def test_destination_created_during_transfer_is_preserved(self):
        target = self.root / "chosen-output.bin"
        content = b"parcel"
        response = Response(
            content, headers={"Content-Type": "application/octet-stream"}
        )
        original_read = response.read

        def read_and_create(size):
            target.write_bytes(b"original")
            return original_read(size)

        response.read = read_and_create
        with self.assertRaisesRegex(files.ServiceError, "preserved"):
            self.download(
                response,
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                target=target,
            )
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(list(self.root.glob(".psstoffice-*")), [])

    def test_error_response_does_not_create_download_file(self):
        response = Response(b'{"error":{"code":"file_unavailable"}}', status=404)
        with self.assertRaisesRegex(files.ServiceError, "file_unavailable"):
            self.download(response, size=1, sha256="0" * 64)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_metadata_and_header_validation_precedes_network(self):
        path = self.root / "parcel.bin"
        path.write_bytes(b"a")
        for arguments in (
            {"room_token": "secret"},
            {"client_file_id": "not-an-id"},
            {"username": ""},
            {"username": "\ud800"},
        ):
            with self.subTest(arguments=arguments):
                options = {
                    "room_token": TOKEN,
                    "client_file_id": CLIENT_ID,
                    **arguments,
                }
                with (
                    patch("files.http.client.HTTPSConnection") as factory,
                    self.assertRaises(ValueError),
                ):
                    files.upload(path, **options, direct=True)
                factory.assert_not_called()
        for size in (-1, 500_000_001, True):
            with (
                self.subTest(size=size),
                patch("files.http.client.HTTPSConnection") as factory,
                self.assertRaises(ValueError),
            ):
                files.download(
                    FILE_ID,
                    self.root / "out",
                    room_token=TOKEN,
                    size_bytes=size,
                    sha256="0" * 64,
                    direct=True,
                )
            factory.assert_not_called()

    def test_oversized_sparse_upload_is_rejected_before_network_or_body_read(self):
        path = self.root / "oversized.bin"
        with path.open("wb") as source:
            source.truncate(500_000_001)
        with (
            patch("files.http.client.HTTPSConnection") as factory,
            self.assertRaisesRegex(ValueError, "500,000,000"),
        ):
            files.upload(path, room_token=TOKEN, client_file_id=CLIENT_ID, direct=True)
        factory.assert_not_called()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires a Unix filesystem")
    def test_fifo_upload_is_rejected_without_blocking_or_network(self):
        path = self.root / "named-pipe"
        os.mkfifo(path)
        with (
            patch("files.http.client.HTTPSConnection") as factory,
            self.assertRaisesRegex(ValueError, "regular file"),
        ):
            files.upload(path, room_token=TOKEN, client_file_id=CLIENT_ID, direct=True)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
