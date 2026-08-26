# xhttp_siz10.py
# XHTTP Hyper data plane: packet-up, stream-up, stream-one and auto.
# Compatible with Xray's default path placement while sharing the WS adaptive
# dual-stack connector, backpressure, quota accounting and socket tuning.

from __future__ import annotations

import asyncio
import secrets
import time
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response, StreamingResponse

from main import (
    LINKS,
    LINKS_LOCK,
    stats,
    connections,
    error_logs,
    logger,
    is_link_allowed,
    is_ip_allowed,
    save_state,
)
from relay_vless import (
    parse_vless_header,
    check_and_use,
    open_upstream,
    tune_upstream_socket,
)
from outbound import open_outbound
from speed_limit import QuotaGate, throttle

router = APIRouter()


class _DuplexStreamingResponse(StreamingResponse):
    """StreamingResponse without Starlette's competing receive() listener.

    Stock StreamingResponse consumes ASGI receive events in a disconnect task.
    XHTTP stream-up/one must consume that same receive channel as request body;
    two consumers can steal upload chunks from each other. Uvicorn cancels the
    response task or makes send fail on disconnect, so one stream_response task
    is both faster and correct for full-duplex transport.
    """

    async def __call__(self, scope, receive, send) -> None:
        await self.stream_response(send)
        background = getattr(self, "background", None)
        if background is not None:
            await background()


MODES = ("packet-up", "stream-up", "stream-one", "auto")
DOWNLINK_MODES = ("packet-up", "stream-up", "auto")
SEQ_UPLOAD_MODES = ("packet-up", "auto")
BASE_DUPLEX_MODES = ("stream-one", "auto")

# Data plane: large reads reduce Python/ASGI crossings; read() still returns
# immediately with available bytes, so small page responses aren't delayed.
XHTTP_READ_MAX = 2 * 1024 * 1024
HEADER_MAX = 16 * 1024
PACKET_BODY_MAX = 8 * 1024 * 1024
MAX_SEQ_BUFFER = 128
MAX_SEQ_BUFFER_BYTES = 64 * 1024 * 1024
DOWNLINK_QUEUE_MAX = 128
MAX_SESSIONS = 5000
MAX_SESSION_ID_LEN = 64
TCP_CONNECT_TIMEOUT = 10.0

SESSION_IDLE_TIMEOUT = 30.0
ACTIVE_IDLE_TIMEOUT = 30.0 * 60.0
REAPER_INTERVAL = 10.0
STREAM_UP_KEEPALIVE = 25.0

FLOW_MIN_HW = 512 * 1024
FLOW_MAX_HW = 64 * 1024 * 1024
FLOW_START_HW = 8 * 1024 * 1024
FLOW_FAST_DRAIN_MS = 2.0
FLOW_SLOW_DRAIN_MS = 30.0

xhttp_sessions: dict[str, dict] = {}
XHTTP_LOCK = asyncio.Lock()
_reaper_started = False


class _AdaptiveFlow:
    """Per-session AIMD upload window with real transport watermarks."""

    __slots__ = ("high_water",)

    def __init__(self) -> None:
        self.high_water = FLOW_START_HW

    async def drain_if_needed(self, writer: asyncio.StreamWriter) -> None:
        transport = writer.transport
        if transport.get_write_buffer_size() < self.high_water:
            return
        started = time.monotonic()
        await writer.drain()
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if elapsed_ms <= FLOW_FAST_DRAIN_MS:
            self.high_water = min(
                int(self.high_water * 1.5) + 64 * 1024, FLOW_MAX_HW
            )
        elif elapsed_ms >= FLOW_SLOW_DRAIN_MS:
            self.high_water = max(self.high_water // 2, FLOW_MIN_HW)
        try:
            transport.set_write_buffer_limits(
                high=self.high_water,
                low=max(self.high_water // 4, 64 * 1024),
            )
        except Exception:
            pass


def _now() -> float:
    return time.monotonic()


def _req_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",", 1)[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    client = getattr(request, "client", None)
    return client.host if client else "نامشخص"


def _validate(mode: str, session_id: str) -> None:
    if mode not in MODES:
        raise HTTPException(status_code=404, detail="unknown mode")
    if not session_id or len(session_id) > MAX_SESSION_ID_LEN:
        raise HTTPException(status_code=400, detail="bad session id")


def _padding() -> str:
    # One response-header pad per stream; unlike body padding this has no impact
    # on application bytes or download throughput.
    return "0" * (100 + secrets.randbelow(401))


def _down_headers() -> dict[str, str]:
    # Xray's official server defaults downlink to SSE so CDNs flush it early.
    return {
        "content-type": "text/event-stream",
        "cache-control": "no-store, no-transform",
        "x-accel-buffering": "no",
        "x-content-type-options": "nosniff",
        "x-padding": _padding(),
    }


def _upload_headers() -> dict[str, str]:
    return {
        "content-type": "application/grpc",
        "cache-control": "no-store, no-transform",
        "x-accel-buffering": "no",
    }


def _ok() -> Response:
    # Xray only needs HTTP 200. Avoid JSON encoding and response-body bytes for
    # every packet-up POST.
    return Response(content=b"", status_code=200, headers={"cache-control": "no-store"})


def _new_gate(uuid: str) -> QuotaGate:
    return QuotaGate(uuid, check_and_use)


def _stage(gate: QuotaGate, nbytes: int) -> int:
    return gate.stage(nbytes)


def _speed_limited(uuid: str) -> bool:
    link = LINKS.get(uuid)
    return bool(link and int(link.get("speed_limit_bytes", 0) or 0) > 0)


async def _check_link(uuid: str) -> dict:
    async with LINKS_LOCK:
        link = LINKS.get(uuid)
    if not is_link_allowed(link):
        raise HTTPException(status_code=403, detail="not authorized")
    return link


async def _get_or_create_session(
    uuid: str, mode: str, session_id: str, ip: str = "نامشخص"
) -> dict:
    # Existing-session fast path holds the global lock for only a dictionary read.
    async with XHTTP_LOCK:
        existing = xhttp_sessions.get(session_id)
        if existing is not None:
            if existing["uuid"] != uuid or existing["mode"] != mode:
                raise HTTPException(status_code=403, detail="session mismatch")
            existing["last_seen"] = _now()
            return existing

    # Permission work is deliberately outside XHTTP_LOCK; bursts of new web
    # streams no longer serialize behind the LINKS lock while holding it.
    link = await _check_link(uuid)
    if not is_ip_allowed(link, uuid, ip):
        logger.warning(
            "XHTTP[%s] rejected uuid=%s… ip=%s (ip limit)",
            mode,
            uuid[:8],
            ip,
        )
        raise HTTPException(status_code=403, detail="ip limit reached")

    async with XHTTP_LOCK:
        existing = xhttp_sessions.get(session_id)
        if existing is not None:
            if existing["uuid"] != uuid or existing["mode"] != mode:
                raise HTTPException(status_code=403, detail="session mismatch")
            existing["last_seen"] = _now()
            return existing
        if len(xhttp_sessions) >= MAX_SESSIONS:
            raise HTTPException(status_code=503, detail="too many sessions")

        conn_id = secrets.token_urlsafe(6)
        sess = {
            "uuid": uuid,
            "mode": mode,
            "writer": None,
            "downlink_task": None,
            "uplink_task": None,
            "uplink_reserved": False,
            # One reserved queue slot guarantees the end sentinel can always be
            # appended even after the data slots reach their high-water mark.
            "down_q": asyncio.Queue(maxsize=DOWNLINK_QUEUE_MAX + 1),
            "down_slots": asyncio.Semaphore(DOWNLINK_QUEUE_MAX),
            "down_closed": False,
            "down_attached": False,
            "last_seen": _now(),
            "conn_id": conn_id,
            "tcp_open": False,
            "closed": False,
            "seq_buf": {},
            "seq_buf_bytes": 0,
            "next_seq": 0,
            "header_buf": bytearray(),
            "gate": None,
            "flow": _AdaptiveFlow(),
            "open_lock": asyncio.Lock(),
            "packet_lock": asyncio.Lock(),
        }
        xhttp_sessions[session_id] = sess
        connections[conn_id] = {
            "uuid": uuid,
            "ip": ip,
            "connected_at": datetime.now().isoformat(),
            "bytes": 0,
            "transport": f"xhttp-{mode}-hyper",
        }

    logger.info(
        "new XHTTP[%s] session [%s] uuid=%s… ip=%s",
        mode,
        session_id[:8],
        uuid[:8],
        ip,
    )
    return sess


def _session_gate(sess: dict, uuid: str) -> QuotaGate:
    gate = sess.get("gate")
    if gate is None:
        gate = _new_gate(uuid)
        sess["gate"] = gate
    return gate


async def _queue_down(sess: dict, payload: bytes) -> bool:
    if sess.get("closed"):
        return False
    slots = sess["down_slots"]
    await slots.acquire()
    if sess.get("closed"):
        slots.release()
        return False
    await sess["down_q"].put(payload)
    return True


def _signal_down_end(sess: dict) -> None:
    if sess.get("down_closed"):
        return
    sess["down_closed"] = True
    # At most DOWNLINK_QUEUE_MAX data items can be queued because each reserves
    # a semaphore slot; queue capacity has one extra position for this sentinel.
    try:
        sess["down_q"].put_nowait(None)
    except asyncio.QueueFull:
        # Defensive fallback; this should be unreachable.
        pass


async def _teardown(session_id: str) -> None:
    async with XHTTP_LOCK:
        sess = xhttp_sessions.pop(session_id, None)
    if sess is None:
        return
    sess["closed"] = True
    _signal_down_end(sess)

    gate = sess.get("gate")
    if gate is not None:
        try:
            await gate.flush()
        except Exception:
            pass

    current = asyncio.current_task()
    for key in ("uplink_task", "downlink_task"):
        task = sess.get(key)
        if task is not None and task is not current and not task.done():
            task.cancel()
    tasks = [
        task
        for key in ("uplink_task", "downlink_task")
        if (task := sess.get(key)) is not None and task is not current
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    writer = sess.get("writer")
    if writer is not None:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    connections.pop(sess.get("conn_id"), None)
    sess["seq_buf"].clear()
    sess["header_buf"].clear()
    logger.info(
        "closed XHTTP[%s] [%s] total=%d",
        sess.get("mode"),
        session_id[:8],
        len(xhttp_sessions),
    )


async def _reaper() -> None:
    while True:
        await asyncio.sleep(REAPER_INTERVAL)
        try:
            now = _now()
            async with XHTTP_LOCK:
                stale = [
                    sid
                    for sid, sess in xhttp_sessions.items()
                    if now - sess["last_seen"]
                    > (ACTIVE_IDLE_TIMEOUT if sess.get("tcp_open") else SESSION_IDLE_TIMEOUT)
                ]
            for sid in stale:
                await _teardown(sid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("xhttp reaper error: %s", exc)


def ensure_reaper() -> None:
    global _reaper_started
    if not _reaper_started:
        asyncio.create_task(_reaper(), name="xhttp-reaper")
        _reaper_started = True


async def _pump_tcp_to_queue(
    session_id: str,
    uuid: str,
    reader: asyncio.StreamReader,
    sess: dict,
) -> None:
    gate = _new_gate(uuid)
    conn = connections.get(sess["conn_id"])
    limited = _speed_limited(uuid)
    ticks = 0
    try:
        while True:
            data = await reader.read(XHTTP_READ_MAX)
            if not data:
                break
            nbytes = len(data)
            amount = _stage(gate, nbytes)
            if amount < 0 or (amount and not await gate.commit(amount)):
                break
            ticks += 1
            if not (ticks & 127):
                limited = _speed_limited(uuid)
            if limited:
                await throttle(uuid, nbytes)
            if conn is not None:
                conn["bytes"] += nbytes
            sess["last_seen"] = _now()
            if not await _queue_down(sess, data):
                break
    except asyncio.CancelledError:
        raise
    except (ConnectionError, OSError):
        pass
    except Exception as exc:
        stats["total_errors"] = int(stats.get("total_errors", 0) or 0) + 1
        error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
    finally:
        try:
            await gate.flush()
        except Exception:
            pass
        await _teardown(session_id)


async def _open_tcp_for_session(
    session_id: str,
    uuid: str,
    sess: dict,
    chunk: bytes,
) -> bool:
    """Collect a fragmented VLESS header and open exactly one upstream."""
    async with sess["open_lock"]:
        writer = sess.get("writer")
        if writer is not None:
            writer.write(chunk)
            return True
        if sess.get("closed"):
            return False

        header = sess["header_buf"]
        header.extend(chunk)
        if len(header) < 19:
            return False
        try:
            _command, address, port, payload = await parse_vless_header(header)
        except ValueError:
            if len(header) >= HEADER_MAX:
                raise HTTPException(status_code=400, detail="invalid VLESS header")
            return False

        # همان لایه‌ی آی‌پی خروجی که WS استفاده می‌کند: ProxyIP یا پروکسی زنجیره‌ای.
        try:
            async with asyncio.timeout(TCP_CONNECT_TIMEOUT):
                reader, writer, payload_sent = await open_outbound(
                    address, port, payload, link=LINKS.get(uuid), uuid=uuid
                )
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="upstream connect timeout") from exc
        tune_upstream_socket(writer, FLOW_START_HW)

        sess["writer"] = writer
        sess["tcp_open"] = True
        sess["last_seen"] = _now()
        header.clear()

        # Send the VLESS response prefix as its own chunk: no copy of the first
        # target payload is needed and the client gets an earlier first byte.
        await _queue_down(sess, b"\x00\x00")
        if payload and not payload_sent:
            writer.write(payload)

        sess["downlink_task"] = asyncio.create_task(
            _pump_tcp_to_queue(session_id, uuid, reader, sess),
            name=f"xhttp-down-{session_id[:8]}",
        )
        logger.info(
            "connect XHTTP[%s] [%s] -> %s:%d",
            sess["mode"],
            session_id[:8],
            address,
            port,
        )
        asyncio.create_task(save_state())
        return True


async def _write_upload_part(
    uuid: str, session_id: str, sess: dict, data: bytes
) -> None:
    writer = sess.get("writer")
    if writer is None:
        await _open_tcp_for_session(session_id, uuid, sess, data)
    else:
        writer.write(data)


async def _pump_request_to_tcp(
    uuid: str, session_id: str, sess: dict, request: Request
) -> None:
    gate = _session_gate(sess, uuid)
    flow = sess["flow"]
    conn = connections.get(sess["conn_id"])
    limited = _speed_limited(uuid)
    ticks = 0

    async for chunk in request.stream():
        if not chunk:
            continue
        nbytes = len(chunk)
        amount = _stage(gate, nbytes)
        if amount < 0 or (amount and not await gate.commit(amount)):
            raise HTTPException(status_code=403, detail="quota/disabled/unknown")
        ticks += 1
        if not (ticks & 127):
            limited = _speed_limited(uuid)
        if limited:
            await throttle(uuid, nbytes)
        if conn is not None:
            conn["bytes"] += nbytes
        sess["last_seen"] = _now()

        await _write_upload_part(uuid, session_id, sess, chunk)
        writer = sess.get("writer")
        if writer is not None:
            await flow.drain_if_needed(writer)

    await gate.flush()
    writer = sess.get("writer")
    if writer is None:
        raise HTTPException(status_code=400, detail="incomplete VLESS header")
    # Request EOF means upload half-close, not that queued download bytes should
    # be discarded. Let the target finish its response and close naturally.
    try:
        writer.write_eof()
    except (AttributeError, OSError):
        pass


async def _iter_down(sess: dict):
    q = sess["down_q"]
    slots = sess["down_slots"]
    while True:
        chunk = await q.get()
        if chunk is None:
            break
        slots.release()
        sess["last_seen"] = _now()
        yield chunk


async def _downstream_gen(session_id: str, sess: dict):
    try:
        async for chunk in _iter_down(sess):
            yield chunk
    finally:
        await _teardown(session_id)


async def _run_uplink(
    uuid: str, session_id: str, sess: dict, request: Request
) -> None:
    try:
        await _pump_request_to_tcp(uuid, session_id, sess, request)
    except asyncio.CancelledError:
        raise
    except HTTPException:
        await _teardown(session_id)
        raise
    except Exception as exc:
        stats["total_errors"] = int(stats.get("total_errors", 0) or 0) + 1
        error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
        await _teardown(session_id)
        raise


def _stream_up_response(
    uuid: str, session_id: str, sess: dict, request: Request
) -> StreamingResponse:
    async def response_body():
        task = asyncio.create_task(
            _run_uplink(uuid, session_id, sess, request),
            name=f"xhttp-up-{session_id[:8]}",
        )
        sess["uplink_task"] = task
        completed_normally = False
        try:
            while not task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=STREAM_UP_KEEPALIVE
                    )
                except asyncio.TimeoutError:
                    # Official XHTTP emits harmless padding on the upload response
                    # so CDNs don't kill a long stream-up request around 100 s.
                    yield b"X" * (100 + secrets.randbelow(401))
            await task
            completed_normally = True
        finally:
            if not completed_normally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await _teardown(session_id)

    return _DuplexStreamingResponse(
        response_body(), headers=_upload_headers(), media_type="application/grpc"
    )


def _stream_one_response(
    uuid: str, session_id: str, sess: dict, request: Request
) -> StreamingResponse:
    async def duplex():
        task = asyncio.create_task(
            _run_uplink(uuid, session_id, sess, request),
            name=f"xhttp-one-up-{session_id[:8]}",
        )
        sess["uplink_task"] = task
        try:
            async for chunk in _iter_down(sess):
                yield chunk
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await _teardown(session_id)

    return _DuplexStreamingResponse(
        duplex(), headers=_down_headers(), media_type="text/event-stream"
    )


@router.get("/xhttp-siz10/{mode}/{uuid}/{session_id}")
async def xhttp_downlink(
    mode: str, uuid: str, session_id: str, request: Request
):
    ensure_reaper()
    _validate(mode, session_id)
    if mode not in DOWNLINK_MODES:
        raise HTTPException(status_code=404, detail="mode has no separate downlink")
    sess = await _get_or_create_session(
        uuid, mode, session_id, _req_client_ip(request)
    )
    if sess.get("closed"):
        raise HTTPException(status_code=404, detail="session closed")
    if sess.get("down_attached"):
        raise HTTPException(status_code=409, detail="downlink already attached")
    sess["down_attached"] = True
    stats["total_requests"] = int(stats.get("total_requests", 0) or 0) + 1
    return StreamingResponse(
        _downstream_gen(session_id, sess),
        headers=_down_headers(),
        media_type="text/event-stream",
    )


@router.post("/xhttp-siz10/{mode}/{uuid}/{session_id}/{seq}")
async def xhttp_packet_up(
    mode: str,
    uuid: str,
    session_id: str,
    seq: int,
    request: Request,
):
    ensure_reaper()
    _validate(mode, session_id)
    if mode not in SEQ_UPLOAD_MODES:
        raise HTTPException(status_code=404, detail="mode does not accept packet upload")
    if seq < 0:
        raise HTTPException(status_code=400, detail="bad sequence")
    sess = await _get_or_create_session(
        uuid, mode, session_id, _req_client_ip(request)
    )
    if sess.get("closed"):
        raise HTTPException(status_code=404, detail="session closed")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > PACKET_BODY_MAX:
                raise HTTPException(status_code=413, detail="packet too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="bad content length")
    body = await request.body()
    if len(body) > PACKET_BODY_MAX:
        raise HTTPException(status_code=413, detail="packet too large")

    stats["total_requests"] = int(stats.get("total_requests", 0) or 0) + 1
    async with sess["packet_lock"]:
        next_seq = sess["next_seq"]
        if seq < next_seq or seq in sess["seq_buf"]:
            return _ok()  # idempotent retry
        if seq - next_seq > MAX_SEQ_BUFFER:
            await _teardown(session_id)
            raise HTTPException(status_code=400, detail="sequence gap too large")
        if (
            len(sess["seq_buf"]) >= MAX_SEQ_BUFFER
            or sess["seq_buf_bytes"] + len(body) > MAX_SEQ_BUFFER_BYTES
        ):
            await _teardown(session_id)
            raise HTTPException(status_code=400, detail="sequence buffer overflow")

        gate = _session_gate(sess, uuid)
        amount = _stage(gate, len(body))
        if amount < 0 or (amount and not await gate.commit(amount)):
            await _teardown(session_id)
            raise HTTPException(status_code=403, detail="quota/disabled/unknown")
        if _speed_limited(uuid):
            await throttle(uuid, len(body))
        conn = connections.get(sess["conn_id"])
        if conn is not None:
            conn["bytes"] += len(body)
        sess["last_seen"] = _now()

        sess["seq_buf"][seq] = body
        sess["seq_buf_bytes"] += len(body)
        while sess["next_seq"] in sess["seq_buf"]:
            current = sess["seq_buf"].pop(sess["next_seq"])
            sess["seq_buf_bytes"] -= len(current)
            await _write_upload_part(uuid, session_id, sess, current)
            sess["next_seq"] += 1

        writer = sess.get("writer")
        if writer is not None:
            await sess["flow"].drain_if_needed(writer)
    return _ok()


async def _start_stream(
    mode: str,
    uuid: str,
    session_id: str,
    request: Request,
    duplex: bool,
):
    ensure_reaper()
    _validate(mode, session_id)
    sess = await _get_or_create_session(
        uuid, mode, session_id, _req_client_ip(request)
    )
    if sess.get("closed"):
        raise HTTPException(status_code=404, detail="session closed")
    if sess.get("uplink_task") is not None or sess.get("uplink_reserved"):
        raise HTTPException(status_code=409, detail="uplink already attached")
    sess["uplink_reserved"] = True
    stats["total_requests"] = int(stats.get("total_requests", 0) or 0) + 1
    if duplex:
        return _stream_one_response(uuid, session_id, sess, request)
    return _stream_up_response(uuid, session_id, sess, request)


@router.post("/xhttp-siz10/{mode}/{uuid}/{session_id}")
async def xhttp_stream_upload(
    mode: str, uuid: str, session_id: str, request: Request
):
    if mode == "packet-up":
        raise HTTPException(status_code=404, detail="packet-up requires sequence")
    # Official XHTTP: a non-empty session without seq is stream-up. Keep explicit
    # stream-one+session as a compatibility path for older builds of this project.
    return await _start_stream(
        mode,
        uuid,
        session_id,
        request,
        duplex=(mode == "stream-one"),
    )


# Official stream-one has no session ID: Xray POSTs exactly to the configured
# base path (normalized with a trailing slash). Keep both slash forms to avoid a
# 307 redirect, which can break a streaming request body.
@router.post("/xhttp-siz10/{mode}/{uuid}")
@router.post("/xhttp-siz10/{mode}/{uuid}/")
async def xhttp_stream_one_base(mode: str, uuid: str, request: Request):
    if mode not in BASE_DUPLEX_MODES:
        raise HTTPException(status_code=404, detail="base path requires stream-one")
    session_id = "one-" + secrets.token_urlsafe(18)
    return await _start_stream(mode, uuid, session_id, request, duplex=True)
