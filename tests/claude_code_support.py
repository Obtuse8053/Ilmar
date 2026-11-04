# SPDX-License-Identifier: Apache-2.0
"""Scripted stand-ins for the ``claude`` CLI, speaking its documented JSON result format."""

from __future__ import annotations

import json

from contracts.agent_runner import AgentScenario


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


SUCCESS = result_json(subtype="success", is_error=False, text="Fixed the bug in app.py.")
ERROR = result_json(subtype="error_during_execution", is_error=True, text="Tool failed.")

# (exit code, stdout, stderr) per contract scenario; HANGS is handled by timing out.
OUTPUTS: dict[AgentScenario, tuple[int, str, str]] = {
    AgentScenario.COMPLETES: (0, SUCCESS + "\n", ""),
    AgentScenario.REPORTS_ERROR_WITH_EXIT_ZERO: (0, ERROR + "\n", ""),
    AgentScenario.CLAIMS_SUCCESS_WITH_NONZERO_EXIT: (1, SUCCESS + "\n", ""),
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
    if scenario is AgentScenario.HANGS:
        lines.append("sleep 600")
    else:
        code, out, err = OUTPUTS[scenario]
        if out:
            lines.append(f"cat <<'ILMAR_EOF'\n{out.rstrip()}\nILMAR_EOF")
        if err:
            lines.append(f"cat >&2 <<'ILMAR_EOF'\n{err.rstrip()}\nILMAR_EOF")
        lines.append(f"exit {code}")
    return "\n".join(lines) + "\n"
