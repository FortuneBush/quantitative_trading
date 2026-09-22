"""Logging configuration tests for the CLI/Uvicorn boundary."""

from __future__ import annotations

import copy
import logging
import re

import uvicorn
from uvicorn.logging import AccessFormatter, DefaultFormatter

from quant_monitor.main import SafeAccessFormatter, _build_uvicorn_log_config


def test_uvicorn_log_config_adds_timestamps_without_mutating_global_defaults():
    original = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    configured = _build_uvicorn_log_config()

    assert configured is not uvicorn.config.LOGGING_CONFIG
    assert uvicorn.config.LOGGING_CONFIG == original
    assert "%(asctime)s" in configured["formatters"]["default"]["fmt"]
    assert "%(asctime)s" in configured["formatters"]["access"]["fmt"]


def test_uvicorn_error_and_access_formatters_render_timestamps():
    configured = _build_uvicorn_log_config()["formatters"]
    prefix = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}")

    default = DefaultFormatter(
        fmt=configured["default"]["fmt"], use_colors=False
    )
    error_record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1, "Started server", (), None
    )
    assert prefix.search(default.format(error_record))

    access = AccessFormatter(
        fmt=configured["access"]["fmt"], use_colors=False
    )
    access_record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/health", "1.1", 200),
        None,
    )
    rendered = access.format(access_record)
    assert prefix.search(rendered)
    assert "GET /health HTTP/1.1" in rendered
    assert "200 OK" in rendered


def test_access_formatter_strips_query_strings_before_logging():
    configured = _build_uvicorn_log_config()["formatters"]["access"]
    access = SafeAccessFormatter(fmt=configured["fmt"], use_colors=False)
    secret = "token=must-not-be-logged"
    access_record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/api/v1/quotes?" + secret, "1.1", 200),
        None,
    )
    rendered = access.format(access_record)
    assert "GET /api/v1/quotes HTTP/1.1" in rendered
    assert secret not in rendered
