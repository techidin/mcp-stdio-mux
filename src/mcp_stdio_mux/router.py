"""JSON-RPC routing: many MCP clients, one backend, each client's ids intact."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, MutableMapping

MAX_MESSAGE_BYTES = 16 * 1024 * 1024
Destination = str
JsonObject = MutableMapping[str, object]
Delivery = tuple[Destination, JsonObject]


def parse_message(payload: bytes, max_bytes: int = MAX_MESSAGE_BYTES) -> JsonObject:
    if len(payload) > max_bytes:
        raise ValueError(f"MCP message exceeds {max_bytes} bytes")
    try:
        message = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON-RPC message: {error}") from error
    if not isinstance(message, dict):
        raise ValueError("MCP message must be a JSON object")
    return message


class MessageRouter:
    """Namespace JSON-RPC state while preserving each client's wire contract."""

    def __init__(self) -> None:
        self._clients: dict[str, Mapping[str, object]] = {}
        self._backend_requests: dict[str, tuple[str, object]] = {}
        self._client_progress: dict[str, tuple[str, object, str | None]] = {}
        self._server_requests: dict[str, tuple[str, object]] = {}
        self._initialize_result: object | None = None
        self._initialize_request_id: str | None = None
        self._waiting_initializers: list[tuple[str, object]] = []
        self._initialized_sent = False
        self._sequence = 0

    @property
    def initialize_result(self) -> object | None:
        """The backend's first `initialize` result, once it has answered."""
        return self._initialize_result

    def add_client(
        self, client_id: str, capabilities: Mapping[str, object] | None = None
    ) -> None:
        self._clients[client_id] = capabilities or {}

    def remove_client(self, client_id: str) -> list[Delivery]:
        """Forget a session; answer the server requests it will never answer."""
        self._clients.pop(client_id, None)
        orphaned = [
            proxy_id
            for proxy_id, owner in self._server_requests.items()
            if owner[0] == client_id
        ]
        answers: list[Delivery] = [
            (
                "backend",
                error_response(
                    self._server_requests.pop(proxy_id)[1], "Client disconnected"
                ),
            )
            for proxy_id in orphaned
        ]
        self._backend_requests = {
            proxy_id: owner
            for proxy_id, owner in self._backend_requests.items()
            if owner[0] != client_id
        }
        self._client_progress = {
            proxy_token: owner
            for proxy_token, owner in self._client_progress.items()
            if owner[0] != client_id
        }
        self._waiting_initializers = [
            waiting for waiting in self._waiting_initializers if waiting[0] != client_id
        ]
        return answers

    def fail_backend(self, error_message: str) -> list[Delivery]:
        deliveries = []
        for client_id, message_id in self._backend_requests.values():
            if client_id in self._clients:
                deliveries.append(
                    (client_id, error_response(message_id, error_message))
                )
        for client_id, message_id in self._waiting_initializers:
            if client_id in self._clients:
                deliveries.append(
                    (client_id, error_response(message_id, error_message))
                )
        self._backend_requests.clear()
        self._waiting_initializers.clear()
        return deliveries

    def route_client(
        self, client_id: str, message: Mapping[str, object]
    ) -> list[Delivery]:
        self.add_client(client_id, self._clients.get(client_id))
        routed = copy.deepcopy(message)
        method = routed.get("method")
        message_id = routed.get("id")

        if method == "initialize" and message_id is not None:
            return self._route_initialize(client_id, routed)
        if method == "notifications/initialized":
            if self._initialized_sent:
                return []
            self._initialized_sent = True
            return [("backend", routed)]
        if method is None and message_id is not None:
            return self._route_server_response(client_id, routed)
        if method == "notifications/cancelled":
            self._restore_cancelled_request(client_id, routed)
        if message_id is not None:
            proxy_id = self._next_id("c")
            self._backend_requests[proxy_id] = (client_id, message_id)
            routed["id"] = proxy_id
        self._namespace_progress_token(client_id, routed)
        return [("backend", routed)]

    def route_backend(self, message: Mapping[str, object]) -> list[Delivery]:
        routed = copy.deepcopy(message)
        method = routed.get("method")
        message_id = routed.get("id")

        # Our proxy ids are strings; anything else cannot be a reply we sent for.
        known = isinstance(message_id, str)
        if method is None and known and message_id == self._initialize_request_id:
            return self._complete_initialize(routed)
        if method is None and known and message_id in self._backend_requests:
            proxy_id = str(message_id)
            client_id, client_message_id = self._backend_requests.pop(proxy_id)
            self._client_progress = {
                token: owner
                for token, owner in self._client_progress.items()
                if owner[2] != proxy_id
            }
            routed["id"] = client_message_id
            return [(client_id, routed)] if client_id in self._clients else []
        if method is not None and message_id is not None:
            return self._route_server_request(routed)
        if method == "notifications/progress":
            progress = self._restore_progress_token(routed)
            return [progress] if progress else []
        if method is None:
            return []  # a reply for a session that left, or for nobody
        return [(client_id, copy.deepcopy(routed)) for client_id in self._clients]

    def _route_initialize(self, client_id: str, message: JsonObject) -> list[Delivery]:
        params = message.get("params")
        capabilities = (
            params.get("capabilities", {}) if isinstance(params, dict) else {}
        )
        self._clients[client_id] = (
            capabilities if isinstance(capabilities, dict) else {}
        )
        client_message_id = message["id"]
        if self._initialize_result is not None:
            return [(client_id, _result(client_message_id, self._initialize_result))]
        if self._initialize_request_id is not None:
            self._waiting_initializers.append((client_id, client_message_id))
            return []
        proxy_id = self._next_id("i")
        self._initialize_request_id = proxy_id
        self._backend_requests[proxy_id] = (client_id, client_message_id)
        message["id"] = proxy_id
        return [("backend", message)]

    def _complete_initialize(self, message: JsonObject) -> list[Delivery]:
        proxy_id = str(message["id"])
        owner = self._backend_requests.pop(proxy_id, None)
        self._initialize_request_id = None
        deliveries: list[Delivery] = []
        if "result" in message:
            self._initialize_result = copy.deepcopy(message["result"])
            if owner is not None and owner[0] in self._clients:
                deliveries.append(
                    (owner[0], _result(owner[1], self._initialize_result))
                )
            for waiting_client, waiting_id in self._waiting_initializers:
                if waiting_client in self._clients:
                    deliveries.append(
                        (waiting_client, _result(waiting_id, self._initialize_result))
                    )
        else:
            if owner is not None and owner[0] in self._clients:
                restored = copy.deepcopy(message)
                restored["id"] = owner[1]
                deliveries.append((owner[0], restored))
            for waiting_client, waiting_id in self._waiting_initializers:
                failed = copy.deepcopy(message)
                failed["id"] = waiting_id
                if waiting_client in self._clients:
                    deliveries.append((waiting_client, failed))
        self._waiting_initializers.clear()
        return deliveries

    def _route_server_request(self, message: JsonObject) -> list[Delivery]:
        method = str(message["method"])
        capability = _required_capability(method)
        client_id = next(
            (
                candidate
                for candidate, capabilities in self._clients.items()
                if capability is None or capability in capabilities
            ),
            None,
        )
        if client_id is None:
            return [
                (
                    "backend",
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "No connected client supports "
                            + (capability or method),
                        },
                    },
                )
            ]
        proxy_id = self._next_id("s")
        self._server_requests[proxy_id] = (client_id, message["id"])
        message["id"] = proxy_id
        return [(client_id, message)]

    def _route_server_response(
        self, client_id: str, message: JsonObject
    ) -> list[Delivery]:
        proxy_id = str(message["id"])
        owner = self._server_requests.get(proxy_id)
        if owner is None or owner[0] != client_id:
            return []
        _, backend_id = self._server_requests.pop(proxy_id)
        message["id"] = backend_id
        return [("backend", message)]

    def _namespace_progress_token(self, client_id: str, message: JsonObject) -> None:
        params = message.get("params")
        metadata = params.get("_meta") if isinstance(params, dict) else None
        if not isinstance(metadata, dict) or "progressToken" not in metadata:
            return
        client_token = metadata["progressToken"]
        proxy_token = self._next_id("p")
        backend_request_id = str(message["id"]) if "id" in message else None
        self._client_progress[proxy_token] = (
            client_id,
            client_token,
            backend_request_id,
        )
        metadata["progressToken"] = proxy_token

    def _restore_progress_token(self, message: JsonObject) -> Delivery | None:
        params = message.get("params")
        if not isinstance(params, dict):
            return None
        proxy_token = str(params.get("progressToken"))
        owner = self._client_progress.get(proxy_token)
        if owner is None:
            return None
        client_id, client_token, _ = owner
        params["progressToken"] = client_token
        return (client_id, message) if client_id in self._clients else None

    def _restore_cancelled_request(self, client_id: str, message: JsonObject) -> None:
        params = message.get("params")
        if not isinstance(params, dict) or "requestId" not in params:
            return
        client_request_id = params["requestId"]
        for proxy_id, owner in self._backend_requests.items():
            if owner == (client_id, client_request_id):
                params["requestId"] = proxy_id
                return

    def _next_id(self, prefix: str) -> str:
        self._sequence += 1
        return f"{prefix}:{self._sequence}"


def _required_capability(method: str) -> str | None:
    return {
        "roots/list": "roots",
        "sampling/createMessage": "sampling",
        "elicitation/create": "elicitation",
    }.get(method)


def _result(message_id: object, result: object) -> JsonObject:
    return {"jsonrpc": "2.0", "id": message_id, "result": copy.deepcopy(result)}


def error_response(message_id: object, message: str, code: int = -32603) -> JsonObject:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": message},
    }


def encode(message: Mapping[str, object]) -> bytes:
    return (
        json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()
