import json
import urllib.error

import pytest

from scripts.healthcheck import ProbeResult, probe_once, wait_until_ready


class _Response:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_probe_accepts_serving_ready_without_research_ready():
    result = probe_once(
        "http://127.0.0.1:8000/ready",
        1,
        opener=lambda *_args, **_kwargs: _Response(
            {"ready": True, "serving_ready": True, "research_ready": False, "phase": "ready"}
        ),
    )
    assert result == ProbeResult(True, 200, {"ready": True, "serving_ready": True, "research_ready": False, "phase": "ready"}, None)


def test_probe_can_require_research_readiness():
    result = probe_once(
        "http://127.0.0.1:8000/ready",
        1,
        require_research=True,
        opener=lambda *_args, **_kwargs: _Response(
            {"serving_ready": True, "research_ready": False}
        ),
    )
    assert not result.ok
    assert result.error == "research_not_ready"


def test_probe_can_require_data_readiness_and_marks_missing_data():
    seen_urls = []

    def opener(request, **_kwargs):
        seen_urls.append(request.full_url)
        return _Response({"serving_ready": True, "research_ready": True, "data_ready": False})

    result = probe_once(
        "http://localhost/ready?tenant=local",
        1,
        require_data=True,
        opener=opener,
    )
    assert not result.ok
    assert result.error == "data_not_ready"
    assert seen_urls == ["http://localhost/ready?tenant=local&require_data=true"]


def test_probe_can_require_calendar_readiness_and_marks_missing_calendar():
    seen_urls = []

    def opener(request, **_kwargs):
        seen_urls.append(request.full_url)
        return _Response({"serving_ready": True, "calendar_ready": False})

    result = probe_once(
        "http://localhost/ready?tenant=local",
        1,
        require_calendar=True,
        opener=opener,
    )
    assert not result.ok
    assert result.error == "calendar_not_ready"
    assert seen_urls == ["http://localhost/ready?tenant=local&require_calendar=true"]


def test_probe_can_require_expected_provider_from_ready_feed():
    result = probe_once(
        "http://127.0.0.1:8000/ready",
        1,
        expected_provider="asharehub",
        opener=lambda *_args, **_kwargs: _Response(
            {"serving_ready": True, "feed": {"provider": "asharehub"}}
        ),
    )
    # /ready exposes the provider as feed.provider (rather than feed.name).
    assert result.ok


def test_probe_rejects_unexpected_provider_without_provider_io():
    result = probe_once(
        "http://127.0.0.1:8000/ready",
        1,
        expected_provider="mock",
        opener=lambda *_args, **_kwargs: _Response(
            {"serving_ready": True, "feed": {"provider": "asharehub"}}
        ),
    )
    assert not result.ok
    assert result.error == "unexpected_provider"


def test_probe_require_data_overrides_a_weaker_query_without_duplicate_flag():
    seen_urls = []

    def opener(request, **_kwargs):
        seen_urls.append(request.full_url)
        return _Response({"serving_ready": True, "data_ready": True})

    result = probe_once(
        "http://localhost/ready?require_data=false",
        1,
        require_data=True,
        opener=opener,
    )
    assert result.ok
    assert seen_urls == ["http://localhost/ready?require_data=true"]


def test_probe_normalizes_both_required_gate_flags_and_deduplicates_them():
    seen_urls = []

    def opener(request, **_kwargs):
        seen_urls.append(request.full_url)
        return _Response(
            {"serving_ready": True, "research_ready": True, "data_ready": True}
        )

    result = probe_once(
        "http://localhost/ready?tenant=local&require_research=false&require_research=false&require_data=false",
        1,
        require_research=True,
        require_data=True,
        opener=opener,
    )
    assert result.ok
    assert seen_urls == [
        "http://localhost/ready?tenant=local&require_research=true&require_data=true"
    ]


def test_probe_normalizes_all_required_gate_flags():
    seen_urls = []

    def opener(request, **_kwargs):
        seen_urls.append(request.full_url)
        return _Response(
            {
                "serving_ready": True,
                "research_ready": True,
                "data_ready": True,
                "calendar_ready": True,
            }
        )

    result = probe_once(
        "http://localhost/ready?require_calendar=false&require_data=false",
        1,
        require_research=True,
        require_data=True,
        require_calendar=True,
        opener=opener,
    )
    assert result.ok
    assert seen_urls == [
        "http://localhost/ready?require_calendar=true&require_data=true&require_research=true"
    ]


@pytest.mark.parametrize(
    "payload, require_research, require_data, require_calendar",
    [
        ({"serving_ready": "false"}, False, False, False),
        ({"serving_ready": True, "research_ready": "true"}, True, False, False),
        ({"serving_ready": True, "data_ready": 1}, False, True, False),
        ({"serving_ready": True, "calendar_ready": 1}, False, False, True),
    ],
)
def test_probe_never_coerces_non_boolean_readiness_values(
    payload, require_research, require_data, require_calendar
):
    result = probe_once(
        "http://localhost/ready",
        1,
        require_research=require_research,
        require_data=require_data,
        require_calendar=require_calendar,
        opener=lambda *_args, **_kwargs: _Response(payload),
    )
    assert not result.ok
    assert result.error == "invalid_readiness_payload"


def test_probe_requires_research_flag_when_gate_is_requested_but_missing():
    result = probe_once(
        "http://localhost/ready",
        1,
        require_research=True,
        opener=lambda *_args, **_kwargs: _Response({"serving_ready": True}),
    )
    assert not result.ok
    assert result.error == "invalid_readiness_payload"


def test_probe_classifies_strict_readiness_503_without_echoing_body():
    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "http://localhost/ready?require_data=true",
            503,
            "not ready",
            hdrs=None,
            fp=None,
        )

    result = probe_once(
        "http://localhost/ready",
        1,
        require_data=True,
        opener=opener,
    )
    assert result == ProbeResult(False, 503, {}, "data_not_ready")


def test_wait_retries_until_ready_without_real_network():
    payloads = iter([
        {"serving_ready": False, "research_ready": False, "phase": "cache_hydration"},
        {"serving_ready": True, "research_ready": True, "phase": "ready"},
    ])
    calls = []

    def opener(*_args, **_kwargs):
        calls.append(1)
        return _Response(next(payloads))

    result = wait_until_ready(
        "http://localhost/ready",
        wait_seconds=2,
        interval_seconds=0,
        opener=opener,
        sleeper=lambda _seconds: None,
    )
    assert result.ok
    assert len(calls) == 2


def test_wait_rejects_invalid_timeout():
    with pytest.raises(ValueError):
        wait_until_ready("http://localhost/ready", timeout_seconds=0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"wait_seconds": float("nan")},
        {"wait_seconds": float("inf")},
        {"interval_seconds": float("nan")},
        {"interval_seconds": float("inf")},
    ],
)
def test_wait_rejects_non_finite_polling_parameters(kwargs):
    with pytest.raises(ValueError):
        wait_until_ready("http://localhost/ready", **kwargs)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_probe_rejects_non_positive_or_non_finite_timeout(value):
    with pytest.raises(ValueError):
        probe_once("http://localhost/ready", value)


def test_probe_preserves_typed_data_gate_on_http_503():
    def opener(*_args, **_kwargs):
        body = _Response({
            "ready": False,
            "serving_ready": True,
            "data_ready": False,
            "data_reason": "fresh_quotes_required",
        })
        raise urllib.error.HTTPError("http://localhost/ready", 503, "not ready", {}, body)

    result = probe_once("http://localhost/ready", 1, require_data=True, opener=opener)
    assert not result.ok
    assert result.status_code == 503
    assert result.error == "data_not_ready"
    assert result.payload["data_reason"] == "fresh_quotes_required"


def test_cli_summary_reads_provider_name_from_ready_feed(capsys, monkeypatch):
    """The CLI summary must not erase the provider exposed as feed.provider."""

    from scripts import healthcheck

    monkeypatch.setattr(
        healthcheck,
        "wait_until_ready",
        lambda *args, **kwargs: ProbeResult(
            True,
            200,
            {"ready": True, "phase": "ready", "feed": {"provider": "mock"}},
            None,
        ),
    )
    assert healthcheck.main(["--url", "http://localhost/ready", "--provider", "mock"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["provider"] == "mock"
