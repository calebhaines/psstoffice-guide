"""Offline transport checks; no room tokens or message contents are printed."""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, sentinel

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import agent


class ProxySelectionTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_default_is_direct_without_proxy_settings(self):
        self.assertIsNone(agent.resolve_proxy())

    def test_standard_https_proxy_and_lowercase_precedence(self):
        os.environ["HTTPS_PROXY"] = "http://upper.invalid:8080"
        self.assertEqual(agent.resolve_proxy(), ("upper.invalid", 8080))
        os.environ["https_proxy"] = "http://lower.invalid:3128"
        self.assertEqual(agent.resolve_proxy(), ("lower.invalid", 3128))

    def test_no_proxy_bypasses_default_proxy(self):
        os.environ["HTTPS_PROXY"] = "http://proxy.invalid:8080"
        for value in (agent.HOST, ".nip.io", "*"):
            with self.subTest(no_proxy=value):
                os.environ["NO_PROXY"] = value
                self.assertIsNone(agent.resolve_proxy())

    def test_explicit_proxy_and_psstoffice_setting_override_no_proxy(self):
        os.environ.update(
            HTTPS_PROXY="http://default.invalid:8080",
            PSSTOFFICE_PROXY="http://explicit.invalid:3128",
            NO_PROXY="*",
        )
        self.assertEqual(agent.resolve_proxy(), ("explicit.invalid", 3128))
        self.assertEqual(
            agent.resolve_proxy("http://argument.invalid"), ("argument.invalid", 80)
        )

    def test_explicit_empty_setting_and_direct_bypass_proxies(self):
        os.environ["HTTPS_PROXY"] = "http://proxy.invalid:8080"
        os.environ["PSSTOFFICE_PROXY"] = ""
        self.assertIsNone(agent.resolve_proxy())
        self.assertIsNone(agent.resolve_proxy(""))
        os.environ["PSSTOFFICE_PROXY"] = "malformed"
        self.assertIsNone(agent.resolve_proxy(direct=True))
        with self.assertRaises(ValueError):
            agent.resolve_proxy("http://proxy.invalid", direct=True)

    def test_proxy_validation_rejects_unsafe_urls(self):
        for value in (
            "https://proxy.invalid",
            "socks5://proxy.invalid",
            "proxy.invalid:8080",
            "http://user:password@proxy.invalid",
            "http://@proxy.invalid",
            "http://proxy.invalid/path",
            "http://proxy.invalid?secret",
            "http://proxy.invalid#secret",
            "http://proxy.invalid?",
            "http://proxy.invalid#",
            "http://proxy.invalid:",
            "http://proxy.invalid:0",
            "http://proxy.invalid:65536",
            "http://proxy.invalid:bad",
            "http://",
            " http://proxy.invalid",
            "http://proxy.invalid\n",
            "http://proxy.invalid\t",
            "http://[broken",
        ):
            with self.subTest(case=value):
                with self.assertRaises(ValueError) as error:
                    agent.resolve_proxy(value)
                self.assertEqual(
                    str(error.exception),
                    "Proxy must be http://host[:port], without credentials, path, "
                    "query, fragment, or whitespace. Use --direct to bypass proxy settings.",
                )

    def test_ipv6_proxy_is_supported(self):
        self.assertEqual(agent.resolve_proxy("http://[::1]:8080/"), ("::1", 8080))


class TransportTests(unittest.TestCase):
    def connection(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"status":"ok"}'
        response.getheader.return_value = ""
        return connection

    def test_direct_connection_has_verified_tls_and_no_tunnel(self):
        connection = self.connection()
        with patch(
            "agent.http.client.HTTPSConnection", return_value=connection
        ) as factory:
            self.assertEqual(agent.request("/healthz", direct=True), {"status": "ok"})
        args, options = factory.call_args
        self.assertEqual(args, (agent.HOST,))
        self.assertEqual(options["port"], 443)
        self.assertEqual(options["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(options["context"].check_hostname)
        connection.set_tunnel.assert_not_called()
        connection.close.assert_called_once()

    def test_proxy_connection_tunnels_to_origin_without_room_headers(self):
        connection = self.connection()
        response = connection.getresponse.return_value
        response.status = 201
        response.read.return_value = b'{"id":"example"}'
        token = "a" * 64
        with patch(
            "agent.http.client.HTTPSConnection", return_value=connection
        ) as factory:
            agent.request(
                "/v1/messages",
                {"message": "example"},
                room_token=token,
                proxy="http://proxy.invalid:8080",
            )
        self.assertEqual(factory.call_args.args, ("proxy.invalid",))
        self.assertEqual(factory.call_args.kwargs["port"], 8080)
        connection.set_tunnel.assert_called_once_with(agent.HOST, 443)
        self.assertEqual(connection.request.call_args.args[3]["X-Room-Token"], token)

    def test_stdlib_tunnel_wraps_tls_for_origin_hostname(self):
        context = MagicMock(spec=ssl.SSLContext)
        connection = http.client.HTTPSConnection("proxy.invalid", 8080, context=context)
        connection.set_tunnel(agent.HOST, 443)
        connection.sock = sentinel.tunnel_socket
        with patch.object(http.client.HTTPConnection, "connect"):
            connection.connect()
        context.wrap_socket.assert_called_once_with(
            sentinel.tunnel_socket, server_hostname=agent.HOST
        )

    def test_certificate_failure_prevents_sending(self):
        connection = self.connection()
        connection.connect.side_effect = ssl.SSLCertVerificationError("private detail")
        with (
            patch("agent.http.client.HTTPSConnection", return_value=connection),
            self.assertRaisesRegex(
                agent.ServiceError, "certificate verification"
            ) as error,
        ):
            agent.request("/v1/messages", {"message": "private"}, direct=True)
        connection.request.assert_not_called()
        self.assertIn("request was not sent", str(error.exception))
        self.assertNotIn("private", str(error.exception))

    def test_distinct_connection_errors_are_safe(self):
        for failure, text in (
            (ConnectionRefusedError("private detail"), "configured proxy refused"),
            (socket.gaierror("private detail"), "DNS lookup failed"),
            (TimeoutError("private detail"), "timed out"),
            (ssl.SSLError("private detail"), "HTTPS handshake failed"),
        ):
            with self.subTest(kind=type(failure).__name__):
                connection = self.connection()
                connection.connect.side_effect = failure
                with (
                    patch("agent.http.client.HTTPSConnection", return_value=connection),
                    self.assertRaisesRegex(agent.ServiceError, text) as error,
                ):
                    agent.request("/healthz", proxy="http://proxy.invalid")
                self.assertNotIn("private detail", str(error.exception))
                connection.request.assert_not_called()

    def test_send_failure_after_transmission_preserves_retry_advice(self):
        connection = self.connection()
        connection.request.side_effect = ConnectionResetError("private detail")
        with (
            patch("agent.http.client.HTTPSConnection", return_value=connection),
            self.assertRaises(agent.ServiceError) as error,
        ):
            agent.request("/v1/messages", {"message": "example"}, direct=True)
        self.assertIn("same client ID", str(error.exception))
        self.assertNotIn("request was not sent", str(error.exception))
        self.assertEqual(connection.request.call_count, 1)

    def test_redirect_is_rejected_without_following(self):
        connection = self.connection()
        connection.getresponse.return_value.status = 307
        connection.getresponse.return_value.read.return_value = b""
        with (
            patch(
                "agent.http.client.HTTPSConnection", return_value=connection
            ) as factory,
            self.assertRaisesRegex(agent.ServiceError, "HTTP 307"),
        ):
            agent.request("/healthz", direct=True)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(connection.request.call_count, 1)

    def test_rejecting_proxy_receives_no_room_token_or_message(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        captured = []
        failures = []

        def reject_tunnel():
            try:
                with listener.accept()[0] as peer:
                    peer.settimeout(3)
                    data = b""
                    while b"\r\n\r\n" not in data and len(data) < 16384:
                        chunk = peer.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    captured.append(data)
                    peer.sendall(
                        b"HTTP/1.1 407 private-peer-detail\r\nContent-Length: 0\r\n\r\n"
                    )
            except OSError as error:
                failures.append(type(error).__name__)

        worker = threading.Thread(target=reject_tunnel, daemon=True)
        worker.start()
        token = "z" * 64
        message = "private-message-sentinel"
        with self.assertRaisesRegex(agent.ServiceError, "proxy tunnel") as error:
            agent.request(
                "/v1/messages",
                {"message": message},
                room_token=token,
                proxy=f"http://127.0.0.1:{port}",
            )
        worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0].startswith(f"CONNECT {agent.HOST}:443 ".encode()))
        self.assertNotIn(b"X-Room-Token", captured[0])
        self.assertNotIn(token.encode(), captured[0])
        self.assertNotIn(message.encode(), captured[0])
        self.assertNotIn("private-peer-detail", str(error.exception))


if __name__ == "__main__":
    unittest.main()
