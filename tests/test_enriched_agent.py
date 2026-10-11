"""Offline checks for typed notes and room-local reply requests."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import agent

TOKEN = "a" * 64
CLIENT_ID = "b" * 32
PARENT_ID = "12345678-1234-1234-1234-123456789abc"


class EnrichedSendTests(unittest.TestCase):
    def send(self, **fields):
        return agent.send(
            "The moon inventory is ready. 🌙",
            room_token=TOKEN,
            client_message_id=CLIENT_ID,
            **fields,
        )

    def test_plain_send_keeps_original_wire_shape(self):
        with patch("agent.request", return_value={"id": PARENT_ID}) as request:
            self.assertEqual(self.send(), {"id": PARENT_ID})
        self.assertEqual(
            request.call_args.args,
            (
                "/v1/messages",
                {
                    "message": "The moon inventory is ready. 🌙",
                    "client_message_id": CLIENT_ID,
                },
            ),
        )
        self.assertEqual(request.call_args.kwargs["room_token"], TOKEN)

    def test_all_types_can_accompany_a_reply_without_changing_text(self):
        for message_type in agent.MESSAGE_TYPES:
            with (
                self.subTest(message_type=message_type),
                patch(
                    "agent.request", return_value={"message_type": message_type}
                ) as request,
            ):
                self.send(
                    username="worker 🌙",
                    message_type=message_type,
                    reply_to=PARENT_ID,
                    proxy="http://proxy.invalid:8080",
                )
                body = request.call_args.args[1]
                self.assertEqual(body["message_type"], message_type)
                self.assertEqual(body["reply_to"], PARENT_ID)
                self.assertEqual(body["username"], "worker 🌙")
                self.assertEqual(body["message"], "The moon inventory is ready. 🌙")
                self.assertEqual(
                    request.call_args.kwargs["proxy"], "http://proxy.invalid:8080"
                )

    def test_reply_and_type_work_independently(self):
        with patch("agent.request") as request:
            self.send(reply_to=PARENT_ID)
            self.assertNotIn("message_type", request.call_args.args[1])
            self.send(message_type="question")
            self.assertNotIn("reply_to", request.call_args.args[1])

    def test_repeated_explicit_send_preserves_every_retry_field(self):
        with patch(
            "agent.request", side_effect=[agent.ServiceError("timeout"), {}]
        ) as request:
            fields = {
                "username": "worker",
                "message_type": "result",
                "reply_to": PARENT_ID,
            }
            with self.assertRaises(agent.ServiceError):
                self.send(**fields)
            self.send(**fields)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0], request.call_args_list[1])

    def test_invalid_types_never_send_a_request(self):
        for value in ("plain", "Task", "unknown", "", True, [], {}):
            with self.subTest(value=value), patch("agent.request") as request:
                with self.assertRaisesRegex(ValueError, "message_type"):
                    self.send(message_type=value)
                request.assert_not_called()

    def test_noncanonical_reply_ids_never_send_a_request(self):
        for value in (
            PARENT_ID.upper(),
            PARENT_ID.replace("-", ""),
            "{" + PARENT_ID + "}",
            "urn:uuid:" + PARENT_ID,
            PARENT_ID + "\n",
            "not-a-uuid",
            "",
            True,
            [],
        ):
            with self.subTest(value=value), patch("agent.request") as request:
                with self.assertRaisesRegex(ValueError, "reply_to"):
                    self.send(reply_to=value)
                request.assert_not_called()

    def test_invalid_payload_and_retry_id_fail_before_network(self):
        cases = (
            {"message": ""},
            {"message": "x" * 16385},
            {"message": "\ud800"},
            {"message": True},
            {"username": ""},
            {"username": "x" * 65},
            {"username": "\ud800"},
            {"username": True},
            {"client_message_id": CLIENT_ID.upper()},
            {"client_message_id": True},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), patch("agent.request") as request:
                fields = {
                    "message": "hello",
                    "room_token": TOKEN,
                    "client_message_id": CLIENT_ID,
                    "message_type": "task",
                    "reply_to": PARENT_ID,
                    **overrides,
                }
                with self.assertRaises(ValueError):
                    agent.send(**fields)
                request.assert_not_called()

    def test_limits_count_utf8_bytes_and_unicode_name_characters(self):
        with patch("agent.request") as request:
            agent.send(
                "🌙" * 4096,
                room_token=TOKEN,
                client_message_id=CLIENT_ID,
                username="🌙" * 64,
                message_type="result",
            )
            self.assertEqual(len(request.call_args.args[1]["message"].encode()), 16384)
            with self.assertRaises(ValueError):
                agent.send(
                    "🌙" * 4097,
                    room_token=TOKEN,
                    client_message_id=CLIENT_ID,
                )
            self.assertEqual(request.call_count, 1)


class EnrichedCliTests(unittest.TestCase):
    def run_command(self, arguments, *, receipt=None):
        output, errors = io.StringIO(), io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO("Inventory complete. 🌙".encode()))
        with (
            patch.dict(os.environ, {"PSSTOFFICE_ROOM_TOKEN": TOKEN}, clear=True),
            patch.object(sys, "argv", ["agent.py", *arguments]),
            patch.object(sys, "stdin", stdin),
            patch("agent.request", return_value=receipt or {}) as request,
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            try:
                result = agent.main()
            except SystemExit as error:
                result = error.code
        return result, output.getvalue(), errors.getvalue(), request

    def test_cli_sends_type_and_reply_and_prints_enriched_receipt(self):
        receipt = {"id": PARENT_ID, "message_type": "result", "reply_to": PARENT_ID}
        result, output, errors, request = self.run_command(
            [
                "--direct",
                "send",
                "--client-id",
                CLIENT_ID,
                "--type",
                "result",
                "--reply-to",
                PARENT_ID,
            ],
            receipt=receipt,
        )
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output), receipt)
        self.assertEqual(errors, "")
        self.assertEqual(
            request.call_args.args[1],
            {
                "message": "Inventory complete. 🌙",
                "client_message_id": CLIENT_ID,
                "message_type": "result",
                "reply_to": PARENT_ID,
            },
        )
        self.assertTrue(request.call_args.kwargs["direct"])
        self.assertNotIn(TOKEN, output)

    def test_cli_rejects_unknown_type_and_noncanonical_reply_without_network(self):
        for suffix, expected_exit in (
            (["--type", "unknown"], 2),
            (["--reply-to", PARENT_ID.upper()], 1),
        ):
            with self.subTest(suffix=suffix):
                result, output, errors, request = self.run_command(
                    ["send", "--client-id", CLIENT_ID, *suffix]
                )
                self.assertEqual(result, expected_exit)
                self.assertEqual(output, "")
                self.assertNotIn(TOKEN, errors)
                request.assert_not_called()

    def test_send_help_explains_type_and_live_room_reply(self):
        result, output, errors, request = self.run_command(["send", "--help"])
        self.assertEqual(result, 0)
        self.assertIn("--type {task,result,question,error}", output)
        self.assertIn("--reply-to", output)
        self.assertIn("live message in this room", " ".join(output.split()))
        self.assertEqual(errors, "")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
