import unittest

from mcp_stdio_mux.router import MessageRouter, parse_message
from tests.helpers import (
    initialize_params,
    initialize_result,
    notification,
    request,
    response,
)


class MessageRouterTests(unittest.TestCase):
    def setUp(self):
        self.router = MessageRouter()

    def test_duplicate_client_request_ids_are_namespaced_and_restored(self):
        first = self.router.route_client("first", request(7, "tools/list"))
        second = self.router.route_client("second", request(7, "tools/list"))

        first_backend_id = first[0][1]["id"]
        second_backend_id = second[0][1]["id"]
        self.assertNotEqual(first_backend_id, second_backend_id)
        self.assertEqual(
            self.router.route_backend(response(second_backend_id, {"tools": []})),
            [("second", response(7, {"tools": []}))],
        )
        self.assertEqual(
            self.router.route_backend(response(first_backend_id, {"tools": []})),
            [("first", response(7, {"tools": []}))],
        )

    def test_initialize_reaches_backend_once_and_is_cached_for_later_clients(self):
        initialize = request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"roots": {"listChanged": True}},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        first = self.router.route_client("first", initialize)
        backend_id = first[0][1]["id"]
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }

        self.assertEqual(
            self.router.route_backend(response(backend_id, result)),
            [("first", response(1, result))],
        )
        self.assertEqual(
            self.router.route_client(
                "first", notification("notifications/initialized")
            ),
            [("backend", notification("notifications/initialized"))],
        )
        second_initialize = {**initialize, "id": 99}
        self.assertEqual(
            self.router.route_client("second", second_initialize),
            [("second", response(99, result))],
        )
        self.assertEqual(
            self.router.route_client(
                "second", notification("notifications/initialized")
            ),
            [],
        )

    def test_waiting_initializer_survives_first_client_disconnect(self):
        first = self.router.route_client(
            "first", request(1, "initialize", initialize_params({}))
        )
        backend_id = first[0][1]["id"]
        self.assertEqual(
            self.router.route_client(
                "second", request(2, "initialize", initialize_params({}))
            ),
            [],
        )
        self.router.remove_client("first")

        self.assertEqual(
            self.router.route_backend(response(backend_id, initialize_result())),
            [("second", response(2, initialize_result()))],
        )

    def test_progress_tokens_are_namespaced_and_restored(self):
        outbound = request(
            3,
            "tools/call",
            {"name": "slow", "arguments": {}, "_meta": {"progressToken": 5}},
        )
        backend_request = self.router.route_client("first", outbound)[0][1]
        backend_token = backend_request["params"]["_meta"]["progressToken"]

        deliveries = self.router.route_backend(
            notification(
                "notifications/progress",
                {"progressToken": backend_token, "progress": 1, "total": 2},
            )
        )

        self.assertEqual(
            deliveries,
            [
                (
                    "first",
                    notification(
                        "notifications/progress",
                        {"progressToken": 5, "progress": 1, "total": 2},
                    ),
                )
            ],
        )

    def test_server_request_uses_client_with_required_capability(self):
        backend_id = self.router.route_client(
            "rooted",
            request(2, "initialize", initialize_params({"roots": {}})),
        )[0][1]["id"]
        self.router.route_backend(response(backend_id, initialize_result()))
        self.router.route_client(
            "plain", request(1, "initialize", initialize_params({}))
        )

        deliveries = self.router.route_backend(request(8, "roots/list"))
        proxy_id = deliveries[0][1]["id"]

        self.assertEqual(deliveries[0][0], "rooted")
        self.assertEqual(
            self.router.route_client("rooted", response(proxy_id, {"roots": []})),
            [("backend", response(8, {"roots": []}))],
        )

    def test_server_request_without_capable_client_returns_error(self):
        self.router.route_client(
            "plain", request(1, "initialize", initialize_params({}))
        )

        self.assertEqual(
            self.router.route_backend(request(8, "sampling/createMessage")),
            [
                (
                    "backend",
                    {
                        "jsonrpc": "2.0",
                        "id": 8,
                        "error": {
                            "code": -32601,
                            "message": "No connected client supports sampling",
                        },
                    },
                )
            ],
        )

    def test_server_response_is_accepted_only_from_selected_client(self):
        backend_id = self.router.route_client(
            "rooted",
            request(1, "initialize", initialize_params({"roots": {}})),
        )[0][1]["id"]
        self.router.route_backend(response(backend_id, initialize_result()))
        self.router.add_client("other", {"roots": {}})
        server_request = self.router.route_backend(request(8, "roots/list"))[0][1]

        self.assertEqual(
            self.router.route_client(
                "other", response(server_request["id"], {"roots": []})
            ),
            [],
        )
        self.assertEqual(
            self.router.route_client(
                "rooted", response(server_request["id"], {"roots": []})
            ),
            [("backend", response(8, {"roots": []}))],
        )

    def test_backend_notification_is_broadcast(self):
        self.router.add_client("first")
        self.router.add_client("second")
        message = notification("notifications/tools/list_changed")

        self.assertEqual(
            self.router.route_backend(message),
            [("first", message), ("second", message)],
        )

    def test_disconnected_client_no_longer_receives_messages(self):
        self.router.add_client("first")
        self.router.add_client("second")
        self.router.remove_client("first")

        self.assertEqual(
            self.router.route_backend(notification("notifications/message")),
            [("second", notification("notifications/message"))],
        )


class MessageParsingTests(unittest.TestCase):
    def test_parse_message_rejects_non_object_json(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            parse_message(b"[]\n")

    def test_parse_message_rejects_oversized_payload(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            parse_message(b'{"jsonrpc":"2.0"}\n', max_bytes=5)
