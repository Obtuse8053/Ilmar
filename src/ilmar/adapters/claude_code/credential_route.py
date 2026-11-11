# SPDX-License-Identifier: Apache-2.0
"""One credential path for Claude Code (ADR-0029 §5, #90).

The agent may reach a model only through the one configured API key, whose
Anthropic workspace carries the spend limit. Before Claude Code starts, a
fixed probe runs in the sandbox with exactly the environment Claude Code will
get. It reports variable *names* (never values), which credential files exist
and which project settings files mention a credential setting. Any other
route refuses the run:

* an ``ANTHROPIC_*`` variable other than the key (``ANTHROPIC_AUTH_TOKEN`` and
  the rest), or ``ANTHROPIC_BASE_URL`` when no gateway is configured;
* an OAuth token or a Bedrock, Vertex or other cloud-provider route
  (``CLAUDE_CODE_OAUTH_TOKEN``, ``CLAUDE_CODE_USE_*``, ``AWS_*`` and so on);
* Claude login, OAuth or user settings files in the home directory, managed
  settings, or cloud credential directories;
* project settings that mention ``apiKeyHelper``, the AWS helpers or a
  credential or provider variable. This fails closed on any mention.

The probe needs only ``/bin/sh``, ``awk`` and ``grep``. If it cannot run, or
its output is incomplete, Claude Code does not start.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

PROBE_SHELL = "/bin/sh"
PROBE_END = "ILMAR-PROBE-END"

HOME_FILES = (
    "$HOME/.claude/.credentials.json",
    "$HOME/.claude.json",
    "$HOME/.claude/settings.json",
    "$HOME/.claude/settings.local.json",
    "$HOME/.aws",
    "$HOME/.config/gcloud",
    "/etc/claude-code/managed-settings.json",
)
PROJECT_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")
SETTING_PATTERN = (
    "apiKeyHelper|awsAuthRefresh|awsCredentialExport|ANTHROPIC_|CLAUDE_CODE_|AWS_|GOOGLE_|VERTEX"
)


def _quoted(paths: Iterable[str]) -> str:
    return " ".join('"' + path + '"' for path in paths)


PROBE_SCRIPT = "\n".join(
    [
        "awk 'BEGIN { for (name in ENVIRON) print \"ENV \" name }'",
        "for f in " + _quoted(HOME_FILES) + "; do",
        '  if [ -e "$f" ]; then echo "FILE $f"; fi',
        "done",
        "for f in " + _quoted(PROJECT_SETTINGS) + "; do",
        '  if [ -f "$f" ] && grep -qE "' + SETTING_PATTERN + '" "$f"; then echo "SETTING $f"; fi',
        "done",
        "echo " + PROBE_END,
        "",
    ]
)

ALLOWED_ANTHROPIC = frozenset({"ANTHROPIC_API_KEY"})
FORBIDDEN_NAMES = frozenset(
    {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUD_ML_REGION",
    }
)
FORBIDDEN_PREFIXES = ("CLAUDE_CODE_USE_", "AWS_", "VERTEX_", "ANTHROPIC_VERTEX_")


def probe_command() -> list[str]:
    return [PROBE_SHELL, "-c", PROBE_SCRIPT]


@dataclass(frozen=True, slots=True)
class ProbeReport:
    variables: frozenset[str]
    files: tuple[str, ...]
    settings: tuple[str, ...]


def parse_probe(stdout: str) -> ProbeReport | None:
    """``None`` if the probe did not finish (no end marker)."""
    variables: set[str] = set()
    files: list[str] = []
    settings: list[str] = []
    finished = False
    for line in stdout.splitlines():
        kind, _, rest = line.partition(" ")
        if line == PROBE_END:
            finished = True
        elif kind == "ENV" and rest:
            variables.add(rest)
        elif kind == "FILE" and rest:
            files.append(rest)
        elif kind == "SETTING" and rest:
            settings.append(rest)
    if not finished:
        return None
    return ProbeReport(frozenset(variables), tuple(files), tuple(settings))


def forbidden_variables(names: Iterable[str], *, gateway_configured: bool) -> list[str]:
    """The variable names that open another route to a model, sorted."""
    allowed = ALLOWED_ANTHROPIC | ({"ANTHROPIC_BASE_URL"} if gateway_configured else set())
    found = set()
    for name in names:
        other_anthropic = name.startswith("ANTHROPIC_") and name not in allowed
        if other_anthropic or name in FORBIDDEN_NAMES or name.startswith(FORBIDDEN_PREFIXES):
            found.add(name)
    return sorted(found)


def refusal(report: ProbeReport, *, gateway_configured: bool) -> str | None:
    """Why the sandbox offers another credential route, by name only; ``None`` if not."""
    parts: list[str] = []
    if variables := forbidden_variables(report.variables, gateway_configured=gateway_configured):
        parts.append("variables " + ", ".join(variables))
    if report.files:
        parts.append("files " + ", ".join(sorted(report.files)))
    if report.settings:
        parts.append("project settings " + ", ".join(sorted(report.settings)))
    if not parts:
        return None
    return "another model credential route is present: " + "; ".join(parts)


def own_environment_refusal(env: Mapping[str, str], *, gateway_configured: bool) -> str | None:
    """The runner's own environment must pass the same rule (checked before use)."""
    bad = forbidden_variables(env, gateway_configured=gateway_configured)
    return f"the runner environment names {', '.join(bad)}" if bad else None
