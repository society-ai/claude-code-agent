"""Bridge IPC — localhost JSON-RPC over a Unix domain socket.

Used so the in-Claude-Code MCP server (a separate process) can ask the
bridge daemon to perform actions that require the bridge's WebSocket
connection — specifically agent discovery (`agents/search`) and outbound
delegation (`tasks/sendSubscribe` + correlated `delegation.result`).

Protocol: newline-delimited JSON. Each request is one line:
    {"id": "<str>", "method": "<str>", "params": {...}}

Each response is one line:
    {"id": "<str>", "result": {...}}      # success
    {"id": "<str>", "error": {"message": "...", ...}}  # failure

Security:
- The socket lives under a directory that's chmod 0700 (creator-only).
- The socket file itself is also chmod 0600 after bind.
- Only processes running as the same user can connect; the bridge does
  not need to authenticate the client.
- Unknown methods return an error, never execute arbitrary code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
from typing import Any, Awaitable, Callable

logger = logging.getLogger("bridge_ipc")

# Per-message size cap. JSON-RPC frames from delegation can include the
# delegated task's full response text; 1 MiB is generous without being
# unbounded.
MAX_FRAME_BYTES = 1 << 20

# Default socket path. Configurable via SOCIETY_AI_BRIDGE_SOCKET.
DEFAULT_SOCKET_PATH = os.path.join(
    os.path.expanduser("~"), ".cache", "society-ai", "bridge.sock"
)


def socket_path() -> str:
    """Resolve the IPC socket path from env, with a per-user default."""
    return os.environ.get("SOCIETY_AI_BRIDGE_SOCKET", DEFAULT_SOCKET_PATH)


# Type alias: handler maps method name -> async callable(params: dict) -> result
HandlerMap = dict[str, Callable[[dict], Awaitable[Any]]]


# -- Server side (bridge) -----------------------------------------------------


class IPCServer:
    """Unix-socket JSON-RPC server hosted by the bridge daemon."""

    def __init__(self, handlers: HandlerMap, path: str | None = None):
        self._handlers = handlers
        self._path = path or socket_path()
        self._server: asyncio.AbstractServer | None = None

    @property
    def path(self) -> str:
        return self._path

    async def start(self) -> None:
        """Start listening. Cleans up any stale socket file from a prior run."""
        sock_dir = os.path.dirname(self._path)
        if sock_dir:
            os.makedirs(sock_dir, exist_ok=True)
            try:
                os.chmod(sock_dir, 0o700)
            except OSError:
                pass

        # Remove a stale socket file from a previous bridge invocation.
        if os.path.exists(self._path):
            try:
                # Sanity-check that what we're about to unlink is actually
                # a socket — don't blow away a regular file by accident.
                st = os.stat(self._path)
                import stat as _stat
                if _stat.S_ISSOCK(st.st_mode):
                    os.unlink(self._path)
                else:
                    raise RuntimeError(
                        f"IPC path {self._path} exists and is not a socket; refusing to unlink"
                    )
            except FileNotFoundError:
                pass

        self._server = await asyncio.start_unix_server(self._handle_client, path=self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        logger.info("IPC server listening on %s", self._path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Could not unlink IPC socket %s: %s", self._path, e)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername") or "<local>"
        logger.debug("IPC client connected: %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                if len(line) > MAX_FRAME_BYTES:
                    await self._write_response(writer, None, error_obj={
                        "message": f"Frame exceeds {MAX_FRAME_BYTES} bytes",
                    })
                    return

                req_id: Any = None
                try:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        raise ValueError("frame must be a JSON object")
                    req_id = msg.get("id")
                    method = msg.get("method")
                    params = msg.get("params") or {}
                    if not isinstance(method, str) or not method:
                        raise ValueError("missing 'method'")
                    if not isinstance(params, dict):
                        raise ValueError("'params' must be an object")
                except (ValueError, json.JSONDecodeError) as e:
                    await self._write_response(writer, req_id, error_obj={
                        "message": f"Bad request: {e}",
                    })
                    continue

                handler = self._handlers.get(method)
                if handler is None:
                    await self._write_response(writer, req_id, error_obj={
                        "message": f"Unknown method: {method}",
                    })
                    continue

                try:
                    result = await handler(params)
                except Exception as e:
                    logger.exception("IPC handler %s raised", method)
                    await self._write_response(writer, req_id, error_obj={
                        "message": f"{type(e).__name__}: {e}",
                    })
                    continue

                await self._write_response(writer, req_id, result=result)
        except (asyncio.CancelledError, ConnectionError):
            return
        except Exception:
            logger.exception("IPC client handler error")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _write_response(
        writer: asyncio.StreamWriter,
        req_id: Any,
        result: Any = None,
        error_obj: dict | None = None,
    ) -> None:
        payload: dict[str, Any] = {"id": req_id}
        if error_obj is not None:
            payload["error"] = error_obj
        else:
            payload["result"] = result
        data = (json.dumps(payload, default=str) + "\n").encode("utf-8")
        try:
            writer.write(data)
            await writer.drain()
        except ConnectionError:
            pass


# -- Client side (MCP server / any other local caller) -----------------------


class IPCClientError(Exception):
    """Raised when the IPC call fails (transport or peer reported error)."""


async def call(method: str, params: dict, timeout: float = 60.0) -> Any:
    """Call a single IPC method on the bridge and return its result.

    Opens a fresh connection per call — cheap, and avoids long-lived sockets
    in the MCP server process. Raises IPCClientError on any failure so the
    caller can wrap into a structured error.
    """
    path = socket_path()
    if not os.path.exists(path):
        raise IPCClientError(
            f"Bridge IPC socket not found at {path}. "
            "Start the bridge daemon: `python bridge.py`."
        )

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path), timeout=5.0
        )
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise IPCClientError(
            f"Could not connect to bridge IPC at {path}: {e}. "
            "Start the bridge daemon: `python bridge.py`."
        ) from e
    except asyncio.TimeoutError as e:
        raise IPCClientError(f"Timed out connecting to bridge IPC at {path}") from e

    try:
        req_id = f"mcp-{os.getpid()}-{id(method)}"
        frame = (json.dumps({"id": req_id, "method": method, "params": params}) + "\n").encode("utf-8")
        if len(frame) > MAX_FRAME_BYTES:
            raise IPCClientError(f"Request frame too large ({len(frame)} > {MAX_FRAME_BYTES})")
        writer.write(frame)
        await writer.drain()

        try:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise IPCClientError(f"Timed out waiting for bridge response ({timeout}s)") from e

        if not line:
            raise IPCClientError("Bridge closed the connection without a response")

        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            raise IPCClientError(f"Malformed response from bridge: {e}") from e

        if not isinstance(msg, dict):
            raise IPCClientError("Bridge response was not a JSON object")
        err = msg.get("error")
        if err is not None:
            raise IPCClientError(err.get("message") if isinstance(err, dict) else str(err))
        return msg.get("result")
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
