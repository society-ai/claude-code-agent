"""Channel hub — bridge-side endpoint for session channel servers.

Each Claude Code session the SessionManager launches spawns a channel
server (channel/server.mjs) as its MCP channel subprocess. That server
opens a persistent line-delimited JSON connection to this hub over a Unix
socket and registers its session key. From then on:

  hub -> server:  {"type":"event","content":..,"meta":{..}}   (push into session)
  server -> hub:  {"type":"register","session_key":..}         (after handshake)

The hub keeps a session_key -> writer map so the bridge can push an event
into a specific session. Delivery is one-way: a session's answer comes back
off its transcript when the turn ends, not over this socket, so there is no
reply frame to handle. It is pure transport: it knows nothing about tasks,
chats, or the platform — the bridge wires those in.

Distinct from bridge_ipc.py (request/response JSON-RPC for MCP->bridge
calls); this is long-lived bidirectional streaming for channels.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

logger = logging.getLogger("channel_hub")

MAX_LINE_BYTES = 8 * 1024 * 1024  # channel events can carry rich payloads


class ChannelHub:
    def __init__(self, sock_path: str):
        self._path = sock_path
        self._server: Optional[asyncio.AbstractServer] = None
        # session_key -> StreamWriter of the connected channel server
        self._conns: dict[str, asyncio.StreamWriter] = {}

    @property
    def path(self) -> str:
        return self._path

    def is_connected(self, session_key: str) -> bool:
        w = self._conns.get(session_key)
        return w is not None and not w.is_closing()

    async def start(self) -> None:
        sock_dir = os.path.dirname(self._path)
        if sock_dir:
            os.makedirs(sock_dir, exist_ok=True)
            try:
                os.chmod(sock_dir, 0o700)
            except OSError:
                pass
        if os.path.exists(self._path):
            import stat as _stat
            try:
                st = os.stat(self._path)
                if _stat.S_ISSOCK(st.st_mode):
                    os.unlink(self._path)
            except FileNotFoundError:
                pass
        self._server = await asyncio.start_unix_server(self._handle, path=self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        logger.info("Channel hub listening on %s", self._path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        for w in list(self._conns.values()):
            try:
                w.close()
            except Exception:
                pass
        self._conns.clear()
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    async def push_event(
        self, session_key: str, content: str, meta: Optional[dict] = None
    ) -> bool:
        """Deliver a channel event into a session. Returns False if the
        session's channel isn't currently connected."""
        w = self._conns.get(session_key)
        if w is None or w.is_closing():
            return False
        frame = {
            "type": "event",
            "content": content,
            "meta": _clean_meta(meta or {}),
        }
        try:
            w.write((json.dumps(frame, default=str) + "\n").encode("utf-8"))
            await w.drain()
            return True
        except (ConnectionError, RuntimeError):
            self._conns.pop(session_key, None)
            return False

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        registered_key: Optional[str] = None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    logger.warning("Channel frame exceeds cap; dropping connection")
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if mtype == "register":
                    key = msg.get("session_key")
                    if isinstance(key, str) and key:
                        # A reconnect replaces any stale writer for this key.
                        old = self._conns.get(key)
                        if old is not None and old is not writer:
                            try:
                                old.close()
                            except Exception:
                                pass
                        self._conns[key] = writer
                        registered_key = key
                        logger.info("Channel registered: session_key=%s", key)
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception:
            logger.exception("Channel hub connection error")
        finally:
            if registered_key and self._conns.get(registered_key) is writer:
                self._conns.pop(registered_key, None)
                logger.info("Channel disconnected: session_key=%s", registered_key)
            try:
                writer.close()
            except Exception:
                pass


def _clean_meta(meta: dict) -> dict:
    """Channel tag attributes must be identifier-safe (letters, digits,
    underscore). Drop anything else so the value reaches Claude intact rather
    than being silently dropped by the channel runtime."""
    out: dict[str, str] = {}
    for k, v in meta.items():
        ks = str(k)
        if ks and all(c.isalnum() or c == "_" for c in ks):
            out[ks] = str(v)
    return out
