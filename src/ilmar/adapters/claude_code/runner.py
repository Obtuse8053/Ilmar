# SPDX-License-Identifier: Apache-2.0
"""Claude Code as an Ilmar ``AgentRunner`` (ADR-0011 §6, ADR-0017).

Runs Claude Code's documented non-interactive mode inside an already started
sandbox and translates its documented JSON result. It uses only the public
CLI interface (MANIFEST §7.5); Claude Code itself is preinstalled in the
operator's agent image, never shipped by Ilmar.

**Credential (ADR-0006, ADR-0011 §4).** The model credential
(``ANTHROPIC_API_KEY``, or a gateway token with ``ANTHROPIC_BASE_URL``) is
the one secret that enters the *agent* sandbox, and only through the
environment of this one command, never the container's own environment, so
other commands and verification sandboxes never see it. It is redacted from
every summary.

**Invocation.** ``claude -p <prompt> --output-format json --permission-mode
<mode> --no-session-persistence --strict-mcp-config`` plus optional
``--max-turns`` and ``--model``. The sandbox is the security boundary, so the
default permission mode is ``bypassPermissions``, which Claude Code's
documentation reserves for isolated environments. ``--strict-mcp-config``
without an MCP config means no MCP servers, hence no extra network
destinations. ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`` turns off
auto-updates, telemetry and error reporting.

**Outcome (F-021).** ``COMPLETED`` needs all three: exit status 0, a parsed
``{"type": "result"}`` object, and ``is_error`` false with ``subtype``
``success``. Anything else is ``FAILED``; a runtime timeout is ``TIMED_OUT``.
The result is advisory: verification decides (ADR-0010).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.workspace import ContainerHandle
from ilmar.ports.container_runtime import ContainerRuntime

_PERMISSION_MODES = frozenset({"bypassPermissions", "acceptEdits", "dontAsk"})
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_SUMMARY_LIMIT = 4000
REDACTED = "[redacted]"


@dataclass(frozen=True, slots=True)
class ClaudeCodeSettings:
    executable: str = "claude"
    model: str | None = None
    max_turns: int | None = 200
    permission_mode: str = "bypassPermissions"
    expected_version: str | None = None
    """If set, ``claude --version`` must report exactly this version (ADR-0011 §6)."""
    base_url: str | None = None
    """An LLM gateway (``ANTHROPIC_BASE_URL``); its host must be on the sandbox allow-list."""
    version_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.executable or self.executable.startswith("-"):
            raise ValueError(f"invalid executable: {self.executable!r}")
        if self.permission_mode not in _PERMISSION_MODES:
            raise ValueError(f"permission_mode must be one of {sorted(_PERMISSION_MODES)}")
        if self.model is not None and not _MODEL.match(self.model):
            raise ValueError(f"invalid model name: {self.model!r}")
        if self.max_turns is not None and self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        if self.expected_version is not None and not _VERSION.match(self.expected_version):
            raise ValueError("expected_version must look like 1.2.3")
        if self.base_url is not None and not self.base_url.startswith("https://"):
            raise ValueError("base_url must be https")


class ClaudeCodeRunner:
    def __init__(
        self, *, api_key: str, settings: ClaudeCodeSettings | None = None, name: str = "claude-code"
    ) -> None:
        if not api_key or any(c in api_key for c in "\r\n\0"):
            raise ValueError("a single-line model credential is required")
        self._api_key = api_key
        self._settings = settings or ClaudeCodeSettings()
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def run(
        self, runtime: ContainerRuntime, container: ContainerHandle, request: AgentRequest
    ) -> AgentResult:
        settings = self._settings
        if settings.expected_version is not None:
            mismatch = self._version_mismatch(runtime, container)
            if mismatch is not None:
                return AgentResult(AgentOutcome.FAILED, mismatch, None)

        result = runtime.run(
            container,
            self.command(request),
            timeout_seconds=request.timeout_seconds,
            env=self._environment(),
        )
        if result.timed_out:
            return AgentResult(AgentOutcome.TIMED_OUT, "Claude Code timed out", result.exit_code)
        parsed = parse_result(result.stdout)
        if parsed is None:
            lines = result.stderr.strip().splitlines()
            detail = self._redact(lines[-1])[:500] if lines else ""
            summary = f"no structured result (exit {result.exit_code})"
            text = f"{summary}: {detail}" if detail else summary
            return AgentResult(AgentOutcome.FAILED, text, result.exit_code)
        text = self._redact(str(parsed.get("result") or ""))[:_SUMMARY_LIMIT]
        subtype = str(parsed.get("subtype") or "")
        success = parsed.get("is_error") is False and subtype == "success"
        if result.exit_code == 0 and success:
            return AgentResult(AgentOutcome.COMPLETED, text or "Claude Code finished", 0)
        reason = subtype or "error"
        if success:  # claims success, but the process failed (F-021)
            reason = f"exit status {result.exit_code} despite a success result"
        return AgentResult(
            AgentOutcome.FAILED, f"{reason}: {text}" if text else reason, result.exit_code
        )

    def command(self, request: AgentRequest) -> list[str]:
        settings = self._settings
        # The prompt starts with fixed text, so it can never be read as an option.
        prompt = (
            f"Ilmar task {request.task.ref.key}: {request.task.title}\n\n{request.instructions}"
        )
        argv = [
            settings.executable,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--permission-mode",
            settings.permission_mode,
            "--no-session-persistence",
            "--strict-mcp-config",
        ]
        if settings.max_turns is not None:
            argv += ["--max-turns", str(settings.max_turns)]
        if settings.model is not None:
            argv += ["--model", settings.model]
        return argv

    def _environment(self) -> dict[str, str]:
        env = {
            "ANTHROPIC_API_KEY": self._api_key,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
        }
        if self._settings.base_url is not None:
            env["ANTHROPIC_BASE_URL"] = self._settings.base_url
        return env

    def _version_mismatch(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> str | None:
        settings = self._settings
        probe = runtime.run(
            container,
            [settings.executable, "--version"],
            timeout_seconds=settings.version_timeout_seconds,
            env={"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"},
        )
        reported = probe.stdout.strip().split(" ", 1)[0] if probe.stdout.strip() else ""
        if probe.succeeded and reported == settings.expected_version:
            return None
        found = reported or f"none (exit {probe.exit_code})"
        return f"Claude Code version {found[:40]}, expected {settings.expected_version}"

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, REDACTED)


def parse_result(stdout: str) -> Mapping[str, Any] | None:
    """The documented ``--output-format json`` result object, or ``None``."""
    candidates = [stdout.strip()]
    candidates += [line for line in reversed(stdout.splitlines()) if line.strip().startswith("{")]
    for text in candidates:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            return data
    return None
