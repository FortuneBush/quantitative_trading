"""Manual live smoke test for the AKShare adapter.

This script is intentionally not part of the default pytest suite because the
AKShare/Eastmoney public endpoint can be rate-limited or unavailable.  Run it
when network access to the upstream service is available.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant_monitor.data_sources import AkShareMarketDataProvider


async def _run(symbol: str, timeout: float, fallback: bool) -> int:
    provider = AkShareMarketDataProvider(
        retries=0,
        quote_cache_seconds=0,
        use_spot_fallback=fallback,
    )
    try:
        quotes = await asyncio.wait_for(provider.fetch_quotes([symbol]), timeout=timeout)
        if not quotes:
            print("LIVE_FAIL: AKShare returned no usable quote", file=sys.stderr)
            return 2
        quote = quotes[0]
        print(
            "LIVE_OK symbol={} last={} volume={} turnover={} quote_time={} checked_at={}".format(
                quote.symbol,
                quote.last,
                quote.volume,
                quote.turnover,
                quote.timestamp.isoformat(),
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
    parser = argparse.ArgumentParser(description="AKShare live quote smoke test")
    parser.add_argument("--symbol", default="600519.SH", help="six-digit CODE.SH symbol")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument(
        "--fallback-sina",
        action="store_true",
        help="also try AKShare's full Sina snapshot when Eastmoney fails (slow)",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.symbol, args.timeout, args.fallback_sina))


if __name__ == "__main__":
    raise SystemExit(main())
