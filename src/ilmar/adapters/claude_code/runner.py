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

**Invocation.** ``claude -p <prompt> --output-format stream-json --verbose
--permission-mode <mode> --no-session-persistence --strict-mcp-config`` plus
optional ``--max-turns`` and ``--model`` (ADR-0035 §1). The sandbox is the security boundary, so the
default permission mode is ``bypassPermissions``, which Claude Code's
documentation reserves for isolated environments. ``--strict-mcp-config``
without an MCP config means no MCP servers, hence no extra network
destinations. ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`` turns off
auto-updates, telemetry and error reporting.

**Stream (ADR-0035).** Standard output goes, chunk by chunk, to a
:class:`~ilmar.adapters.claude_code.stream.StreamValidator`; none of it is
kept. Its first violation ends the run at once as ``FAILED`` (not retried).
No agent progress for the stall timeout ends it as ``STALLED`` (retried).

**Outcome (F-021).** ``COMPLETED`` needs all three: exit status 0, a stream
with exactly one ``{"type": "result"}`` object and no violation, and
``is_error`` false with ``subtype`` ``success``. Anything else is ``FAILED``;
a runtime timeout is ``TIMED_OUT``. The result is advisory: verification
decides (ADR-0010).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from ilmar.adapters.claude_code.credential_route import (
    own_environment_refusal,
    parse_probe,
    probe_command,
    refusal,
)
from ilmar.adapters.claude_code.provider_errors import ProviderFailure, classify
from ilmar.adapters.claude_code.stream import EventObserver, StreamValidator, Violation
from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.errors import PolicyViolationError, ProviderError
from ilmar.domain.workspace import ContainerHandle, StopSignal
from ilmar.ports.container_runtime import ContainerRuntime

_PERMISSION_MODES = frozenset({"bypassPermissions", "acceptEdits", "dontAsk"})
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_SUMMARY_LIMIT = 4000
_REDACT_MIN = 8
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
        self,
        *,
        api_key: str,
        settings: ClaudeCodeSettings | None = None,
        name: str = "claude-code",
        event_observer: EventObserver | None = None,
    ) -> None:
        """``event_observer`` sees only each event's type, subtype and whether it
        followed the result (the pinned-version live test records these)."""
        if not api_key or any(c in api_key for c in "\r\n\0"):
            raise ValueError("a single-line model credential is required")
        self._api_key = api_key
        self._settings = settings or ClaudeCodeSettings()
        self._name = name
        self._observer = event_observer

    @property
    def name(self) -> str:
        return self._name

    def run(
        self,
        runtime: ContainerRuntime,
        container: ContainerHandle,
        request: AgentRequest,
        *,
        stop: StopSignal | None = None,
    ) -> AgentResult:
        settings = self._settings
        env = self._environment()
        # ADR-0029 §5: one credential path, checked before Claude Code starts.
        self._require_one_credential_path(runtime, container, env)
        if settings.expected_version is not None:
            mismatch = self._version_mismatch(runtime, container)
            if mismatch is not None:
                return AgentResult(AgentOutcome.FAILED, mismatch, None)

        validator = StreamValidator(observer=self._observer)
        result = runtime.run(
            container,
            self.command(request),
            timeout_seconds=request.timeout_seconds,
            env=env,
            stop=stop,
            idle_timeout_seconds=request.stall_timeout_seconds,
            stdout_consumer=validator,
        )
        if result.cancelled:  # the runtime removed the sandbox, as on a timeout
            return AgentResult(AgentOutcome.CANCELLED, "Claude Code was stopped on request", None)
        if result.timed_out:
            return AgentResult(AgentOutcome.TIMED_OUT, "Claude Code timed out", result.exit_code)
        if result.stalled:
            return AgentResult(
                AgentOutcome.STALLED,
                f"Claude Code showed no progress for {request.stall_timeout_seconds:g} seconds",
                result.exit_code,
            )
        if result.rejected or validator.rejection is not None:
            # Only the kind and the line number: never a line of the stream. The
            # last line of standard error, redacted, says why (ADR-0035 §4, §6).
            rejection = validator.rejection
            text = str(rejection) if rejection is not None else "malformed stream"
            detail = self._stderr_detail(result.stderr)
            summary = f"{text} ({detail})" if detail else text
            if rejection is not None and rejection.kind is Violation.MISSING_RESULT:
                # The process ended without a result: often an API failure on stderr.
                return self._failure(summary, detail, result.exit_code)
            return AgentResult(AgentOutcome.FAILED, summary, result.exit_code)
        parsed = validator.result
        if parsed is None:  # pragma: no cover - the validator rejects a missing result
            return AgentResult(AgentOutcome.FAILED, "malformed stream: no result", None)
        text = self._redact(str(parsed.get("result") or ""))[:_SUMMARY_LIMIT]
        subtype = str(parsed.get("subtype") or "")
        success = parsed.get("is_error") is False and subtype == "success"
        if result.exit_code == 0 and success:
            return AgentResult(AgentOutcome.COMPLETED, text or "Claude Code finished", 0)
        reason = subtype or "error"
        detail = self._stderr_detail(result.stderr)
        if success:  # claims success, but the process failed (F-021)
            reason = f"exit status {result.exit_code} despite a success result"
            if detail:
                reason += f" ({detail})"
            return AgentResult(
                AgentOutcome.FAILED, f"{reason}: {text}" if text else reason, result.exit_code
            )
        summary = f"{reason}: {text}" if text else reason
        return self._failure(summary, f"{text} {detail}", result.exit_code)

    @staticmethod
    def _failure(summary: str, evidence: str, exit_code: int | None) -> AgentResult:
        """A failed run, classified by what the provider reported (ADR-0029 §5).

        ``evidence`` is redacted text from the result and standard error.
        """
        failure = classify(evidence)
        if failure is None:
            return AgentResult(AgentOutcome.FAILED, summary, exit_code)
        if failure is ProviderFailure.RATE_LIMIT:
            outcome = AgentOutcome.RATE_LIMITED
        else:  # CAP or AUTH: fail closed, never retried
            outcome = AgentOutcome.PROVIDER_REFUSED
        return AgentResult(outcome, f"provider {failure.value}: {summary}", exit_code)

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
            "stream-json",
            "--verbose",
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

    def _require_one_credential_path(
        self, runtime: ContainerRuntime, container: ContainerHandle, env: Mapping[str, str]
    ) -> None:
        """Refuse the run if the sandbox offers any other route to a model.

        Raises ``PolicyViolationError`` (never retried) naming the variables,
        files or settings found, never their values; ``ProviderError`` if the
        probe cannot run, so Claude Code never starts unchecked.
        """
        gateway = self._settings.base_url is not None
        own = own_environment_refusal(env, gateway_configured=gateway)
        if own is not None:  # pragma: no cover - the environment is fixed above
            raise PolicyViolationError(own)
        probe = runtime.run(
            container,
            probe_command(),
            timeout_seconds=self._settings.version_timeout_seconds,
            env=env,
        )
        report = parse_probe(probe.stdout) if probe.succeeded else None
        if report is None:
            raise ProviderError(
                f"the credential-path probe did not complete (exit {probe.exit_code})"
            )
        reason = refusal(report, gateway_configured=gateway)
        if reason is not None:
            raise PolicyViolationError(reason)

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

    def _stderr_detail(self, stderr: str) -> str:
        lines = stderr.strip().splitlines()
        return self._redact(lines[-1])[:500] if lines else ""

    def _redact(self, text: str) -> str:
        """Remove the key and every other value of the agent command's environment
        (ADR-0035 §6). Values shorter than ``_REDACT_MIN`` (flags such as ``1``)
        cannot be secrets and would mangle ordinary text."""
        for value in sorted(self._environment().values(), key=len, reverse=True):
            if len(value) >= _REDACT_MIN:
                text = text.replace(value, REDACTED)
        return text
