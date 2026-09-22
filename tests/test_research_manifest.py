from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal
from enum import Enum
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from quant_monitor.domain import Bar, SHANGHAI_TZ
from quant_monitor.research import (
    CANONICALIZATION_VERSION,
    CanonicalizationError,
    DatasetDescriptor,
    ManifestValidationError,
    ResearchRunManifest,
    ResearchResultRecord,
    SensitiveManifestValueError,
    canonical_json,
    hash_canonical,
    hash_input_records,
    verify_input_hash,
)
from quant_monitor.research.manifest import _security_scan_text


class FixtureState(str, Enum):
    READY = "ready"


def _records():
    return [
        Bar(
            symbol="600036.SH",
            interval="1d",
            timestamp=datetime(2025, 1, 6, 15, 30, tzinfo=SHANGHAI_TZ),
            open=Decimal("39.100"),
            high=Decimal("40.00"),
            low=Decimal("39.10"),
            close=Decimal("39.80"),
            volume=123400,
            turnover=Decimal("4900000.00"),
            source="fixture",
        ),
        Bar(
            symbol="601398.SH",
            interval="1d",
            timestamp=datetime(2025, 1, 6, 15, 30, tzinfo=SHANGHAI_TZ),
            open=Decimal("6.50"),
            high=Decimal("6.60"),
            low=Decimal("6.40"),
            close=Decimal("6.55"),
            volume=800000,
            turnover=Decimal("5200000"),
            source="fixture",
        ),
    ]


def _dataset(**changes):
    values = {
        "name": "bank_daily_bars",
        "schema_version": "bar-v1",
        "source": "fixture",
        "record_count": 2,
        "attributes": {
            "symbols": ["600036.SH", "601398.SH"],
            "interval": "1d",
            "as_of": datetime(2025, 1, 6, 15, 30, tzinfo=SHANGHAI_TZ),
        },
    }
    values.update(changes)
    return DatasetDescriptor(**values)


def _manifest(**changes):
    values = {
        "run_id": "run-local-001",
        "strategy_name": "bank_multifactor",
        "strategy_parameters": {
            "minimum_universe": 15,
            "weights": {"momentum": Decimal("0.30"), "trend": Decimal("0.70")},
        },
        "strategy_code_version": "bank-multifactor-v1+git.0123456",
        "dataset": _dataset(),
        "input_hash": hash_input_records(_records()),
        "calendar_version": "sse-szse-fixture-2025-v1",
        "universe_version": "bank-universe-fixture-v1",
        "adjustment_version": "raw-unadjusted-v1",
        "constraint_version": "constraints-fixture-v1",
        "configuration": {
            "initial_cash": Decimal("1000000.00"),
            "execution": FixtureState.READY,
        },
        "assumptions": {
            "purpose": "research_only",
            "bar_availability": "completed_daily_bar_at_15:30_asia_shanghai",
        },
    }
    values.update(changes)
    return ResearchRunManifest(**values)


def test_canonical_json_normalizes_supported_types_and_mapping_order():
    utc_value = datetime(2025, 1, 6, 7, 30, tzinfo=timezone.utc)
    shanghai_value = datetime(2025, 1, 6, 15, 30, tzinfo=SHANGHAI_TZ)
    left = {
        "z": {"beta": Decimal("1.2300"), "alpha": FixtureState.READY},
        "a": utc_value,
        "set_values": {"b", "a"},
    }
    right = {
        "set_values": {"a", "b"},
        "a": shanghai_value,
        "z": {"alpha": FixtureState.READY, "beta": Decimal("1.23")},
    }

    encoded = canonical_json(left)

    assert encoded == canonical_json(right)
    assert CANONICALIZATION_VERSION.endswith("-v2")
    assert '"$quant_monitor_type":"datetime"' in encoded
    assert '"$quant_monitor_type":"decimal"' in encoded
    assert '"$quant_monitor_type":"enum"' in encoded


def test_verify_input_hash_fails_closed_on_changed_records():
    records = _records()
    digest = hash_input_records(records)
    verify_input_hash(digest, records)
    changed = list(records)
    changed[0] = Bar(
        symbol=changed[0].symbol,
        interval=changed[0].interval,
        timestamp=changed[0].timestamp,
        open=changed[0].open,
        high=changed[0].high,
        low=changed[0].low,
        close=changed[0].close + Decimal("0.01"),
        volume=changed[0].volume,
        turnover=changed[0].turnover,
        source=changed[0].source,
    )
    with pytest.raises(ManifestValidationError, match="do not match"):
        verify_input_hash(digest, changed)


@dataclass(frozen=True)
class FixturePair:
    left: int
    right: str


def test_v2_type_tags_prevent_semantic_collisions():
    aware = datetime(2025, 1, 6, 15, 30, tzinfo=SHANGHAI_TZ)
    collisions = [
        (Decimal("1.23"), "1.23"),
        (date(2025, 1, 6), "2025-01-06"),
        (aware, aware.isoformat(timespec="microseconds")),
        (time(15, 30), "15:30:00.000000"),
        (FixtureState.READY, "ready"),
        ((1, 2), [1, 2]),
        ({1, 2}, frozenset({1, 2})),
        (FixturePair(1, "two"), {"left": 1, "right": "two"}),
        (True, 1),
        (1, 1.0),
    ]

    for left, right in collisions:
        assert canonical_json(left) != canonical_json(right)
        assert hash_canonical(left) != hash_canonical(right)


def test_reserved_tag_key_in_user_mapping_is_escaped():
    encoded = canonical_json({"$quant_monitor_type": "decimal", "value": "1e0"})

    assert json.loads(encoded)["$quant_monitor_type"] == "mapping"


def test_ordered_sequences_keep_their_research_meaning():
    assert hash_canonical(["first", "second"]) != hash_canonical(
        ["second", "first"]
    )


def test_input_hash_is_record_order_independent_but_duplicate_sensitive():
    records = _records()

    original = hash_input_records(records)

    assert original == hash_input_records(reversed(records))
    assert original != hash_input_records(records + [records[0]])
    changed = list(records)
    changed[0] = Bar(
        symbol=records[0].symbol,
        interval=records[0].interval,
        timestamp=records[0].timestamp,
        open=records[0].open,
        high=records[0].high,
        low=records[0].low,
        close=Decimal("39.81"),
        volume=records[0].volume,
        turnover=records[0].turnover,
        source=records[0].source,
    )
    assert original != hash_input_records(changed)


def test_manifest_reproduces_and_run_id_does_not_change_content_hash():
    first = _manifest(run_id="run-local-001")
    repeated = _manifest(run_id="run-local-001")
    another_identity = _manifest(run_id="run-local-999")

    assert first == repeated
    assert first.to_json() == repeated.to_json()
    assert first.content_hash == another_identity.content_hash
    assert first.to_json() != another_identity.to_json()
    assert first.content_hash.startswith("sha256:")

    payload = first.to_dict()
    assert payload["purpose"] == "research_only"
    assert payload["run_id"] == "run-local-001"
    assert payload["content_hash"] == first.content_hash
    assert json.loads(first.to_json()) == payload


def test_manifest_is_deeply_immutable_and_detaches_json_output():
    parameters = {"nested": {"windows": [20, 60]}}
    manifest = _manifest(strategy_parameters=parameters)
    parameters["nested"]["windows"].append(120)

    with pytest.raises(TypeError):
        manifest.strategy_parameters["new"] = True
    with pytest.raises(TypeError):
        manifest.strategy_parameters["nested"]["new"] = True
    assert tuple(manifest.strategy_parameters["nested"]["windows"]) == (20, 60)

    detached = manifest.to_dict()
    detached["strategy"]["parameters"]["nested"]["windows"].append(120)
    assert tuple(manifest.strategy_parameters["nested"]["windows"]) == (20, 60)


def test_manifest_freezes_datetime_instant_away_from_mutable_tzinfo():
    class MutableOffset(tzinfo):
        def __init__(self):
            self.hours = 0

        def utcoffset(self, dt):
            return timedelta(hours=self.hours)

        def dst(self, dt):
            return timedelta(0)

    offset = MutableOffset()
    supplied = datetime(2025, 1, 6, 7, 30, tzinfo=offset)
    manifest = _manifest(strategy_parameters={"evaluated_at": supplied})
    original_hash = manifest.content_hash
    original_content = manifest.content_dict()
    original_json = manifest.to_json()

    offset.hours = 1

    assert manifest.content_hash == original_hash
    assert manifest.content_dict() == original_content
    assert manifest.to_json() == original_json
    assert original_content["strategy"]["parameters"]["evaluated_at"]["value"] == (
        "2025-01-06T15:30:00.000000+08:00"
    )


def test_freezing_does_not_collapse_list_tuple_or_set_or_frozenset():
    left = _manifest(strategy_parameters={"value": [1, 2]})
    tuple_value = _manifest(strategy_parameters={"value": (1, 2)})
    set_value = _manifest(strategy_parameters={"value": {1, 2}})
    frozen_set_value = _manifest(strategy_parameters={"value": frozenset({1, 2})})

    hashes = {
        item.content_hash
        for item in (left, tuple_value, set_value, frozen_set_value)
    }
    assert len(hashes) == 4


@pytest.mark.parametrize(
    "change",
    [
        {"strategy_name": "other_strategy"},
        {"strategy_parameters": {"minimum_universe": 16}},
        {"strategy_code_version": "bank-multifactor-v2"},
        {"dataset": _dataset(source="second_fixture")},
        {"input_hash": hash_input_records(_records()[:1])},
        {"calendar_version": "calendar-v2"},
        {"universe_version": "universe-v2"},
        {"adjustment_version": "qfq-factor-v2"},
        {"constraint_version": "constraints-v2"},
        {"configuration": {"initial_cash": Decimal("2000000")}},
        {"assumptions": {"purpose": "research_only", "delay": "one_session"}},
    ],
)
def test_every_manifest_content_input_changes_the_content_hash(change):
    assert _manifest(**change).content_hash != _manifest().content_hash


@pytest.mark.parametrize(
    "field_name",
    [
        "api_key",
        "apiKey",
        "access_token",
        "authorization",
        "client_secret",
        "private_key",
        "set_cookie",
        "x_auth_key",
        "API密钥",
        "访问令牌",
        "密码",
    ],
)
def test_credential_bearing_field_names_are_rejected_at_any_depth(field_name):
    with pytest.raises(SensitiveManifestValueError) as error:
        _manifest(configuration={"safe": {field_name: "redacted"}})

    assert "redacted" not in str(error.value)


@pytest.mark.parametrize(
    "field_name",
    [
        "pass\u200bword",
        "to\u200bken",
        "api\uff3fkey",
        "\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44",
        "\u5bc6\u200b\u7801",
        "\u5bc6\u7801\u4ee4\u724c\u5316",
    ],
)
def test_unicode_format_characters_cannot_hide_credential_field_names(field_name):
    with pytest.raises(SensitiveManifestValueError):
        _manifest(configuration={field_name: "hidden-value"})


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "C:\\Users\\analyst\\research.json",
        "D:/private/research.json",
        "/home/analyst/research.json",
        "file:///tmp/research.json",
        "Bearer do-not-store-this",
        "https://example.invalid/data?token=do-not-store-this",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_machine_paths_and_recognizable_credentials_are_rejected(unsafe_value):
    with pytest.raises(SensitiveManifestValueError) as error:
        _manifest(assumptions={"note": unsafe_value})

    assert unsafe_value not in str(error.value)


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "\\server\\share\\research.json",
        "//server/share/research.json",
        "\\Windows\\System32\\config",
        "https://user:pass@example.invalid/data",
        "https://example.invalid/data?api%5Fkey=hidden",
        "https%3A%2F%2Fexample.invalid%2Fdata%3Ftoken%3Dhidden",
        "api_key=hidden-value",
        "access token: hidden-value",
        "header Bearer abcdefgh12345 trailing",
        "API 密钥：隐藏值",
        '{"nested":{"client_secret":"hidden-value"}}',
        "prefix C%3A%5CUsers%5Canalyst%5Cresearch.json suffix",
        "prefix:C:\\Users\\analyst\\research.json",
    ],
)
def test_encoded_and_embedded_secrets_and_all_absolute_paths_are_rejected(
    unsafe_value,
):
    with pytest.raises(SensitiveManifestValueError) as error:
        _manifest(assumptions={"note": unsafe_value})

    assert "hidden-value" not in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        "Basic research baseline",
        "Bearer bond model",
        "tokenization experiment",
    ],
)
def test_benign_security_words_in_prose_are_accepted(value):
    _manifest(assumptions={"note": value})


def test_benign_token_metrics_are_accepted():
    manifest = _manifest(
        configuration={
            "token_count": 120,
            "prompt_token_count": 80,
            "tokenization": "wordpiece",
        }
    )

    assert manifest.configuration["token_count"] == 120


@pytest.mark.parametrize(
    "live_looking_value",
    [
        "ash_" + ("a" * 40),
        "prefix ash_" + ("B" * 48) + " suffix",
        ("c" * 32) + "-c-app",
        "prefix " + ("D" * 32) + "-C-APP suffix",
    ],
    ids=(
        "asharehub-key-exact",
        "asharehub-key-embedded-uppercase",
        "alltick-key-exact",
        "alltick-key-embedded-uppercase",
    ),
)
def test_live_looking_provider_keys_are_rejected_in_generic_values(
    live_looking_value,
):
    with pytest.raises(SensitiveManifestValueError) as error:
        _manifest(assumptions={"note": live_looking_value})

    assert live_looking_value not in str(error.value)


def test_dataset_descriptor_applies_the_same_security_validation():
    with pytest.raises(SensitiveManifestValueError):
        _dataset(attributes={"headers": {"Authorization": "redacted"}})

    with pytest.raises(SensitiveManifestValueError):
        _dataset(source="C:\\private\\source")


@pytest.mark.parametrize(
    "value",
    [
        {"value": float("nan")},
        {"value": Decimal("Infinity")},
        {"value": datetime(2025, 1, 6, 15, 30)},
        {1: "non-string-key"},
        {"value": b"opaque"},
    ],
)
def test_nondeterministic_or_unsupported_values_are_rejected(value):
    with pytest.raises(CanonicalizationError):
        canonical_json(value)


def test_aware_time_is_rejected_even_when_utcoffset_returns_none():
    from datetime import tzinfo

    class BrokenTZ(tzinfo):
        def utcoffset(self, dt):
            return None

        def dst(self, dt):
            return None

    with pytest.raises(CanonicalizationError):
        canonical_json(time(15, 30, tzinfo=BrokenTZ()))


def test_nul_and_lone_surrogate_are_domain_errors():
    for value in ("before\x00after", "before\ud800after"):
        with pytest.raises(CanonicalizationError):
            canonical_json(value)


@pytest.mark.parametrize("count", [True, False, 1.0, "2", Decimal("2")])
def test_record_count_requires_exact_integer_type(count):
    with pytest.raises(ManifestValidationError):
        _dataset(record_count=count)


@pytest.mark.parametrize("field", ["name", "schema_version", "source"])
def test_dataset_identifiers_require_strings(field):
    with pytest.raises(ManifestValidationError):
        _dataset(**{field: 123})


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "strategy_name",
        "strategy_code_version",
        "calendar_version",
        "universe_version",
        "adjustment_version",
        "constraint_version",
        "manifest_schema_version",
    ],
)
def test_manifest_identifiers_require_strings(field):
    with pytest.raises(ManifestValidationError):
        _manifest(**{field: 123})


def test_hash_namespace_requires_a_string():
    with pytest.raises(ManifestValidationError):
        hash_canonical({"value": 1}, namespace=123)


def test_secret_like_mapping_key_is_rejected_even_when_value_is_absent():
    for value in ({"password": None}, {"API密钥": None}, {"client_secret": None}):
        with pytest.raises(SensitiveManifestValueError):
            canonical_json(value)


def test_exception_never_echoes_rejected_secret():
    secret = "ash_" + ("x" * 48)
    with pytest.raises(SensitiveManifestValueError) as error:
        canonical_json({"note": "prefix " + secret + " suffix"})

    assert secret not in str(error.value)


def test_result_artifact_rejects_credentials_before_immutable_export():
    """A valid-looking result hash must not make a secret exportable."""

    result = {
        "strategy_name": "fixture",
        "diagnostics": {"metadata": {"api_key": "do-not-export"}},
        "result_hash": "sha256:" + ("0" * 64),
    }
    result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with pytest.raises(SensitiveManifestValueError) as error:
        ResearchResultRecord.from_json(
            "run-sensitive-result",
            result_json,
            created_at=datetime(2025, 1, 6, 15, 30, tzinfo=SHANGHAI_TZ),
        )
    assert "do-not-export" not in str(error.value)


def test_result_artifact_rejects_embedded_json_credentials():
    result = {
        "strategy_name": "fixture",
        "diagnostics": {"note": '{"nested":{"access_token":"hidden"}}'},
        "result_hash": "sha256:" + ("0" * 64),
    }
    result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with pytest.raises(SensitiveManifestValueError) as error:
        ResearchResultRecord.from_json(
            "run-embedded-sensitive-result",
            result_json,
            created_at=datetime(2025, 1, 6, 15, 30, tzinfo=SHANGHAI_TZ),
        )
    assert "hidden" not in str(error.value)


def test_mapping_items_are_snapshotted_once():
    class ItemsOnlyMapping(dict):
        def __init__(self):
            super().__init__({"alpha": 1, "beta": 2})
            self.items_calls = 0

        def items(self):
            self.items_calls += 1
            if self.items_calls > 1:
                raise AssertionError("items() called more than once")
            return super().items()

        def keys(self):
            raise AssertionError("keys() must not be used")

        def __getitem__(self, key):
            raise AssertionError("mapping must not be re-read after snapshot")

    value = ItemsOnlyMapping()
    assert canonical_json(value) == '{"alpha":1,"beta":2}'
    assert value.items_calls == 1


def test_frozen_set_manifest_hash_is_stable_across_source_order():
    left = _manifest(
        strategy_parameters={"values": set(["gamma", "alpha", "beta"])}
    )
    right = _manifest(
        strategy_parameters={"values": set(["beta", "gamma", "alpha"])}
    )

    assert left.content_hash == right.content_hash


def test_hash_is_stable_across_python_hash_seeds():
    project_root = Path(__file__).resolve().parents[1]
    script = (
        "from quant_monitor.research import hash_canonical; "
        "print(hash_canonical({'values': set(['gamma','alpha','beta']), "
        "'tuple': (1, 2)}))"
    )
    outputs = []
    for seed in ("1", "987654"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        environment["PYTHONPATH"] = str(project_root)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(project_root),
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(completed.stdout.strip())
    assert outputs[0] == outputs[1]


def test_cyclic_values_are_rejected():
    value = []
    value.append(value)

    with pytest.raises(CanonicalizationError):
        canonical_json(value)


def test_manifest_requires_a_well_formed_input_hash():
    with pytest.raises(ManifestValidationError):
        _manifest(input_hash="not-a-sha256-hash")


def test_large_ascii_security_scan_does_not_make_an_extra_filtered_copy():
    # Result artifacts can contain thousands of equity/trade rows.  The
    # scanner must retain its checks without doubling peak memory for ordinary
    # ASCII JSON, which is the serialized artifact's normal representation.
    value = "{\"equity_curve\":[" + ("0," * 250_000) + "0]}"
    scanned = _security_scan_text(value)
    assert scanned == value
    assert scanned is value
