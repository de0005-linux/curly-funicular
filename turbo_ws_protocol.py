"""High-throughput Uvicorn WebSocket protocol for bulk VLESS traffic.

Pinned to Uvicorn 0.52.4. It keeps the standards-compliant websockets Sans-I/O
parser for client frames, while removing three hot-path costs:
1) pause/resume of socket reading after every message;
2) Starlette/ASGI dict round-trips for each binary message;
3) BytesIO serialization that copies every server-to-client payload.

The direct sender is used only with per-message compression disabled. Frames are
standard FIN+binary, unmasked server frames. Control and close frames remain
owned by the Sans-I/O state machine.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
from typing import Any

from uvicorn.protocols.utils import ClientDisconnected
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


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
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x10)
    except OSError:
        pass


def _binary_header(length: int) -> bytes:
    if length < 126:
        return bytes((0x82, length))
    if length < 65536:
        return struct.pack("!BBH", 0x82, 126, length)
    return struct.pack("!BBQ", 0x82, 127, length)


class TurboWebSocketsSansIOProtocol(WebSocketsSansIOProtocol):
    """Sans-I/O protocol with bounded burst queueing and a no-full-copy sender."""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        _tune_accepted_socket(self.transport)

    def send_receive_event_to_app(self) -> None:
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
        return await self.receive()

    async def turbo_send_bytes(self, data: bytes | bytearray | memoryview) -> None:
        """Write an uncompressed server binary frame as header + payload pieces.

        No await occurs between the two transport writes, so control frames cannot
        interleave. If compression is ever enabled, fall back to the official
        Sans-I/O serializer because RSV1 and extension state would be required.
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

        if self.config.ws_per_message_deflate:
            await self.send({"type": "websocket.send", "bytes": bytes(data)})
            return

        self.transport.write(_binary_header(len(data)))
        self.transport.write(data)

    async def turbo_flush(self) -> None:
        if not self.writable.is_set() and not self.disconnected:
            await self.writable.wait()


__all__ = ["TurboWebSocketsSansIOProtocol"]
