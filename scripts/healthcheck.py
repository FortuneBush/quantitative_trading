"""Poll the local readiness endpoint for Windows automation.

This command intentionally uses only the Python standard library.  It is safe
to run from Task Scheduler, ``start.cmd`` or a monitoring agent: a failed
probe never contacts the market-data provider and exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    status_code: Optional[int]
    payload: dict[str, Any]
    error: Optional[str] = None


def _validate_duration(value: float, *, name: str, allow_zero: bool) -> float:
    """Return a finite duration, rejecting values that could make polling unbounded.

    ``argparse`` accepts strings such as ``nan`` and ``inf`` for ``type=float``.
    Letting those values reach the deadline arithmetic can result in an endless
    readiness loop, so validation is intentionally performed both by
    :func:`wait_until_ready` and by the single-probe entry point.
    """

    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be a finite number")
    if numeric < 0 or (not allow_zero and numeric <= 0):
        if allow_zero:
            raise ValueError(f"{name} must be non-negative")
        raise ValueError(f"{name} must be greater than zero")
    return numeric


def _strict_bool(payload: dict[str, Any], key: str) -> Optional[bool]:
    """Read a readiness flag without treating strings/integers as booleans."""

    value = payload.get(key)
    return value if type(value) is bool else None


def _normalize_required_query_flags(
    url: str,
    *,
    require_research: bool,
    require_data: bool,
    require_calendar: bool,
) -> str:
    """Make CLI-required readiness gates authoritative in the probe URL.

    The endpoint supports query gates itself.  A caller may pass a weaker
    ``require_* = false`` URL, so each requested gate is replaced with exactly
    one ``true`` value while unrelated parameters and their order are retained.
    """

    required = {
        key: "true"
        for key, enabled in (
            ("require_research", require_research),
            ("require_data", require_data),
            ("require_calendar", require_calendar),
        )
        if enabled
    }
    if not required:
        return url
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    seen: set[str] = set()
    normalized_query: list[tuple[str, str]] = []
    for key, value in query:
        if key in required:
            if key not in seen:
                normalized_query.append((key, required[key]))
                seen.add(key)
            continue
        normalized_query.append((key, value))
    for key, value in required.items():
        if key not in seen:
            normalized_query.append((key, value))
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(normalized_query),
            parsed.fragment,
        )
    )


def probe_once(
    url: str,
    timeout_seconds: float,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    require_research: bool = False,
    require_data: bool = False,
    require_calendar: bool = False,
    expected_provider: Optional[str] = None,
) -> ProbeResult:
    """Perform one readiness probe and return a sanitized result."""

    timeout_seconds = _validate_duration(
        timeout_seconds, name="timeout_seconds", allow_zero=False
    )

    try:
        probe_url = _normalize_required_query_flags(
            url,
            require_research=require_research,
            require_data=require_data,
            require_calendar=require_calendar,
        )
        request = urllib.request.Request(
            probe_url,
            headers={"Accept": "application/json", "User-Agent": "quant-monitor-healthcheck/1"},
            method="GET",
        )
        with opener(request, timeout=timeout_seconds) as response:
            response_status = getattr(response, "status", None)
            status_code = int(
                response_status if response_status is not None else response.getcode()
            )
            raw = response.read()
        decoded = json.loads(raw.decode("utf-8"))
        payload = decoded if isinstance(decoded, dict) else {}
        provider_name = None
        if isinstance(payload.get("provider"), dict):
            provider_name = payload["provider"].get("name")
        if provider_name is None and isinstance(payload.get("feed"), dict):
            provider_name = payload["feed"].get("name") or payload["feed"].get(
                "provider"
            )
        serving_ready = _strict_bool(payload, "serving_ready")
        if serving_ready is None and "serving_ready" not in payload:
            # ``ready`` is the legacy field emitted by older local instances.
            serving_ready = _strict_bool(payload, "ready")
        research_ready = _strict_bool(payload, "research_ready")
        data_ready = _strict_bool(payload, "data_ready")
        calendar_ready = _strict_bool(payload, "calendar_ready")
        provider_ok = True
        if expected_provider is not None:
            provider_ok = str(provider_name or "").strip().casefold() == str(
                expected_provider
            ).strip().casefold()
        ok = status_code == 200 and serving_ready is True and provider_ok and (
            research_ready is True if require_research else True
        ) and (data_ready is True if require_data else True)
        ok = ok and (calendar_ready is True if require_calendar else True)
        error = None if ok else "service_not_ready"
        if serving_ready is None:
            error = "invalid_readiness_payload"
        elif require_research and research_ready is None:
            error = "invalid_readiness_payload"
        elif require_data and data_ready is None:
            error = "invalid_readiness_payload"
        elif require_research and serving_ready and research_ready is False:
            error = "research_not_ready"
        elif require_data and serving_ready and data_ready is False:
            error = "data_not_ready"
        elif require_calendar and calendar_ready is None:
            error = "invalid_readiness_payload"
        elif require_calendar and serving_ready and calendar_ready is False:
            error = "calendar_not_ready"
        if expected_provider is not None and serving_ready and not provider_ok:
            error = "unexpected_provider"
        return ProbeResult(ok, status_code, payload, error)
    except urllib.error.HTTPError as exc:
        # /ready deliberately returns 503 while bootstrapping.  Preserve only
        # the status and a stable error code; never echo arbitrary response
        # text. The readiness JSON is safe to inspect for typed gates.
        payload = {}
        try:
            decoded = json.loads(exc.read().decode("utf-8"))
            if isinstance(decoded, dict):
                payload = decoded
        except (AttributeError, KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
            pass
        if int(exc.code) == 503 and require_calendar:
            error = "calendar_not_ready"
        elif int(exc.code) == 503 and require_data:
            error = "data_not_ready"
        elif int(exc.code) == 503 and require_research:
            error = "research_not_ready"
        else:
            error = "http_error"
        return ProbeResult(False, int(exc.code), payload, error)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return ProbeResult(False, None, {}, type(exc).__name__)


def wait_until_ready(
    url: str,
    *,
    timeout_seconds: float = 3.0,
    wait_seconds: float = 30.0,
    interval_seconds: float = 1.0,
    require_research: bool = False,
    require_data: bool = False,
    require_calendar: bool = False,
    expected_provider: Optional[str] = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> ProbeResult:
    """Poll until ready or the bounded deadline expires."""

    timeout_seconds = _validate_duration(
        timeout_seconds, name="timeout_seconds", allow_zero=False
    )
    wait_seconds = _validate_duration(wait_seconds, name="wait_seconds", allow_zero=True)
    interval_seconds = _validate_duration(
        interval_seconds, name="interval_seconds", allow_zero=True
    )
    deadline = time.monotonic() + wait_seconds
    result = ProbeResult(False, None, {}, "timeout")
    while True:
        result = probe_once(
            url,
            timeout_seconds,
            opener=opener,
            require_research=require_research,
            require_data=require_data,
            require_calendar=require_calendar,
            expected_provider=expected_provider,
        )
        if result.ok or time.monotonic() >= deadline:
            return result
        delay = min(interval_seconds, max(0.0, deadline - time.monotonic()))
        if delay <= 0 and interval_seconds > 0:
            return result
        sleeper(delay)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="等待 A 股量化监控台就绪")
    parser.add_argument("--url", default="http://127.0.0.1:8000/ready")
    parser.add_argument("--timeout", type=float, default=3.0, help="单次 HTTP 超时（秒）")
    parser.add_argument("--wait", type=float, default=30.0, help="总等待时间（秒）")
    parser.add_argument("--interval", type=float, default=1.0, help="重试间隔（秒）")
    parser.add_argument(
        "--require-research",
        action="store_true",
        help="同时要求 research_ready=true（默认只检查 serving_ready）",
    )
    parser.add_argument(
        "--require-data",
        action="store_true",
        help="同时要求 data_ready=true（交易时段要求行情新鲜；休市允许上一收盘缓存）",
    )
    parser.add_argument(
        "--require-calendar",
        action="store_true",
        help="同时要求 calendar_ready=true（AShareHub 完整交易日历缓存）",
    )
    parser.add_argument(
        "--provider",
        choices=("asharehub", "alltick", "akshare", "mock"),
        help="同时校验服务实际使用的数据源，避免误探测旧实例",
    )
    args = parser.parse_args(argv)
    try:
        result = wait_until_ready(
            args.url,
            timeout_seconds=args.timeout,
            wait_seconds=args.wait,
            interval_seconds=args.interval,
            require_research=args.require_research,
            require_data=args.require_data,
            require_calendar=args.require_calendar,
            expected_provider=args.provider,
        )
    except ValueError as exc:
        parser.error(str(exc))
    provider_payload = result.payload.get("provider")
    feed_payload = result.payload.get("feed")
    provider_name = None
    if isinstance(provider_payload, dict):
        provider_name = provider_payload.get("name") or provider_payload.get("provider")
    if provider_name is None and isinstance(feed_payload, dict):
        provider_name = feed_payload.get("name") or feed_payload.get("provider")
    summary = {
        "ok": result.ok,
        "status_code": result.status_code,
        "error": result.error,
        "phase": result.payload.get("phase"),
        "serving_ready": result.payload.get("serving_ready", result.payload.get("ready")),
        "research_ready": result.payload.get("research_ready"),
        "data_ready": result.payload.get("data_ready"),
        "calendar_ready": result.payload.get("calendar_ready"),
        "provider": provider_name,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
