"""Logging configuration tests for the CLI/Uvicorn boundary."""

from __future__ import annotations

import copy
import logging
import re

import uvicorn
from uvicorn.logging import AccessFormatter, DefaultFormatter

from quant_monitor.main import _build_uvicorn_log_config


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
