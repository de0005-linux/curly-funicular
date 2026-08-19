# xhttp_siz10.py
# ══════════════════════════════════════════════════════════════════════════════
# Siz10a · XHTTP Ultra Transport — چهار مد:
#   • packet-up  : آپلینک پکتی با seq  + دانلینک GET   (سازگارترین با CDN)
#   • stream-up  : یک POST پیوسته برای آپلینک + دانلینک GET
#   • stream-one : یک درخواست POST دوطرفه (full-duplex) — کمترین تاخیر؋ نیازمند HTTP/2
#   • auto       : هر دو را هم‌زمان می‌پذیرد — کلاینت روی H2/H3 خودبه‌خود
#                  stream-one می‌گیرد و روی HTTP/1.1 به packet-up برمی‌گردد
#
# موتور تطبیقی مشترک بین هر چهار مد:
#   QuotaGate (حسابرسی batch شده، از speed_limit) + _AdaptiveFlow (AIMD روی high-water)
#   + سوکت تیون‌شده (TCP_NODELAY و بافرهای بزرگ).
# منطق relay_vless دست‌نخورده باقی مانده است.
# ══════════════════════════════════════════════════════════════════════════════

import asyncio
import secrets
import socket
import time
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

from main import (
    LINKS,
    LINKS_LOCK,
    stats,
    hourly_traffic,
    connections,
    error_logs,
    logger,
    is_link_allowed,
    is_ip_allowed,
    save_state,
)
from relay_vless import parse_vless_header, check_and_use
from speed_limit import throttle, QuotaGate

router = APIRouter()

# ── ترابرد ───────────────────────────────────────────────────────────────────
MODES = ("packet-up", "stream-up", "stream-one", "auto")
DOWNLINK_MODES = ("packet-up", "stream-up", "auto")   # مدهایی که دانلینک GET جدا دارند
SEQ_UPLOAD_MODES = ("packet-up", "auto")              # آپلینک پکتی با seq
DUPLEX_MODES = ("stream-one", "auto")                 # یک POST دوطرفه

XHTTP_BUF = 512 * 1024
DOWNLINK_QUEUE_MAX = 512
REAPER_INTERVAL = 10
TCP_CONNECT_TIMEOUT = 10.0

# دو تایم‌اوت جداگانه (دقت بیشتر نسبت به تایم‌اوت تکی قبلی):
#   • سشنی که هنوز TCP باز نکرده (فقط دست‌دادن ناقص) زود جمع می‌شود
#   • سشن با TCP باز تا مدت طولانی زنده می‌ماند (قبلاً هرگز جمع نمی‌شد → نشتی حافظه)
SESSION_IDLE_TIMEOUT = 30
ACTIVE_IDLE_TIMEOUT = 30 * 60

MAX_SESSIONS = 5000          # سقف سشن هم‌زمان (دفاع در برابر سوءاستفاده)
MAX_SESSION_ID_LEN = 64
MAX_SEQ_BUFFER = 128         # سقف پکت خارج از ترتیب در packet-up

# ── تنظیمات موتور تطبیقی ──────────────────────────────────────────────────
SOCK_BUF_SIZE = 4 * 1024 * 1024     # SO_SNDBUF / SO_RCVBUF

FLOW_MIN_HW = 256 * 1024
FLOW_MAX_HW = 16 * 1024 * 1024
FLOW_START_HW = 2 * 1024 * 1024
FLOW_FAST_DRAIN_MS = 2.0
FLOW_SLOW_DRAIN_MS = 25.0

xhttp_sessions: dict = {}
XHTTP_LOCK = asyncio.Lock()

FINGERPRINTS = {
    "chrome": {
        "content-type": "application/grpc",
        "cache-control": "no-cache, no-store",
        "x-accel-buffering": "no",
        "server": "cloudflare",
    },
    "plain": {
        "content-type": "application/octet-stream",
        "cache-control": "no-store",
        "x-accel-buffering": "no",
    },
}
DEFAULT_FINGERPRINT = "chrome"


def _resp_headers(fp: str) -> dict:
    """هدرهای پاسخ + پدینگ تصادفی.
    پدینگ هم الگوی طول پاسخ را می‌شکند و هم بعضی پراکسی‌های میانی را وادار می‌کند
    بافر نکنند و داده را بلافاصله فوروارد کنند."""
    h = dict(FINGERPRINTS.get(fp, FINGERPRINTS[DEFAULT_FINGERPRINT]))
    h["x-padding"] = "0" * secrets.choice(range(100, 800))
    return h


def _tune_socket(writer: asyncio.StreamWriter):
    """تیون کامل سوکت مقصد — دقیقاً همان چیزی که در مسیر WebSocket اعمال می‌شود:
    NODELAY + بافرهای بزرگ + QUICKACK + BBR + NOTSENT_LOWAT + IPTOS_LOWDELAY."""
    transport = getattr(writer, "transport", None)
    sock = transport.get_extra_info("socket") if transport else None
    if not sock:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUF_SIZE)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUF_SIZE)
    except OSError:
        pass
    quickack = getattr(socket, "TCP_QUICKACK", None)
    if quickack is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, quickack, 1)
        except OSError:
            pass
    cc_opt = getattr(socket, "TCP_CONGESTION", None)
    if cc_opt is not None:
        for cc in (b"bbr", b"cubic"):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, cc_opt, cc)
                break
            except OSError:
                continue
    lowat = getattr(socket, "TCP_NOTSENT_LOWAT", None)
    if lowat is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, lowat, 512 * 1024)
        except OSError:
            pass
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x10)
    except OSError:
        pass


def _new_gate(uuid: str) -> QuotaGate:
    return QuotaGate(uuid, check_and_use)


class _AdaptiveFlow:
    """
    high-water تطبیقی برای drain()، شبیه AIMD در کنترل ازدحام TCP:
      • drain سریع → سقف بافر مجاز additive increase می‌شود → syscall کمتر → سرعت بیشتر.
      • drain کند (backpressure واقعی) → سقف نصف می‌شود → جلوگیری از bufferbloat.
    هر سشن نمونه‌ی جداگانه دارد، پس مسیرهای کند و سریع با هم تداخل ندارند.
    """

    __slots__ = ("high_water", "last_drain_ms")

    def __init__(self):
        self.high_water = FLOW_START_HW
        self.last_drain_ms = 0.0

    def should_drain(self, buf_size: int) -> bool:
        return buf_size > self.high_water

    async def drain(self, writer: asyncio.StreamWriter):
        t0 = time.monotonic()
        await writer.drain()
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.last_drain_ms = elapsed_ms
        if elapsed_ms < FLOW_FAST_DRAIN_MS:
            self.high_water = min(FLOW_MAX_HW, int(self.high_water * 1.5) + 65536)
        elif elapsed_ms > FLOW_SLOW_DRAIN_MS:
            self.high_water = max(FLOW_MIN_HW, self.high_water // 2)


def _req_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "نامشخص"


def _validate(mode: str, session_id: str):
    if mode not in MODES:
        raise HTTPException(status_code=404, detail="unknown mode")
    if not session_id or len(session_id) > MAX_SESSION_ID_LEN:
        raise HTTPException(status_code=400, detail="bad session id")


async def _open_tcp_from_header(first_chunk: bytes):
    command, address, port, payload = await parse_vless_header(first_chunk)
    reader, writer = await asyncio.wait_for(
        # limit بزرگ: پیش‌فرض StreamReader فقط ۶۴KB است و روی لینک پرسرعت گلوگاه می‌شود
        asyncio.open_connection(address, port, limit=XHTTP_BUF * 8),
        timeout=TCP_CONNECT_TIMEOUT,
    )
    _tune_socket(writer)
    if payload:
        writer.write(payload)
        await writer.drain()
    return reader, writer, address, port


async def _check_link(uuid: str):
    async with LINKS_LOCK:
        link = LINKS.get(uuid)
    if not is_link_allowed(link):
        raise HTTPException(status_code=403, detail="not authorized")


async def _get_or_create_session(uuid: str, mode: str, session_id: str, ip: str = "نامشخص") -> dict:
    """Session بر اساس session_id که خودِ کلاینت در URL فرستاده، lazily ساخته می‌شود."""
    async with XHTTP_LOCK:
        sess = xhttp_sessions.get(session_id)
        if sess is not None:
            # همان session_id نباید با UUID دیگری دوباره استفاده شود
            if sess["uuid"] != uuid:
                raise HTTPException(status_code=403, detail="session mismatch")
            sess["last_seen"] = time.time()
            return sess

        if len(xhttp_sessions) >= MAX_SESSIONS:
            raise HTTPException(status_code=503, detail="too many sessions")

        async with LINKS_LOCK:
            link = LINKS.get(uuid)
        if not is_ip_allowed(link, uuid, ip):
            logger.warning(f"🚫 XHTTP[{mode}] rejected uuid={uuid[:8]} ip={ip} (ip limit reached)")
            raise HTTPException(status_code=403, detail="ip limit reached")

        conn_id = secrets.token_urlsafe(6)
        connections[conn_id] = {
            "uuid": uuid,
            "ip": ip,
            "connected_at": datetime.now().isoformat(),
            "bytes": 0,
            "transport": f"xhttp-{mode}",
        }
        sess = {
            "uuid": uuid, "mode": mode, "writer": None,
            "downlink_task": None, "uplink_task": None,
            "down_q": asyncio.Queue(maxsize=DOWNLINK_QUEUE_MAX),
            "last_seen": time.time(),
            "conn_id": conn_id, "tcp_open": False, "closed": False,
            "seq_buf": {}, "next_seq": 0,
            "gate": None,   # QuotaGate تطبیقی آپلینک (لازی)
            "flow": None,   # _AdaptiveFlow آپلینک (لازی)
            "open_lock": asyncio.Lock(),
        }
        xhttp_sessions[session_id] = sess
        logger.info(f"new XHTTP[{mode}] session [{session_id[:8]}] uuid={uuid[:8]} ip={ip}")
        return sess


def _session_gate(sess: dict, uuid: str) -> QuotaGate:
    gate = sess.get("gate")
    if gate is None:
        gate = _new_gate(uuid)
        sess["gate"] = gate
    return gate


def _session_flow(sess: dict) -> _AdaptiveFlow:
    flow = sess.get("flow")
    if flow is None:
        flow = _AdaptiveFlow()
        sess["flow"] = flow
    return flow


async def _teardown(session_id: str):
    async with XHTTP_LOCK:
        sess = xhttp_sessions.pop(session_id, None)
    if not sess:
        return
    sess["closed"] = True

    # حسابرسی نهایی کوتا قبل از بستن (دقت مصرف)
    gate = sess.get("gate")
    if gate is not None:
        try:
            await gate.flush()
        except Exception:
            pass

    current = asyncio.current_task()
    for key in ("uplink_task", "downlink_task"):
        task = sess.get(key)
        # تسکی که خودش دارد teardown را صدا می‌زند نباید منتظر خودش بماند (ددلاک)
        if task and task is not current and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    writer = sess.get("writer")
    if writer:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    connections.pop(sess.get("conn_id"), None)
    sess["seq_buf"] = {}
    dq = sess.get("down_q")
    if dq:
        try:
            dq.put_nowait(None)   # پایان جریان دانلینک
        except Exception:
            pass
    logger.info(f"closed XHTTP[{sess.get('mode')}] [{session_id[:8]}] total={len(xhttp_sessions)}")


async def _reaper():
    while True:
        await asyncio.sleep(REAPER_INTERVAL)
        try:
            now = time.time()
            async with XHTTP_LOCK:
                stale = []
                for sid, s in xhttp_sessions.items():
                    idle = now - s["last_seen"]
                    limit = ACTIVE_IDLE_TIMEOUT if s.get("tcp_open") else SESSION_IDLE_TIMEOUT
                    if idle > limit:
                        stale.append(sid)
            for sid in stale:
                await _teardown(sid)
        except Exception as exc:
            logger.warning(f"xhttp reaper error: {exc}")


_reaper_started = False


def ensure_reaper():
    global _reaper_started
    if not _reaper_started:
        asyncio.create_task(_reaper())
        _reaper_started = True


async def _pump_tcp_to_queue(session_id: str, uuid: str, reader: asyncio.StreamReader, down_q: asyncio.Queue):
    """دانلینک: TCP → صف؛ مشترک بین هر چهار مد."""
    first = True
    gate = _new_gate(uuid)
    conn = connections.get((xhttp_sessions.get(session_id) or {}).get("conn_id"))
    try:
        while True:
            data = await reader.read(XHTTP_BUF)
            if not data:
                break
            n = len(data)
            if not await gate.add(n):
                break
            await throttle(uuid, n)
            if conn is not None:
                conn["bytes"] += n
            payload = (b"\x00\x00" + data) if first else data
            first = False
            await down_q.put(payload)
    except (asyncio.CancelledError, Exception):
        pass
    finally:
        try:
            await gate.flush()
        except Exception:
            pass
        await _teardown(session_id)


async def _open_tcp_for_session(session_id: str, uuid: str, sess: dict, first_chunk: bytes):
    """تونل TCP را از روی هدر VLESS باز می‌کند و پمپ دانلینک را راه می‌اندازد.
    با قفل مخصوص سشن، تا دو درخواست هم‌زمان (مثلاً در auto) دو تونل موازی نسازند."""
    async with sess["open_lock"]:
        if sess.get("writer") is not None or sess.get("closed"):
            return
        reader, writer, address, port = await _open_tcp_from_header(first_chunk)
        logger.info(f"connect XHTTP[{sess['mode']}] [{session_id[:8]}] -> {address}:{port}")
        sess["writer"] = writer
        sess["tcp_open"] = True
        sess["downlink_task"] = asyncio.create_task(
            _pump_tcp_to_queue(session_id, uuid, reader, sess["down_q"])
        )
        asyncio.create_task(save_state())


def _downstream_gen(sess: dict):
    async def gen():
        q = sess["down_q"]
        while True:
            chunk = await q.get()
            if chunk is None:
                break
            sess["last_seen"] = time.time()
            yield chunk
    return gen()


# ════════════════════════════ پمپ آپلینک جریانی (مشترک stream-up و stream-one) ═══════════
async def _pump_request_to_tcp(uuid: str, session_id: str, sess: dict, request: Request):
    """هیچ داده‌ای بافر/coalesce نمی‌شود — هر بایت فوری write() می‌شود؛
    فقط «کی برای drain صبر کنیم» و «کی کوتا را حساب کنیم» تطبیقی است."""
    gate = _session_gate(sess, uuid)
    flow = _session_flow(sess)
    conn = connections.get(sess["conn_id"])
    writer = sess.get("writer")

    async for chunk in request.stream():
        if not chunk:
            continue
        n = len(chunk)
        sess["last_seen"] = time.time()

        if not await gate.add(n):
            raise HTTPException(status_code=403, detail="quota/disabled/unknown")
        await throttle(uuid, n)

        if conn is not None:
            conn["bytes"] += n

        if writer is None:
            await _open_tcp_for_session(session_id, uuid, sess, chunk)
            writer = sess.get("writer")
            continue

        writer.write(chunk)
        if flow.should_drain(writer.transport.get_write_buffer_size()):
            await flow.drain(writer)


# ════════════════════════════ GET دانلینک (packet-up / stream-up / auto) ════════════��════
@router.get("/xhttp-siz10/{mode}/{uuid}/{session_id}")
async def xhttp_downlink(mode: str, uuid: str, session_id: str, request: Request):
    ensure_reaper()
    _validate(mode, session_id)
    if mode not in DOWNLINK_MODES:
        raise HTTPException(status_code=404, detail="mode has no separate downlink")
    await _check_link(uuid)

    fp = request.query_params.get("fp", DEFAULT_FINGERPRINT)
    sess = await _get_or_create_session(uuid, mode, session_id, _req_client_ip(request))
    if sess.get("closed"):
        raise HTTPException(status_code=404, detail="session closed")

    stats["total_requests"] += 1
    headers = _resp_headers(fp)
    return StreamingResponse(_downstream_gen(sess), headers=headers, media_type=headers["content-type"])


# ════════════════════════════ PACKET-UP آپلینک با seq (packet-up / auto) ═══════════════════
@router.post("/xhttp-siz10/{mode}/{uuid}/{session_id}/{seq}")
async def xhttp_packet_up(mode: str, uuid: str, session_id: str, seq: int, request: Request):
    ensure_reaper()
    _validate(mode, session_id)
    if mode not in SEQ_UPLOAD_MODES:
        raise HTTPException(status_code=404, detail="mode does not accept packet upload")
    await _check_link(uuid)

    sess = await _get_or_create_session(uuid, mode, session_id, _req_client_ip(request))
    if sess.get("closed"):
        raise HTTPException(status_code=404, detail="session closed")

    sess["last_seen"] = time.time()
    body = await request.body()
    if not body:
        return {"ok": True}

    n = len(body)
    gate = _session_gate(sess, uuid)
    if not await gate.add(n):
        await _teardown(session_id)
        raise HTTPException(status_code=403, detail="quota/disabled/unknown")
    await throttle(uuid, n)

    stats["total_requests"] += 1
    conn = connections.get(sess["conn_id"])
    if conn is not None:
        conn["bytes"] += n

    flow = _session_flow(sess)
    try:
        if sess.get("writer") is None:
            # اولین پکت حاوی هدر VLESS است؛ پکت‌های زودرس بافر می‌شوند
            if seq != 0:
                if len(sess["seq_buf"]) >= MAX_SEQ_BUFFER:
                    await _teardown(session_id)
                    raise HTTPException(status_code=400, detail="seq buffer overflow")
                sess["seq_buf"][seq] = body
                return {"ok": True, "buffered": True}

            await _open_tcp_for_session(session_id, uuid, sess, body)
            writer = sess.get("writer")
            if writer is None:
                raise HTTPException(status_code=502, detail="tunnel not open")
            nxt = 1
            while nxt in sess["seq_buf"]:
                writer.write(sess["seq_buf"].pop(nxt))
                nxt += 1
            sess["next_seq"] = nxt
            if flow.should_drain(writer.transport.get_write_buffer_size()):
                await flow.drain(writer)
            return {"ok": True, "connected": True}

        writer = sess["writer"]
        if seq == sess["next_seq"]:
            writer.write(body)
            sess["next_seq"] += 1
            while sess["next_seq"] in sess["seq_buf"]:
                writer.write(sess["seq_buf"].pop(sess["next_seq"]))
                sess["next_seq"] += 1
        else:
            if len(sess["seq_buf"]) >= MAX_SEQ_BUFFER:
                await _teardown(session_id)
                raise HTTPException(status_code=400, detail="seq buffer overflow")
            sess["seq_buf"][seq] = body

        if flow.should_drain(writer.transport.get_write_buffer_size()):
            await flow.drain(writer)
    except HTTPException:
        raise
    except Exception as exc:
        stats["total_errors"] += 1
        error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
        await _teardown(session_id)
        raise HTTPException(status_code=502, detail="write failed")

    return {"ok": True}


# ══════════════════════ POST سه‌بخشی: stream-up (آپلینک) / stream-one و auto (دوطرفه) ═══════
@router.post("/xhttp-siz10/{mode}/{uuid}/{session_id}")
async def xhttp_stream_upload(mode: str, uuid: str, session_id: str, request: Request):
    ensure_reaper()
    _validate(mode, session_id)
    if mode == "packet-up":
        raise HTTPException(status_code=404, detail="packet-up requires a seq segment")
    await _check_link(uuid)

    sess = await _get_or_create_session(uuid, mode, session_id, _req_client_ip(request))
    if sess.get("closed"):
        raise HTTPException(status_code=404, detail="session closed")

    stats["total_requests"] += 1

    if mode in DUPLEX_MODES:
        return _stream_one_response(uuid, session_id, sess, request)

    # ── stream-up: فقط آپلینک؛ دانلینک روی GET جداگانه می‌آید ──
    try:
        await _pump_request_to_tcp(uuid, session_id, sess, request)
    except HTTPException:
        await _teardown(session_id)
        raise
    except Exception as exc:
        stats["total_errors"] += 1
        error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
        await _teardown(session_id)
        raise HTTPException(status_code=502, detail="stream error")

    gate = _session_gate(sess, uuid)
    await gate.flush()
    return {"ok": True}


# ═════════════════════════ STREAM-ONE — یک درخواست POST دوطرفه ════════════════════════
def _stream_one_response(uuid: str, session_id: str, sess: dict, request: Request) -> StreamingResponse:
    """
    در stream-one کل تونل داخل یک درخواست HTTP جا می‌شود:
    بدنه‌ی درخواست = آپلینک، بدنه‌ی پاسخ = دانلینک. دو طرف کاملاً موازی اجرا می‌شوند
    (یک تسک برای آپلینک + ژنراتور پاسخ برای دانلینک)، پس یک RTT کمتر از packet-up/stream-up
    دارد و برای مرور وب و بازی محسوساً سریع‌تر است. نیازمند HTTP/2 (یا H3) است.
    """
    fp = request.query_params.get("fp", DEFAULT_FINGERPRINT)
    headers = _resp_headers(fp)

    async def uplink():
        try:
            await _pump_request_to_tcp(uuid, session_id, sess, request)
        except asyncio.CancelledError:
            raise
        except HTTPException:
            pass
        except Exception as exc:
            stats["total_errors"] += 1
            error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
        finally:
            # پایان بدنه‌ی درخواست = کلاینت دیگر چیزی نمی‌فرستد → سشن را جمع کن
            await _teardown(session_id)

    async def duplex():
        task = asyncio.create_task(uplink())
        sess["uplink_task"] = task
        q = sess["down_q"]
        try:
            while True:
                chunk = await q.get()
                if chunk is None:
                    break
                sess["last_seen"] = time.time()
                yield chunk
        finally:
            if not task.done():
                task.cancel()
            await _teardown(session_id)

    return StreamingResponse(duplex(), headers=headers, media_type=headers["content-type"])
