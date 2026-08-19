"""Uvicorn WebSocket data-plane tuned for bulk VLESS/WS transfers.

This module deliberately stays on Uvicorn's supported Sans-I/O WebSocket engine;
it only changes queue watermarks, accepted-socket tuning, and exposes two small
fast-path methods that avoid the Starlette/ASGI dictionary hop for each binary
message.  All framing, masking validation, close handling and protocol state
remain owned by ``websockets``.

The implementation is pinned to Uvicorn 0.52.4 in requirements.txt.  If this
module cannot be imported, main.py falls back to Uvicorn's stock protocol.
"""

from __future__ import annotations

import asyncio
import os
import socket
from typing import Any

from uvicorn.protocols.utils import ClientDisconnected
from uvicorn.protocols.websockets.websockets_sansio_impl import (
    WebSocketsSansIOProtocol,
)
from websockets.exceptions import InvalidState


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


# A stock Uvicorn Sans-I/O connection pauses transport reading after *every*
# complete message. Xray sends many binary messages while downloading/uploading;
# pause/resume on each one becomes an avoidable syscall/event-loop bottleneck.
RX_QUEUE_HIGH = _env_int("WS_TURBO_QUEUE_HIGH", 128, 8, 1024)
RX_QUEUE_LOW = _env_int("WS_TURBO_QUEUE_LOW", 32, 1, RX_QUEUE_HIGH - 1)

SOCKET_BUFFER = _env_int(
    "WS_TURBO_SOCKET_BUFFER", 16 * 1024 * 1024, 256 * 1024, 64 * 1024 * 1024
)
WRITE_BUFFER_HIGH = _env_int(
    "WS_TURBO_WRITE_HIGH", 8 * 1024 * 1024, 256 * 1024, 32 * 1024 * 1024
)
WRITE_BUFFER_LOW = min(
    _env_int("WS_TURBO_WRITE_LOW", 1024 * 1024, 64 * 1024, 8 * 1024 * 1024),
    WRITE_BUFFER_HIGH // 2,
)
PREFERRED_CC = (b"bbr", b"cubic")


def _tune_accepted_socket(transport: asyncio.Transport) -> None:
    """Tune the actual accepted client socket, not merely the listen socket."""
    try:
        transport.set_write_buffer_limits(high=WRITE_BUFFER_HIGH, low=WRITE_BUFFER_LOW)
    except Exception:
        pass

    try:
        sock = transport.get_extra_info("socket")
    except Exception:
        sock = None
    if sock is None:
        return

    for level, option, value in (
        (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
        (socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER),
        (socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER),
    ):
        try:
            sock.setsockopt(level, option, value)
        except OSError:
            pass

    quickack = getattr(socket, "TCP_QUICKACK", None)
    if quickack is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, quickack, 1)
        except OSError:
            pass

    congestion = getattr(socket, "TCP_CONGESTION", None)
    if congestion is not None:
        for algorithm in PREFERRED_CC:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, congestion, algorithm)
                break
            except OSError:
                continue

    # Low-delay DSCP hint; unsupported/container-restricted kernels simply ignore it.
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x10)
    except OSError:
        pass


class TurboWebSocketsSansIOProtocol(WebSocketsSansIOProtocol):
    """Sans-I/O WebSocket protocol with bulk-transfer queueing and fast I/O hooks."""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        _tune_accepted_socket(self.transport)

    def send_receive_event_to_app(self) -> None:
        """Queue bursts and pause only at a real high-water mark.

        Uvicorn 0.52.4's stock implementation pauses socket reading for every
        individual message. Here, up to ``RX_QUEUE_HIGH`` complete messages may
        be queued. This preserves bounded backpressure while removing thousands
        of pause/resume transitions during a large transfer.
        """
        data = self.frames[0] if len(self.frames) == 1 else b"".join(self.frames)
        self.frames = []

        if self.close_sent:
            return

        if self.curr_msg_data_type == "text":
            try:
                message = {"type": "websocket.receive", "text": data.decode()}
            except UnicodeDecodeError:
                self.logger.exception("Invalid UTF-8 sequence received from client.")
                self.conn.send_close(1007)
                self.handle_parser_exception()
                return
        else:
            # Keep the single-frame object unchanged. websockets/Uvicorn 0.50+
            # specifically avoids copying this payload.
            message = {"type": "websocket.receive", "bytes": data}

        self.queue.put_nowait(message)  # type: ignore[arg-type]
        if not self.read_paused and self.queue.qsize() >= RX_QUEUE_HIGH:
            self.read_paused = True
            self.transport.pause_reading()

    async def receive(self):
        message = await self.queue.get()
        if self.read_paused and self.queue.qsize() <= RX_QUEUE_LOW:
            self.read_paused = False
            self.transport.resume_reading()
        return message

    async def turbo_receive(self):
        """Direct receive hook used by relay_vless after Starlette accepted WS."""
        return await self.receive()

    async def turbo_send_bytes(self, data: bytes | bytearray | memoryview) -> None:
        """Send one official Sans-I/O binary frame without the ASGI dict hop.

        This does not handcraft WebSocket frames. ``websockets`` still validates
        protocol state and serializes the frame; we only avoid Starlette and
        Uvicorn unpacking/repacking an ASGI message for every payload.
        """
        if not self.writable.is_set():
            await self.writable.wait()
        if (
            self.disconnected
            or self.close_sent
            or not self.handshake_complete
            or self.transport.is_closing()
        ):
            raise ClientDisconnected()

        try:
            self.conn.send_binary(data)
            output = self.conn.data_to_send()
        except InvalidState as exc:
            raise ClientDisconnected() from exc

        # Keep buffers separate when Sans-I/O returns header + payload pieces;
        # b''.join(...) would create a full-size duplicate of a multi-MB frame.
        for part in output:
            self.transport.write(part)

    async def turbo_flush(self) -> None:
        """Wait until transport backpressure has dropped below its low watermark."""
        if not self.writable.is_set() and not self.disconnected:
            await self.writable.wait()


__all__ = ["TurboWebSocketsSansIOProtocol"]
