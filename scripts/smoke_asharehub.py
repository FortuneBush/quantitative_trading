"""Explicit one-request live smoke test for the AShareHub realtime endpoint.

This script is intentionally excluded from pytest and application startup. By
it makes exactly one upstream request for one locally validated symbol.
The API key is read only from the process environment and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import re
import sys
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant_monitor.data_sources import AShareHubMarketDataProvider
from quant_monitor.domain import normalize_symbol


_SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")


async def _run_once(api_key: str, symbols: Sequence[str], timeout: float) -> int:
    provider = AShareHubMarketDataProvider(
        api_key,
        symbols=symbols,
        request_timeout_seconds=max(1.0, float(timeout)),
        quote_cache_seconds=300.0,
        daily_soft_limit=90,
        budget_path=PROJECT_ROOT / "data" / "asharehub_budget.json",
        retries=0,
    )
    try:
        rows = await provider.fetch_quotes_relaxed(symbols)
        if not rows:
            print(
                "LIVE_FAIL: AShareHub returned no usable realtime rows",
                file=sys.stderr,
            )
            return 2
        returned = sorted({item.symbol for item in rows})
        used = provider.realtime_status["requests_used_today"]
        print(
            "LIVE_OK provider=asharehub requested={} returned={} symbols={} local_requests_used_today={}".format(
                len(symbols), len(rows), ",".join(returned), used
            )
        )
        return 0
    except Exception as exc:
        # Provider exceptions are deliberately sanitized and never contain the key.
        print("LIVE_FAIL {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return 2
    finally:
        await provider.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AShareHub live smoke test (exactly one realtime request)"
    )
    parser.add_argument(
        "--symbols",
        default="600519.SH",
        help="one CODE.SH/SZ/BJ symbol; smoke mode performs exactly one request",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    api_key = (
        os.environ.get("QUANT_MONITOR_ASHAREHUB_API_KEY")
        or os.environ.get("QUANT_MONITOR_ASHAREHUB_KEY")
        or ""
    ).strip()
    if not api_key:
        print(
            "LIVE_FAIL: set QUANT_MONITOR_ASHAREHUB_API_KEY in the process environment",
            file=sys.stderr,
        )
        return 2

    symbols = tuple(
        dict.fromkeys(
            item.strip().upper()
            for item in str(args.symbols).split(",")
            if item.strip()
        )
    )
    if not symbols:
        print("LIVE_FAIL: --symbols requires one code", file=sys.stderr)
        return 2
    if any(not _SYMBOL_RE.fullmatch(symbol) for symbol in symbols):
        print(
            "LIVE_FAIL: every symbol must use six-digit CODE.SH/SZ/BJ form",
            file=sys.stderr,
        )
        return 2
    if len(symbols) != 1:
        print(
            "LIVE_FAIL: smoke mode accepts exactly one symbol to minimize quota risk",
            file=sys.stderr,
        )
        return 2

    try:
        normalized_symbols = tuple(normalize_symbol(item) for item in symbols)
    except ValueError as exc:
        print("LIVE_FAIL: {}".format(exc), file=sys.stderr)
        return 2
    return asyncio.run(_run_once(api_key, normalized_symbols, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
