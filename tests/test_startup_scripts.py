import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_launcher_uses_per_user_dpapi_without_embedding_a_token():
    cmd_text = (ROOT / "scripts" / "start.cmd").read_text(encoding="utf-8")
    powershell_text = (ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")
    combined = cmd_text + powershell_text

    assert "Read-Host" in powershell_text
    assert "-AsSecureString" in powershell_text
    assert "ConvertFrom-SecureString" in powershell_text
    assert "ConvertTo-SecureString" in powershell_text
    assert "LOCALAPPDATA" in powershell_text
    assert "asharehub-api-key.dpapi" in powershell_text
    assert "--reset-token" in powershell_text
    assert 'SelectedProvider = "asharehub"' in powershell_text
    assert '"--provider"' in powershell_text
    assert "$env:QUANT_MONITOR_DATA_PROVIDER = $SelectedProvider" in powershell_text
    assert '"data\\quant_monitor_{0}.db" -f $SelectedProvider' in powershell_text
    assert "QUANT_MONITOR_DATABASE_PATH" in powershell_text
    assert "$ProcessDatabasePathWasInjected = $false" in powershell_text
    assert "$CustomConfigSupplied = $false" in powershell_text
    assert "-not $CustomConfigSupplied" in powershell_text
    assert "Remove-Item Env:\\QUANT_MONITOR_DATABASE_PATH" in powershell_text
    assert "QUANT_MONITOR_ALLTICK_TOKEN" in powershell_text
    assert "QUANT_MONITOR_ASHAREHUB_API_KEY" in powershell_text
    assert "QUANT_MONITOR_ASHAREHUB_KEY" in powershell_text
    assert "--reset-key" in powershell_text
    assert '"--smoke"' in powershell_text
    assert '"scripts\\smoke_asharehub.py"' in powershell_text
    assert "$PlainValue = $null" in powershell_text
    assert not re.search(r"(?i)[0-9a-f]{32}-c-app", combined)
    assert not re.search(r"(?i)ash_[0-9a-f]{40,}", combined)


def test_cmd_is_a_thin_single_entrypoint_and_preserves_arguments():
    cmd_text = (ROOT / "scripts" / "start.cmd").read_text(encoding="utf-8")

    assert "DisableDelayedExpansion" in cmd_text
    assert "-ExecutionPolicy Bypass" in cmd_text
    assert '"%~dp0start.ps1" %*' in cmd_text
    assert "QUANT_MONITOR_ALLTICK_TOKEN" not in cmd_text
    assert "QUANT_MONITOR_ASHAREHUB_API_KEY" not in cmd_text


def test_asharehub_budget_state_is_gitignored_and_smoke_shares_it():
    ignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    smoke_text = (ROOT / "scripts" / "smoke_asharehub.py").read_text(
        encoding="utf-8"
    )

    assert "data/asharehub_budget.json" in ignore_text
    assert "data/asharehub_budget.json.lock" in ignore_text
    assert '"data" / "asharehub_budget.json"' in smoke_text
    assert "AShareHubMarketDataProvider" in smoke_text
    assert "retries=0" in smoke_text
    assert "len(symbols) != 1" in smoke_text


def test_repository_does_not_embed_live_provider_credentials():
    excluded_parts = {".venv", ".venv-posix", ".git", "data", "__pycache__"}
    live_patterns = (
        re.compile(r"(?i)ash_[0-9a-f]{40,}"),
        re.compile(r"(?i)[0-9a-f]{32}-c-app"),
    )

    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in excluded_parts for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        assert not any(pattern.search(text) for pattern in live_patterns), str(path)


def test_windows_bash_delegates_to_the_same_secure_launcher():
    bash_text = (ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")

    assert "run_windows_launcher" in bash_text
    assert "powershell.exe" in bash_text
    assert 'start.ps1' in bash_text
    assert "-ExecutionPolicy Bypass" in bash_text
    assert "import fastapi, uvicorn'" in bash_text
    assert "DEPENDENCY_PROBE='import fastapi, uvicorn'" in bash_text
    assert "powershell.exe -NoLogo -NoProfile -Command 'exit 0'" in bash_text
    assert "status\" -ne 126" in bash_text
    assert "quant_monitor_${QUANT_MONITOR_DATA_PROVIDER}.db" in bash_text
    assert "QUANT_MONITOR_DATABASE_PATH" in bash_text
    assert "CUSTOM_CONFIG_SUPPLIED=0" in bash_text
    assert '[[ "$CUSTOM_CONFIG_SUPPLIED" -eq 0' in bash_text
    assert 'SELECTED_PROVIDER="${QUANT_MONITOR_DATA_PROVIDER:-asharehub}"' in bash_text
    assert 'FORWARD_ARGS+=("$1")' in bash_text
    assert 'run_windows_launcher --provider "$SELECTED_PROVIDER"' in bash_text
    assert 'exec "$PYTHON" -m quant_monitor "${FORWARD_ARGS[@]}"' in bash_text
    assert "import fastapi, uvicorn, akshare, websockets" in bash_text


def test_env_example_matches_safe_runtime_polling_default():
    env_text = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "QUANT_MONITOR_POLL_INTERVAL=60" in env_text
