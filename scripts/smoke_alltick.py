"""Explicit live smoke test for the AllTick provider.

The token is read only from QUANT_MONITOR_ALLTICK_TOKEN (or the documented
API_KEY alias) and is never printed. This script is intentionally excluded
from pytest because it consumes a real provider quota.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant_monitor.data_sources import AllTickMarketDataProvider


async def _exercise(
    token: str,
    symbol: str,
    bars: int,
    listen_seconds: float,
) -> int:
    provider = AllTickMarketDataProvider(
        token,
        symbols=[symbol],
        use_websocket=listen_seconds > 0,
        retries=0,
    )
    try:
        quotes = await provider.fetch_quotes([symbol])
        if not quotes:
            print("LIVE_FAIL: AllTick returned no usable quote", file=sys.stderr)
            return 2
        initial = quotes[0]
        latest = initial
        if listen_seconds > 0:
            await asyncio.sleep(listen_seconds)
            refreshed = await provider.fetch_quotes([symbol])
            if refreshed:
                latest = refreshed[0]
        history = (
            await provider.fetch_bars(symbol, interval="1d", limit=bars)
            if bars > 0
            else []
        )
        print(
            "LIVE_OK provider=alltick symbol={} last={} quote_time={} "
            "quote_updated={} bars={} checked_at={}".format(
                latest.symbol,
                latest.last,
                latest.timestamp.isoformat(),
                latest.timestamp != initial.timestamp or latest.last != initial.last,
                len(history),
                datetime.now().astimezone().isoformat(),
            )
        )
        return 0
    except Exception as exc:
        print("LIVE_FAIL {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return 2
    finally:
        await provider.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="AllTick live market-data smoke test")
    parser.add_argument("--symbol", default="600519.SH")
    parser.add_argument("--bars", type=int, default=0, help="also fetch N daily bars")
    parser.add_argument(
        "--listen-seconds",
        type=float,
        default=0,
        help="listen for WebSocket trade updates after the HTTP bootstrap",
    )
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()
    token = (
        os.environ.get("QUANT_MONITOR_ALLTICK_TOKEN")
        or os.environ.get("QUANT_MONITOR_ALLTICK_API_KEY")
        or ""
    ).strip()
    if not token:
        print(
            "LIVE_FAIL: set QUANT_MONITOR_ALLTICK_TOKEN in the process environment",
            file=sys.stderr,
        )
        return 2
    try:
        return asyncio.run(
            asyncio.wait_for(
                _exercise(
                    token,
                    args.symbol,
                    max(0, args.bars),
                    max(0.0, args.listen_seconds),
                ),
                timeout=max(1.0, args.timeout),
            )
        )
    except asyncio.TimeoutError:
        print("LIVE_FAIL: AllTick smoke test timed out", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
