"""Out-of-band source and executor adapters for the policy-broker worker.

Both configuration and executable paths are required to live outside this
repository. A merge in the scanner repo therefore cannot replace the policy,
the authority snapshot, or the command that performs a consequential action.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .broker import ExecutionResult, IntentExecutors, TwoFactorWallError
from .queue import Intent
from .validator import LiveSources

_SHELL_NAMES = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})


def _outside_repo(path: str | Path, repo_root: Path, what: str) -> Path:
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise RuntimeError(f"{what} must not be a symlink: {raw}")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{what} cannot be resolved: {raw}: {exc}") from exc
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        if not resolved.is_file():
            raise RuntimeError(f"{what} must be a regular file: {resolved}")
        if resolved.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError(f"{what} must not be group/world writable: {resolved}")
        if resolved.parent.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError(
                f"{what} parent directory must not be group/world writable: {resolved.parent}"
            )
        return resolved
    raise RuntimeError(f"{what} must live outside this repository: {resolved}")


class JsonAuthoritySource:
    """Read an independent, atomically-written authority snapshot on every call."""

    def __init__(self, path: str | Path, repo_root: str | Path):
        self.path = _outside_repo(path, Path(repo_root), "authority snapshot")

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"authority snapshot unreadable: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("authority snapshot must be a JSON object")
        return data

    @staticmethod
    def _age_seconds(value: str) -> float:
        if not isinstance(value, str):
            raise RuntimeError("observation timestamps must be ISO-8601 strings")
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise RuntimeError("observation timestamps must include a timezone")
        return max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())

    def live_sources(self) -> LiveSources:
        def field(name: str):
            data = self._load()
            if name not in data:
                raise RuntimeError(f"authority snapshot missing {name!r}")
            return data[name]

        def ages(name: str) -> dict[str, float]:
            raw = field(name)
            if not isinstance(raw, dict) or not raw:
                raise RuntimeError(f"authority snapshot {name!r} must be a non-empty object")
            return {str(key): self._age_seconds(value) for key, value in raw.items()}

        def seconds_since_last_flip() -> float:
            value = field("last_flip_at")
            return self._age_seconds(value) if value else 10 ** 12

        def gate_config(name: str) -> dict:
            gates = field("gate_config")
            if not isinstance(gates, dict) or name not in gates:
                raise RuntimeError(f"authority snapshot missing gate config {name!r}")
            return gates[name]

        def market_book_depth_usd(market: str) -> float:
            books = field("market_book_depth_usd")
            if not isinstance(books, dict) or market not in books:
                raise RuntimeError(f"authority snapshot missing book depth for {market!r}")
            return books[market]

        return LiveSources(
            portfolio_value_usd=lambda: field("portfolio_value_usd"),
            realized_pnl_usd=lambda: field("realized_pnl_usd"),
            ledger_balances=lambda: field("ledger_balances"),
            venue_balances=lambda: field("venue_balances"),
            gate_config=gate_config,
            input_ages_seconds=lambda: ages("input_observed_at"),
            heartbeat_ages_seconds=lambda: ages("heartbeat_observed_at"),
            seconds_since_last_flip=seconds_since_last_flip,
            kill_switch_dry_run=lambda: field("kill_switch_dry_run"),
            market_book_depth_usd=market_book_depth_usd,
        )


class CommandIntentAdapter:
    """Invoke a fixed out-of-repo executable without a shell.

    Only intent metadata is written to stdin. The child must return strict JSON
    ``{"verified": true|false, "detail": "..."}``; ambiguous output fails
    closed as an unverifiable result.
    """

    def __init__(self, argv: list[str], timeout_seconds: float, repo_root: Path):
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            raise RuntimeError("executor argv must be a non-empty string list")
        executable = Path(argv[0]).expanduser()
        if not executable.is_absolute():
            raise RuntimeError("executor path must be absolute")
        executable_name = executable.name.lower()
        if executable_name in _SHELL_NAMES:
            raise RuntimeError("shell executables are not permitted as broker adapters")
        if executable_name == "env":
            raise RuntimeError("env dispatch is not permitted for broker adapters")
        executable = _outside_repo(executable, repo_root, "executor")
        try:
            first_line = executable.open("rb").readline(256).decode("utf-8", "ignore").strip()
        except OSError as exc:
            raise RuntimeError(f"executor cannot be inspected: {exc}") from exc
        if first_line.startswith("#!"):
            try:
                shebang_parts = shlex.split(first_line[2:])
            except ValueError as exc:
                raise RuntimeError(f"executor has an invalid shebang: {exc}") from exc
            interpreter = Path(shebang_parts[0]).name.lower() if shebang_parts else ""
            if interpreter in _SHELL_NAMES:
                raise RuntimeError("shell-script executors are not permitted as broker adapters")
            if interpreter == "env":
                possible_commands = {
                    Path(part).name.lower()
                    for part in shebang_parts[1:]
                    if not part.startswith("-") and "=" not in part
                }
                if possible_commands & _SHELL_NAMES:
                    raise RuntimeError("shell-script executors are not permitted as broker adapters")
        if any(arg in ("-c", "--command") for arg in argv[1:]):
            raise RuntimeError("command-string execution flags are not permitted")
        mode = executable.stat().st_mode
        if not stat.S_ISREG(mode) or not os.access(executable, os.X_OK):
            raise RuntimeError(f"executor is not an executable regular file: {executable}")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            raise RuntimeError("executor timeout_seconds must be finite and > 0") from None
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise RuntimeError("executor timeout_seconds must be finite and > 0")
        self.argv = [str(executable), *argv[1:]]
        self.timeout_seconds = timeout

    def __call__(self, intent: Intent) -> ExecutionResult:
        try:
            proc = subprocess.run(
                self.argv,
                input=json.dumps({
                    "intent_type": intent.intent_type,
                    "idempotency_key": intent.idempotency_key,
                    "payload": json.loads(intent.payload_json),
                }, sort_keys=True, allow_nan=False).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"executor unavailable: {type(exc).__name__}") from None
        if proc.returncode == 42:
            raise TwoFactorWallError("executor reported a 2FA/KYC wall")
        if proc.returncode != 0:
            return ExecutionResult(False, f"executor exited {proc.returncode}")
        try:
            result = json.loads(proc.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ExecutionResult(False, "executor returned malformed JSON")
        if not isinstance(result, dict) or not isinstance(result.get("verified"), bool):
            return ExecutionResult(False, "executor returned an invalid verification contract")
        detail = result.get("detail", "")
        if not isinstance(detail, str):
            return ExecutionResult(False, "executor detail must be a string")
        return ExecutionResult(result["verified"], detail[:1000])


def load_command_executors(
    config_path: str | Path,
    repo_root: str | Path,
) -> IntentExecutors:
    repo_root = Path(repo_root).resolve()
    path = _outside_repo(config_path, repo_root, "executor config")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"executor config unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("executor config must be a JSON object")

    adapters = {}
    for intent_type in ("flip_lane", "move_capital", "rotate_secret"):
        config = raw.get(intent_type)
        if not isinstance(config, dict):
            raise RuntimeError(f"executor config missing {intent_type!r}")
        adapters[intent_type] = CommandIntentAdapter(
            config.get("argv"), config.get("timeout_seconds", 30.0), repo_root,
        )
    return IntentExecutors(**adapters)
