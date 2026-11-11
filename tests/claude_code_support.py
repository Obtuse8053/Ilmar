# SPDX-License-Identifier: Apache-2.0
"""Scripted stand-ins for the ``claude`` CLI, speaking its documented ``stream-json``
format (ADR-0035): one JSON event per line, ending with the result object."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from ilmar.domain.workspace import CommandResult

from contracts.agent_runner import AgentScenario
from fakes import FakeContainerRuntime


def result_json(*, subtype: str, is_error: bool, text: str) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": subtype,
            "is_error": is_error,
            "result": text,
            "session_id": "00000000-0000-0000-0000-000000000000",
            "num_turns": 3,
            "duration_ms": 1200,
            "total_cost_usd": 0.01,
        }
    )


INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "s", "tools": ["Bash"]})
ASSISTANT = json.dumps(
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking."}]}}
)
TOOL_RESULT = json.dumps(
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
)
RATE_LIMIT = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})
API_RETRY = json.dumps({"type": "system", "subtype": "api_retry", "attempt": 1})


def stream(
    result: str,
    *,
    before: tuple[str, ...] = (INIT, ASSISTANT, TOOL_RESULT),
    after: tuple[str, ...] = (),
) -> str:
    """A complete ``stream-json`` transcript around one result line."""
    return "".join(f"{line}\n" for line in (*before, result, *after))


SUCCESS_RESULT = result_json(subtype="success", is_error=False, text="Fixed the bug in app.py.")
ERROR_RESULT = result_json(subtype="error_during_execution", is_error=True, text="Tool failed.")
SUCCESS = stream(SUCCESS_RESULT)
ERROR = stream(ERROR_RESULT)

# (exit code, stdout, stderr) per contract scenario; HANGS is handled by timing out.
OUTPUTS: dict[AgentScenario, tuple[int, str, str]] = {
    AgentScenario.COMPLETES: (0, SUCCESS, ""),
    AgentScenario.REPORTS_ERROR_WITH_EXIT_ZERO: (0, ERROR, ""),
    AgentScenario.CLAIMS_SUCCESS_WITH_NONZERO_EXIT: (1, SUCCESS, ""),
    AgentScenario.CRASHES: (2, "", "Error: something broke\n"),
    AgentScenario.UNPARSEABLE_OUTPUT: (0, "All done!\n", ""),
}


def shell_script(scenario: AgentScenario, record: str) -> str:
    """A POSIX ``claude`` stand-in for real containers; logs argv and env to ``record``."""
    lines = [
        "#!/bin/sh",
        f'printf "%s\\n" "$@" > "{record}.argv"',
        f'env > "{record}.env"',
        'if [ "$1" = "--version" ]; then echo "9.9.9 (Claude Code)"; exit 0; fi',
    ]
    if scenario is AgentScenario.HANGS:  # busy: progress every second, never done
        lines.append("while :; do printf '%s\\n' '{\"type\": \"assistant\"}'; sleep 1; done")
    elif scenario is AgentScenario.STALLS:  # one event, then silence
        lines.append(f"printf '%s\\n' '{INIT}'")
        lines.append("sleep 600")
    else:
        code, out, err = OUTPUTS[scenario]
        if out:
            lines.append(f"cat <<'ILMAR_EOF'\n{out.rstrip()}\nILMAR_EOF")
        if err:
            lines.append(f"cat >&2 <<'ILMAR_EOF'\n{err.rstrip()}\nILMAR_EOF")
        lines.append(f"exit {code}")
    return "\n".join(lines) + "\n"


# -- the credential-path probe (ADR-0029 §5, #90) ------------------------------------------


class ProbeStandIn:
    """Answers ``/bin/sh -c <probe>`` the way the real probe does in a correctly
    sealed sandbox: the names of the environment it was given plus any
    ``image_env``, listed files and settings, the home and working directory,
    and the mount table with every sealed path in place unless named in
    ``unsealed``. ``drop`` removes output lines that start with a marker (to
    simulate a step that never finished). Records each environment it saw."""

    def __init__(
        self,
        *,
        image_env: Sequence[str] = (),
        hide_env: Sequence[str] = (),
        files: Sequence[str] = (),
        settings: Sequence[str] = (),
        unsealed: Sequence[str] = (),
        wrong_access: Sequence[str] = (),
        exit_code: int = 0,
        drop: Sequence[str] = (),
        extra: Sequence[str] = (),
    ) -> None:
        self.image_env = tuple(image_env)
        self.hide_env = set(hide_env)
        self.files = tuple(files)
        self.settings = tuple(settings)
        self.unsealed = set(unsealed)
        self.wrong_access = set(wrong_access)
        self.exit_code = exit_code
        self.drop = tuple(drop)
        self.extra = tuple(extra)
        self.seen: list[dict[str, str]] = []

    def __call__(
        self, argv: Sequence[str], timeout: float, env: Mapping[str, str]
    ) -> CommandResult:
        from ilmar.adapters.claude_code.credential_route import (
            PROBE_END,
            PROBE_SCRIPT,
            SEALED_PATHS,
        )

        assert list(argv) == ["/bin/sh", "-c", PROBE_SCRIPT], argv
        self.seen.append(dict(env))
        names = sorted({*env, *self.image_env, "HOME", "PATH"} - self.hide_env)
        lines = [f"ENV {name}" for name in names] + ["ENV-DONE"]
        lines += ["HOME-DIR /home/ilmar", "WORKDIR /workspace"]
        lines += [f"FILE {f}" for f in self.files] + [f"SETTING {s}" for s in self.settings]
        lines += ["MOUNT / ro,nosuid", "MOUNT /workspace rw,relatime", "MOUNT /home/ilmar rw"]
        for sealed in SEALED_PATHS:
            target = (
                "/home/ilmar/" + sealed.path[2:]
                if sealed.path.startswith("~/")
                else "/workspace/" + sealed.path
            )
            if target in self.unsealed:
                continue
            access = "rw" if sealed.writable else "ro"
            if target in self.wrong_access:
                access = "ro" if access == "rw" else "rw"
            lines.append(f"MOUNT {target} {access},nosuid,nodev")
        lines += ["MOUNTS-DONE", *self.extra, PROBE_END]
        kept = [line for line in lines if not line.startswith(self.drop)] if self.drop else lines
        return CommandResult(self.exit_code, "\n".join(kept) + "\n", "", 0.1)


def allow_probe(runtime: FakeContainerRuntime, probe: ProbeStandIn | None = None) -> ProbeStandIn:
    """Register a clean probe (or ``probe``) on a fake runtime."""
    probe = probe or ProbeStandIn()
    runtime.register("/bin/sh", probe)
    return probe
