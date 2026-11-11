# SPDX-License-Identifier: Apache-2.0
"""One credential path for Claude Code (ADR-0029 §5, ADR-0036, #90).

The agent may reach a model only through the one configured API key, whose
Anthropic workspace carries the spend limit. Two layers enforce this.

**The sandbox boundary (ADR-0036).** Claude Code reads settings, including
``apiKeyHelper`` and ``env``, from ``.claude/`` in the project and from
``~/.claude/settings*.json``, and may reload them while it runs. The agent
sandbox therefore seals those locations (``SEALED_PATHS``): the project's
``.claude/`` and both user settings files are empty, read-only mounts that the
agent can neither write, remove nor rename, and ``~/.claude`` itself is a
writable mount that cannot be renamed away. Nothing the agent does after the
check can create a settings file that Claude Code reads.

**The probe, before Claude Code starts.** A fixed script runs in the sandbox
with exactly the environment Claude Code will get. It reports variable
*names* (never values), the credential files that exist, project settings
that mention a credential setting, and the mount table. The run is refused
unless all of these hold:

* the probe finished every step: ``awk`` and ``grep`` are present, the
  environment, settings and mount scans completed, every file it had to read
  could be read, and every variable the runner passes was reported;
* there is no ``ANTHROPIC_*`` variable other than the key, and no
  ``ANTHROPIC_BASE_URL`` unless a gateway is configured;
* there is no OAuth token and no Bedrock, Vertex or other cloud route
  (``CLAUDE_CODE_OAUTH_TOKEN``, ``CLAUDE_CODE_USE_*``, ``AWS_*`` and so on);
* there are no Claude login or state files, managed settings, or cloud
  credential directories;
* no project settings mention a credential setting (this fails closed on any
  mention);
* every sealed path is in place with the right access.

Anything incomplete or unexpected refuses: Claude Code never starts unchecked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from ilmar.domain.workspace import SealedPath

PROBE_SHELL = "/bin/sh"
PROBE_END = "ILMAR-PROBE-END"
PROBE_ERROR = "ILMAR-PROBE-ERROR"

SEALED_PATHS = (
    SealedPath(".claude"),  # project and local settings, hooks, apiKeyHelper
    SealedPath("~/.claude", writable=True),  # Claude Code's state; cannot be renamed away
    SealedPath("~/.claude/settings.json"),
    SealedPath("~/.claude/settings.local.json"),
)

HOME_FILES = (
    "$HOME/.claude.json",
    "$HOME/.claude/.credentials.json",
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


def _shell_path(sealed: SealedPath) -> str:
    return "$HOME/" + sealed.path[2:] if sealed.path.startswith("~/") else sealed.path


READ_ONLY_SEALS = tuple(_shell_path(s) for s in SEALED_PATHS if not s.writable)


PROBE_SCRIPT = "\n".join(
    [
        "for tool in awk grep; do",
        '  command -v "$tool" >/dev/null 2>&1 || { echo "' + PROBE_ERROR + ' missing $tool"; '
        "exit 3; }",
        "done",
        'awk \'BEGIN { for (name in ENVIRON) print "ENV " name; print "ENV-DONE" }\' '
        '|| { echo "' + PROBE_ERROR + ' environment"; exit 3; }',
        'echo "HOME-DIR $HOME"',
        'echo "WORKDIR $(pwd)"',
        "for f in " + _quoted(HOME_FILES) + "; do",
        '  if [ -e "$f" ] || [ -L "$f" ]; then echo "FILE $f"; fi',
        "done",
        "for f in " + _quoted(PROJECT_SETTINGS) + "; do",
        '  if [ -e "$f" ] || [ -L "$f" ]; then',
        '    grep -qE "' + SETTING_PATTERN + '" "$f"',
        "    status=$?",
        '    if [ "$status" -eq 0 ]; then echo "SETTING $f";',
        '    elif [ "$status" -ne 1 ]; then echo "' + PROBE_ERROR + ' read $f"; exit 4; fi',
        "  fi",
        "done",
        # A read-only seal must be empty: nothing copied or left in it.
        "for d in " + _quoted(READ_ONLY_SEALS) + "; do",
        '  for e in "$d"/* "$d"/.[!.]* "$d"/..?*; do',
        '    if [ -e "$e" ] || [ -L "$e" ]; then echo "NONEMPTY $d"; break; fi',
        "  done",
        "done",
        'awk \'{ print "MOUNT " $2 " " $4 } END { print "MOUNTS-DONE" }\' /proc/self/mounts '
        '|| { echo "' + PROBE_ERROR + ' mounts"; exit 5; }',
        "echo " + PROBE_END,
        "",
    ]
)

ALLOWED_ANTHROPIC = frozenset({"ANTHROPIC_API_KEY"})
FORBIDDEN_NAMES = frozenset(
    {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",  # would move the user settings out of the sealed paths
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
    home: PurePosixPath
    workdir: PurePosixPath
    mounts: tuple[tuple[str, frozenset[str]], ...]
    """Mount points and options, in the order of the mount table."""
    nonempty: tuple[str, ...] = ()


def parse_probe(stdout: str) -> ProbeReport | None:
    """``None`` unless every step of the probe finished and reported."""
    variables: set[str] = set()
    files: list[str] = []
    settings: list[str] = []
    mounts: list[tuple[str, frozenset[str]]] = []
    nonempty: list[str] = []
    home = workdir = None
    seen: set[str] = set()
    for line in stdout.splitlines():
        kind, _, rest = line.partition(" ")
        if kind in {PROBE_ERROR, PROBE_END, "ENV-DONE", "MOUNTS-DONE"}:
            seen.add(kind)
        elif kind == "ENV" and rest:
            variables.add(rest)
        elif kind == "FILE" and rest:
            files.append(rest)
        elif kind == "SETTING" and rest:
            settings.append(rest)
        elif kind == "HOME-DIR" and rest.startswith("/"):
            home = PurePosixPath(rest)
        elif kind == "WORKDIR" and rest.startswith("/"):
            workdir = PurePosixPath(rest)
        elif kind == "MOUNT":
            point, _, options = rest.partition(" ")
            mounts.append((point, frozenset(options.split(","))))
        elif kind == "NONEMPTY" and rest:
            nonempty.append(rest)
    complete = {PROBE_END, "ENV-DONE", "MOUNTS-DONE"} <= seen and PROBE_ERROR not in seen
    if not complete or home is None or workdir is None:
        return None
    return ProbeReport(
        frozenset(variables),
        tuple(files),
        tuple(settings),
        home,
        workdir,
        tuple(mounts),
        tuple(nonempty),
    )


def forbidden_variables(names: Iterable[str], *, gateway_configured: bool) -> list[str]:
    """The variable names that open another route to a model, sorted."""
    allowed = ALLOWED_ANTHROPIC | ({"ANTHROPIC_BASE_URL"} if gateway_configured else set())
    found = set()
    for name in names:
        other_anthropic = name.startswith("ANTHROPIC_") and name not in allowed
        if other_anthropic or name in FORBIDDEN_NAMES or name.startswith(FORBIDDEN_PREFIXES):
            found.add(name)
    return sorted(found)


def sealed_target(sealed: SealedPath, report: ProbeReport) -> PurePosixPath:
    if sealed.path.startswith("~/"):
        return report.home / sealed.path[2:]
    if sealed.path.startswith("/"):
        return PurePosixPath(sealed.path)
    return report.workdir / sealed.path


def _top_mount(report: ProbeReport, target: PurePosixPath) -> tuple[str, frozenset[str]] | None:
    """The mount that is visible at ``target``: the last one in the table whose
    mount point is ``target`` or one of its parents. A later mount on a parent
    hides an earlier one below it."""
    for point, options in reversed(report.mounts):
        mounted = PurePosixPath(point)
        if mounted == target or mounted in target.parents:
            return point, options
    return None


def unsealed(report: ProbeReport, sealed_paths: Iterable[SealedPath]) -> list[str]:
    """The sealed paths that are not the visible mount, with the access they need."""
    missing = []
    for sealed in sealed_paths:
        target = sealed_target(sealed, report)
        top = _top_mount(report, target)
        wanted = "rw" if sealed.writable else "ro"
        if top is None or top[0] != str(target) or wanted not in top[1]:
            missing.append(str(target))
    return missing


def refusal(
    report: ProbeReport,
    *,
    gateway_configured: bool,
    expected_variables: Iterable[str] = (),
    sealed_paths: Iterable[SealedPath] = SEALED_PATHS,
) -> str | None:
    """Why the sandbox is not safe to start Claude Code in, by name only."""
    parts: list[str] = []
    if absent := sorted(set(expected_variables) - report.variables):
        parts.append("incomplete environment scan, missing " + ", ".join(absent))
    if variables := forbidden_variables(report.variables, gateway_configured=gateway_configured):
        parts.append("variables " + ", ".join(variables))
    if report.files:
        parts.append("files " + ", ".join(sorted(report.files)))
    if report.settings:
        parts.append("project settings " + ", ".join(sorted(report.settings)))
    if missing := unsealed(report, sealed_paths):
        parts.append("settings locations not sealed: " + ", ".join(missing))
    if report.nonempty:
        parts.append("sealed settings locations not empty: " + ", ".join(report.nonempty))
    if not parts:
        return None
    return "the agent sandbox is not limited to one model credential: " + "; ".join(parts)


def own_environment_refusal(env: Mapping[str, str], *, gateway_configured: bool) -> str | None:
    """The runner's own environment must pass the same rule (checked before use)."""
    bad = forbidden_variables(env, gateway_configured=gateway_configured)
    return f"the runner environment names {', '.join(bad)}" if bad else None
