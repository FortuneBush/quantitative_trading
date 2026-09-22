"""Content-addressed identities for built-in research implementations.

``strategy_code_version`` used to be an arbitrary human label.  A label is
useful for display, but it cannot prove that a replay used the same code that
produced a stored result.  This module gives the built-in replayable strategy
an immutable, content-addressed identity assembled from a fixed allow-list of
source units.  The allow-list is deliberately explicit (rather than walking a
directory) so adding unrelated adapters, credentials or local files cannot
silently change a research artifact's identity.

The digest is an execution *source* identity, not a signed release or a proof
of provenance.  Deployments that need that stronger guarantee should sign the
resulting descriptor externally.  No network, provider or database access is
performed.
"""

from __future__ import annotations

import hashlib
import platform
import sys
from pathlib import Path
from typing import Any, Dict, Mapping

from .manifest import hash_canonical


CODE_IDENTITY_SCHEMA_VERSION = "strategy-code-identity-v1"

# Paths are package-relative and intentionally contain no machine-local path
# in the public descriptor.  Keep this list in dependency order so an audit
# can understand which code participates in a replay.
_STRATEGY_UNITS = {
    "moving_average_cross": (
        "quant_monitor/domain/models.py",
        "quant_monitor/strategies/base.py",
        "quant_monitor/strategies/moving_average.py",
        "quant_monitor/services/indicators.py",
        "quant_monitor/research/manifest.py",
        "quant_monitor/research/code_identity.py",
    ),
    "bank_daily_multifactor_v1": (
        "quant_monitor/domain/models.py",
        "quant_monitor/domain/lineage.py",
        "quant_monitor/strategies/base.py",
        "quant_monitor/strategies/bank_multifactor.py",
        "quant_monitor/backtesting/models.py",
        "quant_monitor/backtesting/engine.py",
        "quant_monitor/trading_calendar.py",
        "quant_monitor/serialization.py",
        "quant_monitor/research/manifest.py",
        "quant_monitor/research/lineage_gate.py",
        "quant_monitor/research/replay.py",
        "quant_monitor/research/code_identity.py",
    ),
}


def _workspace_root() -> Path:
    # code_identity.py lives at quant_monitor/research/code_identity.py.
    return Path(__file__).resolve().parents[2]


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    # Source units are bounded project files; reading one at a time avoids
    # the chunk-boundary ambiguity of a CRLF split across two reads.
    payload = path.read_bytes()
    # Python source semantics do not depend on the checkout's CRLF choice.
    # Canonicalising line endings keeps a manifest portable between Windows
    # and POSIX while still hashing the full source.
    digest.update(payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    return "sha256:" + digest.hexdigest()


def strategy_code_identity(strategy_name: str) -> Mapping[str, Any]:
    """Return the current local source identity for a replayable strategy.

    Missing or unreadable source units raise instead of returning a partial
    identity.  This is important: a partial digest would make a run appear
    reproducible after a damaged installation.
    """

    name = str(strategy_name).strip()
    try:
        units = _STRATEGY_UNITS[name]
    except KeyError as exc:
        raise ValueError("strategy has no content identity: {}".format(name)) from exc

    root = _workspace_root()
    digests: Dict[str, str] = {}
    for relative in units:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError("strategy code unit is unavailable: {}".format(relative))
        digests[relative] = _file_digest(path)
    descriptor: Dict[str, Any] = {
        "schema_version": CODE_IDENTITY_SCHEMA_VERSION,
        "strategy_name": name,
        "units": digests,
        # Record the interpreter family so a replay report does not silently
        # conflate bytecode/runtime changes with the same source revision.
        "python": "{}.{}".format(sys.version_info.major, sys.version_info.minor),
        "implementation": platform.python_implementation(),
    }
    descriptor["version"] = hash_canonical(
        descriptor, namespace="research-strategy-code-identity-v1"
    )
    return descriptor


def strategy_code_version(strategy_name: str) -> str:
    """Return the stable ``sha256:...`` version suitable for a manifest."""

    value = strategy_code_identity(strategy_name).get("version")
    if not isinstance(value, str):  # pragma: no cover - defensive contract
        raise RuntimeError("strategy code identity did not produce a version")
    return value


__all__ = [
    "CODE_IDENTITY_SCHEMA_VERSION",
    "strategy_code_identity",
    "strategy_code_version",
]
