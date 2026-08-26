# outbound.py
# ── لایه‌ی «آی‌پی خروجی» (Exit IP) ────────────────────────────────────────────
#
# این ماژول همان مکانیزمی را پیاده می‌کند که مخزن cmliu/edgetunnel برای
# «تنظیم آی‌پی خروجی» استفاده می‌کند:
#
#   1) ProxyIP / 反代  (mode="proxyip")
#      به‌جای اتصال مستقیم به مقصد، به یک «ریلی معکوس» (reverse proxy) وصل
#      می‌شویم و *همان اولین بسته‌ی خام کلاینت* را بدون تغییر برایش می‌نویسیم.
#      آن ریلی بر اساس SNI داخل ClientHello مقصد را پیدا می‌کند و خودش به
#      مقصد وصل می‌شود؛ در نتیجه آی‌پی‌ای که سایت مقصد می‌بیند آی‌پی همان
#      ProxyIP است، نه آی‌پی سرور ما. دقیقاً رفتار connectProxyIP در مرجع.
#
#   2) پروکسی زنجیره‌ای SOCKS5 / HTTP / HTTPS  (mode="socks5"|"http"|"https")
#      معادل socks5Connect / httpConnect / httpsConnect در مرجع. اینجا آدرس
#      واقعی مقصد به پروکسی اعلام می‌شود، پس برای هر پورت و هر پروتکلی
#      (نه فقط TLS) آی‌پی خروجی تضمین‌شده عوض می‌شود.
#
# معادل‌های تنظیمات مرجع:
#   PROXYIP               -> settings["proxyip"]
#   PROXY_CONCURRENT_DIAL -> settings["concurrency"]
#   GO2SOCKS5             -> settings["force_hosts"]
#   SOCKS5 / HTTP(S)      -> settings["proxy_url"]
#   反代兜底 (fallback)    -> settings["fallback"]
#   代理全局 (global)      -> settings["global_proxy"]
#
# نکته‌ی مهندسی: این ماژول عمداً هیچ چیزی از main.py یا relay_vless.py import
# نمی‌کند تا حلقه‌ی import ایجاد نشود. مسیرِ شماره‌گیری (dialer) و تیونر سوکت
# از بیرون با set_dialer()/set_tuner() تزریق می‌شوند تا Happy-Eyeballs و
# حافظه‌ی مسیرِ relay_vless برای اتصال‌های پروکسی هم حفظ شود.

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import logging
import os
import re
import socket
import ssl
import time
from typing import Any, Awaitable, Callable, Iterable

try:  # httpx فقط برای DoH لازم است؛ در نبودش به getaddrinfo برمی‌گردیم.
    import httpx  # type: ignore
except Exception:  # pragma: no cover - محیط بدون httpx
    httpx = None  # type: ignore

logger = logging.getLogger("X4G.outbound")

# ── ثابت‌ها ──────────────────────────────────────────────────────────────────
MODES = ("direct", "proxyip", "socks5", "http", "https")

DEFAULT_PROXY_PORT = 443           # پورت پیش‌فرض ProxyIP، مثل مرجع
PROXY_DEFAULT_PORTS = {"socks5": 1080, "http": 80, "https": 443}

MAX_POOL = 8                       # مرجع هم لیست را به ۸ کاندید می‌بُرد
POOL_TTL = 300.0
POOL_CACHE_MAX = 512
DOH_TIMEOUT = 4.0
DIAL_TIMEOUT = 10.0
HANDSHAKE_TIMEOUT = 10.0
CONNECT_HEADER_MAX = 8 * 1024

_DOH_ENDPOINTS = (
    "https:" + "//1.1.1.1/dns-query",
    "https:" + "//dns.google/resolve",
)

_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_TP_PORT_RE = re.compile(r"\.tp(\d+)")
_SPLIT_RE = re.compile(r"[\s\"']+")
_MULTI_COMMA_RE = re.compile(r",+")
_B64_RE = re.compile(r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$")


# ── تنظیمات ──────────────────────────────────────────────────────────────────
def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(str(os.environ.get(name, default)).strip()))
    except Exception:
        return default


def _default_mode(proxyip: str, proxy_url: str) -> str:
    explicit = str(os.environ.get("OUTBOUND_MODE", "")).strip().lower()
    if explicit in MODES:
        return explicit
    if proxy_url:
        scheme = proxy_url.split("://", 1)[0].lower() if "://" in proxy_url else "socks5"
        return scheme if scheme in ("socks5", "http", "https") else "socks5"
    if proxyip:
        return "proxyip"
    return "direct"


def _initial_settings() -> dict:
    proxyip = str(os.environ.get("PROXYIP", "")).strip()
    proxy_url = str(
        os.environ.get("SOCKS5")
        or os.environ.get("OUTBOUND_PROXY")
        or ""
    ).strip()
    return {
        "mode": _default_mode(proxyip, proxy_url),
        "proxyip": proxyip,
        "concurrency": _env_int("PROXY_CONCURRENT_DIAL", 1),
        "fallback": _env_bool("PROXYIP_FALLBACK", True),
        "global_proxy": _env_bool("PROXY_GLOBAL", False),
        "force_hosts": str(os.environ.get("GO2SOCKS5", "")).strip(),
        "proxy_url": proxy_url,
    }


SETTINGS: dict = _initial_settings()

_pool_cache: dict[tuple, tuple[float, list[tuple[str, int]]]] = {}
_pool_index = 0
_force_patterns: tuple[re.Pattern, ...] = ()
_force_patterns_src: str | None = None
_doh_client: Any = None


def reset_caches() -> None:
    global _force_patterns, _force_patterns_src
    _pool_cache.clear()
    _force_patterns = ()
    _force_patterns_src = None


def configure(**kwargs) -> dict:
    """به‌روزرسانی امن تنظیمات (از پنل، ربات یا state روی دیسک)."""
    changed = False
    for key, value in kwargs.items():
        if key not in SETTINGS:
            continue
        if key == "mode":
            new = str(value or "direct").strip().lower()
            if new not in MODES:
                continue
        elif key == "concurrency":
            try:
                new = max(1, min(16, int(value)))
            except Exception:
                continue
        elif key in ("fallback", "global_proxy"):
            new = bool(value)
        else:
            new = str(value or "").strip()
        if SETTINGS[key] != new:
            SETTINGS[key] = new
            changed = True
    if changed:
        reset_caches()
    return dict(SETTINGS)


def export_settings() -> dict:
    return dict(SETTINGS)


def settings_summary() -> dict:
    """نسخه‌ی نمایشی؛ رمز پروکسی ماسک می‌شود."""
    out = dict(SETTINGS)
    url = out.get("proxy_url") or ""
    if url:
        try:
            parsed = parse_proxy_url(url, out.get("mode", "socks5"))
            host = parsed["hostname"]
            port = parsed["port"]
            user = parsed.get("username") or ""
            masked = (user + ":***@") if user else ""
            out["proxy_url"] = masked + host + ":" + str(port)
        except Exception:
            out["proxy_url"] = "(invalid)"
    out["pool_preview"] = [
        host + ":" + str(port) for host, port in parse_endpoint_list(SETTINGS["proxyip"])
    ][:MAX_POOL]
    return out


def is_active() -> bool:
    mode = SETTINGS.get("mode", "direct")
    if mode == "proxyip":
        return bool(SETTINGS.get("proxyip"))
    if mode in ("socks5", "http", "https"):
        return bool(SETTINGS.get("proxy_url"))
    return False


# ── تزریق dialer/tuner از relay_vless ───────────────────────────────────────
Dialer = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def _default_dialer(host: str, port: int):
    return await asyncio.open_connection(host, port)


_dialer: Dialer = _default_dialer
_tuner: Callable[[asyncio.StreamWriter], None] | None = None


def set_dialer(fn: Dialer) -> None:
    global _dialer
    _dialer = fn


def set_tuner(fn: Callable[[asyncio.StreamWriter], None]) -> None:
    global _tuner
    _tuner = fn


def _tune(writer: asyncio.StreamWriter) -> None:
    if _tuner is None:
        return
    try:
        _tuner(writer)
    except Exception:
        pass


# ── کمک‌کارهای آدرس ─────────────────────────────────────────────────────────
def strip_brackets(host: str) -> str:
    host = str(host or "").strip()
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def is_ipv4(value: str) -> bool:
    value = str(value or "").strip()
    if not _IPV4_RE.match(value):
        return False
    try:
        ipaddress.IPv4Address(value)
        return True
    except Exception:
        return False


def is_ipv6(value: str) -> bool:
    try:
        ipaddress.IPv6Address(strip_brackets(value).split("%")[0])
        return True
    except Exception:
        return False


def is_ip(value: str) -> bool:
    return is_ipv4(value) or is_ipv6(value)


def normalize_list(text: Any) -> list[str]:
    """معادل 整理成数组 در مرجع (با کمی تحمل بیشتر برای فاصله)."""
    if not text:
        return []
    if isinstance(text, (list, tuple, set)):
        raw = ",".join(str(item) for item in text)
    else:
        raw = str(text)
    raw = _SPLIT_RE.sub(",", raw)
    raw = _MULTI_COMMA_RE.sub(",", raw).strip(",")
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_endpoint_string(
    raw: Any, default_port: int = DEFAULT_PROXY_PORT
) -> tuple[str, int] | None:
    """معادل 解析地址端口字符串: پشتیبانی از host، host:port، [v6]:port و ‎.tpNNN."""
    value = str(raw or "").strip().lower()
    if not value:
        return None
    value = value.split("#")[0].strip()
    if not value:
        return None

    port = default_port
    tp = _TP_PORT_RE.search(value)
    if tp:
        try:
            candidate = int(tp.group(1))
            if 0 < candidate < 65536:
                port = candidate
        except Exception:
            pass

    if "]:" in value:
        host, _, tail = value.rpartition("]:")
        host = host + "]"
        digits = re.sub(r"\D", "", tail)
        if digits:
            try:
                port = int(digits)
            except Exception:
                pass
        return (host, port) if 0 < port < 65536 else None

    if value.count(":") == 1 and not value.startswith("["):
        host, _, tail = value.rpartition(":")
        digits = re.sub(r"\D", "", tail)
        if digits:
            try:
                port = int(digits)
            except Exception:
                pass
        if host:
            return (host, port) if 0 < port < 65536 else None

    if value.count(":") > 1 and not value.startswith("[") and is_ipv6(value):
        return ("[" + value + "]", port)

    return (value, port) if 0 < port < 65536 else None


def parse_endpoint_list(
    raw: Any, default_port: int = DEFAULT_PROXY_PORT
) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for item in normalize_list(raw):
        parsed = parse_endpoint_string(item, default_port)
        if parsed and parsed not in seen:
            seen.add(parsed)
            out.append(parsed)
    return out


def parse_proxy_url(raw: Any, mode: str = "socks5") -> dict:
    """معادل 获取SOCKS5账号: user:pass@host:port با پشتیبانی base64 و IPv6."""
    value = str(raw or "").strip()
    if not value:
        raise ValueError("empty proxy url")
    scheme = mode
    match = re.match(r"^(socks5|socks|http|https)://", value, re.IGNORECASE)
    if match:
        scheme = match.group(1).lower()
        if scheme == "socks":
            scheme = "socks5"
        value = value[match.end():]
    value = value.split("#")[0].strip()

    at = value.rfind("@")
    if at != -1:
        auth = value[:at].replace("%3D", "=").replace("%3d", "=")
        if ":" not in auth and _B64_RE.match(auth) and len(auth) % 4 == 0:
            try:
                auth = base64.b64decode(auth).decode("utf-8", "strict")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                pass
        value = auth + "@" + value[at + 1:]

    at = value.rfind("@")
    host_part = (value if at == -1 else value[at + 1:]).split("/")[0]
    auth_part = "" if at == -1 else value[:at]

    username = password = ""
    if auth_part:
        if ":" not in auth_part:
            raise ValueError('proxy auth must be "username:password"')
        username, _, password = auth_part.partition(":")

    default_port = PROXY_DEFAULT_PORTS.get(scheme, 80)
    endpoint = parse_endpoint_string(host_part, default_port)
    if not endpoint:
        raise ValueError("invalid proxy host")
    hostname, port = endpoint
    return {
        "scheme": scheme,
        "hostname": hostname,
        "port": port,
        "username": username,
        "password": password,
    }


def _root_domain(host: str) -> str:
    host = strip_brackets(str(host or "").strip().lower())
    if not host or is_ip(host):
        return host
    labels = [part for part in host.split(".") if part]
    if len(labels) <= 2:
        return ".".join(labels)
    return ".".join(labels[-2:])


def _seeded_shuffle(items: list, seed_text: str) -> list:
    """چینش قطعی بر پایه‌ی (دامنه‌ی ریشه‌ی مقصد + UUID) مثل مرجع.

    هدف، پایداری نشست است: یک سایت مشخص برای یک کاربر مشخص همیشه روی همان
    ProxyIP می‌نشیند و وسط کار آی‌پی خروجی‌اش عوض نمی‌شود.
    """
    result = list(items)
    if len(result) < 2:
        return result
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    state = int.from_bytes(digest[:8], "big") or 0x9E3779B97F4A7C15
    for index in range(len(result) - 1, 0, -1):
        # xorshift64* — قطعی، سریع و مستقل از نسخه‌ی پایتون
        state ^= (state >> 12) & 0xFFFFFFFFFFFFFFFF
        state ^= (state << 25) & 0xFFFFFFFFFFFFFFFF
        state ^= (state >> 27) & 0xFFFFFFFFFFFFFFFF
        state &= 0xFFFFFFFFFFFFFFFF
        pick = ((state * 0x2545F4914F6CDD1D) & 0xFFFFFFFFFFFFFFFF) % (index + 1)
        result[index], result[pick] = result[pick], result[index]
    return result


# ── DoH: TXT -> A -> AAAA (همان ترتیب مرجع) ─────────────────────────────────
async def _doh_answers(name: str, rtype: str) -> list[dict]:
    global _doh_client
    if httpx is None:
        return []
    if _doh_client is None:
        try:
            _doh_client = httpx.AsyncClient(timeout=DOH_TIMEOUT)
        except Exception:
            return []
    for endpoint in _DOH_ENDPOINTS:
        try:
            response = await _doh_client.get(
                endpoint,
                params={"name": name, "type": rtype},
                headers={"accept": "application/dns-json"},
            )
            if response.status_code != 200:
                continue
            payload = response.json()
            answers = payload.get("Answer") or []
            if answers:
                return [item for item in answers if isinstance(item, dict)]
        except Exception:
            continue
    return []


def _parse_txt_records(answers: Iterable[dict], default_port: int) -> list[tuple[str, int]]:
    """معادل 解析TXT反代记录: هر رکورد TXT می‌تواند چند host:port داشته باشد."""
    out: list[tuple[str, int]] = []
    for answer in answers:
        if int(answer.get("type", 0) or 0) != 16:
            continue
        data = str(answer.get("data", "") or "").strip().strip('"')
        if not data:
            continue
        data = data.replace("\\010", ",").replace("\\n", ",")
        for endpoint in parse_endpoint_list(data, default_port):
            if endpoint not in out:
                out.append(endpoint)
    return out


async def _resolve_endpoint(host: str, port: int) -> list[tuple[str, int]]:
    """یک ورودی ProxyIP را به کاندیدهای قابل شماره‌گیری تبدیل می‌کند."""
    if is_ip(host):
        if is_ipv6(host) and not host.startswith("["):
            return [("[" + host + "]", port)]
        return [(host, port)]

    txt = _parse_txt_records(await _doh_answers(host, "TXT"), port)
    if txt:
        return txt

    a_records = [
        str(item.get("data", "")).strip()
        for item in await _doh_answers(host, "A")
        if int(item.get("type", 0) or 0) == 1
    ]
    ipv4 = [(value, port) for value in a_records if is_ipv4(value)]
    if ipv4:
        return ipv4

    aaaa_records = [
        str(item.get("data", "")).strip()
        for item in await _doh_answers(host, "AAAA")
        if int(item.get("type", 0) or 0) == 28
    ]
    ipv6 = [("[" + value + "]", port) for value in aaaa_records if is_ipv6(value)]
    if ipv6:
        return ipv6

    if httpx is None:
        # بدون httpx از resolver سیستمی استفاده می‌کنیم (TXT پشتیبانی نمی‌شود).
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except Exception:
            infos = []
        system: list[tuple[str, int]] = []
        for family, _type, _proto, _canon, sockaddr in infos:
            address = sockaddr[0]
            if family == socket.AF_INET6:
                candidate = ("[" + address + "]", port)
            else:
                candidate = (address, port)
            if candidate not in system:
                system.append(candidate)
        if system:
            return system

    # مثل مرجع: اگر هیچ رکوردی پیدا نشد، خودِ دامنه را نگه می‌داریم.
    return [(host, port)]


async def resolve_pool(
    raw: Any, target_host: str = "", uuid: str = ""
) -> list[tuple[str, int]]:
    """لیست نهایی ProxyIP: resolve + مرتب‌سازی + چینش قطعی + حداکثر ۸ کاندید."""
    entries = parse_endpoint_list(raw)
    if not entries:
        return []

    root = _root_domain(target_host)
    cache_key = (str(raw), root, str(uuid))
    now = time.monotonic()
    hit = _pool_cache.get(cache_key)
    if hit and hit[0] > now:
        return list(hit[1])

    resolved: list[tuple[str, int]] = []
    results = await asyncio.gather(
        *(_resolve_endpoint(host, port) for host, port in entries),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            continue
        for candidate in result:
            if candidate not in resolved:
                resolved.append(candidate)

    resolved.sort()
    pool = _seeded_shuffle(resolved, root + "|" + str(uuid))[:MAX_POOL]

    if len(_pool_cache) >= POOL_CACHE_MAX:
        _pool_cache.clear()
    _pool_cache[cache_key] = (now + POOL_TTL, list(pool))
    return list(pool)


# ── لیست میزبان‌های اجباری (معادل GO2SOCKS5) ────────────────────────────────
def _compiled_force_patterns() -> tuple[re.Pattern, ...]:
    global _force_patterns, _force_patterns_src
    source = str(SETTINGS.get("force_hosts") or "")
    if _force_patterns_src == source:
        return _force_patterns
    patterns: list[re.Pattern] = []
    for item in normalize_list(source):
        escaped = re.escape(item).replace(r"\*", ".*")
        try:
            patterns.append(re.compile("^" + escaped + "$", re.IGNORECASE))
        except re.error:
            continue
    _force_patterns = tuple(patterns)
    _force_patterns_src = source
    return _force_patterns


def host_forces_proxy(host: str) -> bool:
    target = strip_brackets(str(host or "").strip().lower())
    if not target:
        return False
    return any(pattern.match(target) for pattern in _compiled_force_patterns())


def _effective(link: dict | None) -> dict:
    """تنظیمات مؤثر: پیش‌فرض سراسری + override اختصاصی هر کانفیگ."""
    effective = dict(SETTINGS)
    if isinstance(link, dict):
        override = link.get("outbound")
        if isinstance(override, dict):
            for key, value in override.items():
                if key in effective and value not in (None, ""):
                    effective[key] = value
        legacy = link.get("proxyip")
        if isinstance(legacy, str) and legacy.strip():
            effective["proxyip"] = legacy.strip()
            if effective.get("mode") == "direct":
                effective["mode"] = "proxyip"
    try:
        effective["concurrency"] = max(1, min(16, int(effective.get("concurrency", 1))))
    except Exception:
        effective["concurrency"] = 1
    return effective


# ── شماره‌گیری ──────────────────────────────────────────────────────────────
async def _dial(host: str, port: int):
    return await _dialer(strip_brackets(host), port)


async def _race_dial(candidates: list[tuple[str, int]]):
    """معادل 并发打开候选连接: اولین اتصال موفق برنده، بقیه بسته می‌شوند."""
    if len(candidates) == 1:
        host, port = candidates[0]
        async with asyncio.timeout(DIAL_TIMEOUT):
            reader, writer = await _dial(host, port)
        return reader, writer, (host, port)

    async def attempt(host: str, port: int):
        reader, writer = await _dial(host, port)
        return reader, writer, (host, port)

    pending = {
        asyncio.create_task(attempt(host, port)) for host, port in candidates
    }
    winner = None
    last_error: BaseException | None = None
    deadline = time.monotonic() + DIAL_TIMEOUT
    try:
        while pending and winner is None:
            done, pending = await asyncio.wait(
                pending,
                timeout=max(deadline - time.monotonic(), 0.01),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise asyncio.TimeoutError
            for task in done:
                try:
                    result = task.result()
                except Exception as exc:
                    last_error = exc
                    continue
                if winner is None:
                    winner = result
                else:
                    try:
                        result[1].close()
                    except Exception:
                        pass
    finally:
        for task in pending:
            task.cancel()
        if pending:
            for result in await asyncio.gather(*pending, return_exceptions=True):
                if isinstance(result, tuple):
                    try:
                        result[1].close()
                    except Exception:
                        pass
    if winner is None:
        raise last_error or OSError("proxy dial failed")
    return winner


async def connect_proxyip(
    pool: list[tuple[str, int]],
    first_packet: bytes | bytearray | memoryview | None,
    concurrency: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, tuple[str, int]]:
    """معادل connectProxyIP: دسته‌دسته، با اندیس چرخشی، و نوشتن بسته‌ی اول خام."""
    global _pool_index
    if not pool:
        raise OSError("empty proxyip pool")

    batch = max(1, int(concurrency))
    last_error: BaseException | None = None
    total = len(pool)
    for offset in range(0, total, batch):
        candidates: list[tuple[str, int]] = []
        indices: list[int] = []
        for step in range(batch):
            if offset + step >= total:
                break
            index = (_pool_index + offset + step) % total
            candidates.append(pool[index])
            indices.append(index)
        if not candidates:
            continue
        writer = None
        try:
            reader, writer, chosen = await _race_dial(candidates)
            _tune(writer)
            if first_packet:
                writer.write(bytes(first_packet))
                await writer.drain()
            _pool_index = indices[candidates.index(chosen)] if chosen in candidates else 0
            logger.info(
                "ProxyIP connected via %s:%d (pool=%d)", chosen[0], chosen[1], total
            )
            return reader, writer, chosen
        except Exception as exc:
            last_error = exc
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            logger.warning("ProxyIP batch failed: %s", exc)
    raise last_error or OSError("all proxyip candidates failed")


# ── SOCKS5 / HTTP CONNECT (معادل socks5Connect / httpConnect / httpsConnect) ─
def _socks5_target(host: str, port: int) -> bytes:
    bare = strip_brackets(host)
    if is_ipv4(bare):
        atyp = b"\x01" + ipaddress.IPv4Address(bare).packed
    elif is_ipv6(bare):
        atyp = b"\x04" + ipaddress.IPv6Address(bare.split("%")[0]).packed
    else:
        encoded = bare.encode("idna") if bare.isascii() is False else bare.encode()
        if len(encoded) > 255:
            raise ValueError("hostname too long for socks5")
        atyp = b"\x03" + bytes([len(encoded)]) + encoded
    return b"\x05\x01\x00" + atyp + port.to_bytes(2, "big")


async def _read_socks5_reply(reader: asyncio.StreamReader) -> None:
    head = await reader.readexactly(4)
    if head[0] != 0x05:
        raise OSError("socks5 bad version")
    if head[1] != 0x00:
        raise OSError("socks5 connect failed: code " + str(head[1]))
    atyp = head[3]
    if atyp == 0x01:
        await reader.readexactly(4 + 2)
    elif atyp == 0x04:
        await reader.readexactly(16 + 2)
    elif atyp == 0x03:
        length = (await reader.readexactly(1))[0]
        await reader.readexactly(length + 2)
    else:
        raise OSError("socks5 bad atyp")


async def socks5_connect(target_host, target_port, first_packet, params):
    reader, writer = await _dial(params["hostname"], params["port"])
    _tune(writer)
    try:
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            username = params.get("username") or ""
            password = params.get("password") or ""
            if username and password:
                writer.write(b"\x05\x02\x00\x02")
            else:
                writer.write(b"\x05\x01\x00")
            await writer.drain()

            greeting = await reader.readexactly(2)
            if greeting[0] != 0x05:
                raise OSError("socks5 method selection failed")
            method = greeting[1]
            if method == 0x02:
                if not (username and password):
                    raise OSError("socks5 requires authentication")
                user_bytes = username.encode()
                pass_bytes = password.encode()
                writer.write(
                    b"\x01"
                    + bytes([len(user_bytes)])
                    + user_bytes
                    + bytes([len(pass_bytes)])
                    + pass_bytes
                )
                await writer.drain()
                auth = await reader.readexactly(2)
                if auth[1] != 0x00:
                    raise OSError("socks5 authentication failed")
            elif method != 0x00:
                raise OSError("socks5 unsupported auth method: " + str(method))

            writer.write(_socks5_target(target_host, target_port))
            await writer.drain()
            await _read_socks5_reply(reader)

            if first_packet:
                writer.write(bytes(first_packet))
                await writer.drain()
        return reader, writer
    except BaseException:
        try:
            writer.close()
        except Exception:
            pass
        raise


def _connect_request(target_host: str, target_port: int, params: dict) -> bytes:
    authority = strip_brackets(target_host)
    if is_ipv6(authority):
        authority = "[" + authority + "]"
    authority = authority + ":" + str(target_port)
    lines = [
        "CONNECT " + authority + " HTTP/1.1",
        "Host: " + authority,
    ]
    username = params.get("username") or ""
    password = params.get("password") or ""
    if username and password:
        token = base64.b64encode((username + ":" + password).encode()).decode()
        lines.append("Proxy-Authorization: Basic " + token)
    lines.append("User-Agent: Mozilla/5.0")
    lines.append("Connection: keep-alive")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def http_connect(target_host, target_port, first_packet, params, use_tls=False):
    reader, writer = await _dial(params["hostname"], params["port"])
    _tune(writer)
    try:
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            if use_tls:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                bare = strip_brackets(params["hostname"])
                server_hostname = None if is_ip(bare) else bare
                await writer.start_tls(context, server_hostname=server_hostname)

            writer.write(_connect_request(target_host, target_port, params))
            await writer.drain()

            # readuntil بقیه‌ی بایت‌ها را در بافر نگه می‌دارد، پس داده‌ی
            # چسبیده به انتهای هدر گم نمی‌شود (همان مشکلی که مرجع با
            # TransformStream حل کرده است).
            header = await reader.readuntil(b"\r\n\r\n")
            if len(header) > CONNECT_HEADER_MAX:
                raise OSError("proxy CONNECT header too long")
            status_line = header.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            match = re.match(r"HTTP/\d\.\d\s+(\d+)", status_line)
            code = int(match.group(1)) if match else -1
            if not (200 <= code < 300):
                raise OSError("proxy CONNECT failed: HTTP " + str(code))

            if first_packet:
                writer.write(bytes(first_packet))
                await writer.drain()
        return reader, writer
    except BaseException:
        try:
            writer.close()
        except Exception:
            pass
        raise


# ── ورودی عمومی ─────────────────────────────────────────────────────────────
async def open_outbound(
    address: str,
    port: int,
    first_packet: bytes | bytearray | memoryview | None = None,
    *,
    link: dict | None = None,
    uuid: str = "",
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bool]:
    """اتصال خروجی بر اساس تنظیمات آی‌پی خروجی.

    بازگشتی: (reader, writer, first_packet_written)
    اگر first_packet_written برابر True باشد، فراخوان دیگر نباید بسته‌ی اول را
    دوباره بنویسد (در مسیر ProxyIP/پروکسی، بسته‌ی اول بخشی از دست‌دهی است).
    """
    effective = _effective(link)
    mode = str(effective.get("mode") or "direct").lower()

    if mode == "direct":
        reader, writer = await _dial(address, port)
        _tune(writer)
        return reader, writer, False

    if mode in ("socks5", "http", "https"):
        # همان شرط مرجع: پروکسی فقط وقتی سراسری است یا میزبان در لیست اجبار.
        if not (effective.get("global_proxy") or host_forces_proxy(address)):
            reader, writer = await _dial(address, port)
            _tune(writer)
            return reader, writer, False
        try:
            params = parse_proxy_url(effective.get("proxy_url"), mode)
        except Exception as exc:
            logger.warning("invalid proxy url, falling back to direct: %s", exc)
            reader, writer = await _dial(address, port)
            _tune(writer)
            return reader, writer, False
        try:
            if params["scheme"] == "socks5":
                reader, writer = await socks5_connect(address, port, first_packet, params)
            elif params["scheme"] == "https":
                reader, writer = await http_connect(
                    address, port, first_packet, params, use_tls=True
                )
            else:
                reader, writer = await http_connect(
                    address, port, first_packet, params, use_tls=False
                )
            return reader, writer, bool(first_packet)
        except Exception as exc:
            if not effective.get("fallback"):
                raise
            logger.warning("chained proxy failed, falling back to direct: %s", exc)
            reader, writer = await _dial(address, port)
            _tune(writer)
            return reader, writer, False

    # mode == "proxyip" — روش اصلی مرجع
    pool = await resolve_pool(effective.get("proxyip"), address, uuid)
    if not pool:
        reader, writer = await _dial(address, port)
        _tune(writer)
        return reader, writer, False
    try:
        reader, writer, _chosen = await connect_proxyip(
            pool, first_packet, int(effective.get("concurrency", 1))
        )
        return reader, writer, bool(first_packet)
    except Exception as exc:
        if not effective.get("fallback"):
            # همان رفتار مرجع وقتی 反代兜底 خاموش است: اتصال قطع می‌شود.
            raise OSError(
                "all ProxyIP candidates failed and fallback is disabled"
            ) from exc
        logger.warning("ProxyIP failed, falling back to direct: %s", exc)
        reader, writer = await _dial(address, port)
        _tune(writer)
        return reader, writer, False


async def probe(
    target_host: str = "www.cloudflare.com",
    uuid: str = "",
    link: dict | None = None,
) -> dict:
    """تست عملی تنظیمات: resolve لیست + اتصال TCP واقعی به هر کاندید."""
    effective = _effective(link)
    mode = str(effective.get("mode") or "direct").lower()
    result: dict = {
        "mode": mode,
        "target": target_host,
        "candidates": [],
        "pool_size": 0,
        "proxy": None,
    }

    async def _timed(coro_factory) -> dict:
        entry: dict = {}
        started = time.monotonic()
        writer = None
        try:
            async with asyncio.timeout(6.0):
                reader, writer = await coro_factory()
            entry["ok"] = True
            entry["ms"] = round((time.monotonic() - started) * 1000.0, 1)
        except Exception as exc:
            entry["ok"] = False
            entry["error"] = str(exc) or exc.__class__.__name__
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
        return entry

    if mode == "proxyip":
        pool = await resolve_pool(effective.get("proxyip"), target_host, uuid)
        result["pool_size"] = len(pool)
        for host, port in pool:
            entry = await _timed(lambda h=host, p=port: _dial(h, p))
            entry["endpoint"] = host + ":" + str(port)
            result["candidates"].append(entry)
        return result

    if mode in ("socks5", "http", "https"):
        try:
            params = parse_proxy_url(effective.get("proxy_url"), mode)
        except Exception as exc:
            result["error"] = str(exc)
            return result
        result["proxy"] = params["hostname"] + ":" + str(params["port"])
        result["pool_size"] = 1

        async def _open():
            if params["scheme"] == "socks5":
                return await socks5_connect(target_host, 443, None, params)
            if params["scheme"] == "https":
                return await http_connect(target_host, 443, None, params, use_tls=True)
            return await http_connect(target_host, 443, None, params, use_tls=False)

        entry = await _timed(_open)
        entry["endpoint"] = result["proxy"]
        result["candidates"].append(entry)
        return result

    entry = await _timed(lambda: _dial(target_host, 443))
    entry["endpoint"] = target_host + ":443"
    result["pool_size"] = 1
    result["candidates"].append(entry)
    return result
