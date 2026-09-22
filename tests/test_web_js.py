"""Dependency-free browser module checks."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_bank_research_browser_module():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/js/bank_research.test.cjs"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dashboard_quote_freshness_contract():
    """The dashboard must expose row-level cache freshness without network I/O."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/js/dashboard_contract.test.cjs"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_research_report_browser_module():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/js/research_report.test.cjs"],
        cwd=root, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
