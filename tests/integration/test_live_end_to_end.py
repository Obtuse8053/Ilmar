# SPDX-License-Identifier: Apache-2.0
"""Opt-in live end to end (#11d): GitHub, Podman and real Claude Code, nothing faked.

Runs against ``<owner>/ilmar-disposable`` only, with a short-lived, repository-
scoped fine-grained token and a dedicated, spend-capped Anthropic key, both
from the environment. See ``docs/operations/live-testing.md`` for how to set
up and run it; the guard (:func:`live_harness.preflight`) refuses anything else
before touching the repository.

1. ``test_preflight``: the repository and token are exactly as required.
2. ``test_a_task_is_published_verified_merged_and_completed``: one issue becomes
   one verified pull request; merging it completes the task and releases the
   claim.
3. ``test_a_coordinator_killed_mid_agent_recovers_with_one_pull_request``: the
   crash drill.

Each run gets its own base branch, label and issue; nothing existing is
rewritten, and everything the run made is closed or deleted afterwards
(``ILMAR_LIVE_KEEP=1`` keeps it for inspection). A JSON record without any
credential is written to ``ILMAR_LIVE_REPORT_DIR``.
"""

from __future__ import annotations

import os
import secrets as random
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import live_harness as h
from ilmar.adapters.github.client import urllib_transport
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import OperationKind, OperationStatus, ResourceKind, ResourceStatus

pytestmark = pytest.mark.integration

SRC = Path(__file__).resolve().parents[2] / "src"
REQUIRED = os.environ.get("ILMAR_LIVE_REQUIRED") == "1"
KEEP = os.environ.get("ILMAR_LIVE_KEEP") == "1"
CLI_TIMEOUT = 1800


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> h.LiveSettings:
    if missing := h.missing_variables(os.environ):
        message = f"the live tests need: {', '.join(missing)} (docs/operations/live-testing.md)"
        if REQUIRED:
            pytest.fail(message)
        pytest.skip(message)
    return h.settings_from(os.environ, default_report=tmp_path_factory.mktemp("live-report"))


@pytest.fixture(scope="module")
def github(live: h.LiveSettings) -> h.LiveGitHub:
    return h.LiveGitHub(live.github_token, urllib_transport)


@pytest.fixture(scope="module")
def checked(github: h.LiveGitHub, live: h.LiveSettings) -> h.Preflight:
    return h.preflight(github, live, now=datetime.now(UTC))


@dataclass
class Session:
    """One live run: its branch, issue, state directory and CLI invocations."""

    live: h.LiveSettings
    github: h.LiveGitHub
    run: h.LiveRun
    root: Path
    outputs: list[str] = field(default_factory=list)

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def config(self) -> Path:
        return self.root / "config" / "live.toml"

    @property
    def instance(self) -> str:
        return "live-" + self.run.run_id[:12]

    @property
    def database(self) -> Path:
        return self.state / "executions.sqlite3"

    @property
    def coordinator_repository(self) -> Path:
        slug = self.live.repository.replace("/", "__")
        return self.state / "coordinator" / "source-control" / "repos" / f"{slug}.git"

    def command(self) -> list[str]:
        return [sys.executable, "-m", "ilmar.cli", "run", "--once", "--config", str(self.config)]

    def environment(self) -> dict[str, str]:
        env = h.child_environment(self.live, os.environ)
        env["PYTHONPATH"] = str(SRC)
        return env

    def cli(self) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(  # noqa: S603 - our own CLI, fixed argv
            self.command(), env=self.environment(), capture_output=True, text=True,
            timeout=CLI_TIMEOUT, check=False,
        )  # fmt: skip
        self.outputs.append(done.stdout + done.stderr)
        return done

    def shown(self, done: subprocess.CompletedProcess[str]) -> str:
        """The CLI's output for an assertion message, with every credential redacted."""
        return h.redact(f"exit {done.returncode}\n{done.stdout}\n{done.stderr}", self.live.secrets)

    def store(self) -> SQLiteExecutionStore:
        return SQLiteExecutionStore(self.database)


@pytest.fixture
def session(
    live: h.LiveSettings, github: h.LiveGitHub, checked: h.Preflight, tmp_path: Path
) -> Iterator[Session]:
    run_id = f"{datetime.now(UTC):%m%d%H%M}-{random.token_hex(3)}"
    run = h.seed_run(github, live.repository, checked.default_branch, run_id)
    current = Session(live, github, run, tmp_path.resolve())
    h.write_config(current.config, h.live_config(live, run, current.state))
    try:
        yield current
    finally:
        # Always, also after a failure: no sandbox may keep running with a
        # credential in memory, and no file may keep one on disk.
        removed = h.SubprocessPodman().remove_instance(current.instance)
        wiped = [f"state/{p}" for p in h.wipe_leaks(current.state, live.secrets)]
        wiped += [f"config/{p}" for p in h.wipe_leaks(current.config.parent, live.secrets)]
        problems = [] if KEEP else h.cleanup_run(github, live.repository, run)
        assert wiped == [], f"credential files found and deleted: {wiped}"
        assert problems == [], problems
        del removed  # removing leftovers after a failure is expected; after a pass, none exist


# -- checks shared by both runs ------------------------------------------------------------


def published(session: Session) -> Execution:
    with session.store() as store:
        (execution,) = store.list_in_states(set(ExecutionState))
        assert execution.state is ExecutionState.SUCCEEDED, execution.last_error
        assert store.unfinished_operations() == ()
        rows = store.resources(execution_id=execution.id)
        assert not [r for r in rows if r.status is ResourceStatus.QUARANTINED]
        return execution


def verified_pull_request(session: Session, execution: Execution) -> dict[str, Any]:
    """The one pull request, whose head is exactly the frozen, verified candidate."""
    candidate = execution.candidate_revision
    assert candidate is not None
    with session.store() as store:
        evidence = [e for e in store.evidence(execution.id) if e.attempt == execution.attempt]
        verify = [
            o
            for o in store.operations(execution.id)
            if o.kind is OperationKind.VERIFY
            and o.attempt == execution.attempt
            and o.status is OperationStatus.SUCCEEDED
        ]
    assert len(evidence) == 1
    assert len(verify) == 1
    record = evidence[0]
    assert record.candidate_revision == candidate
    assert record.base_revision == execution.base_revision
    assert record.verdict.publishable
    assert record.digest() == verify[0].result["evidence_digest"]

    repo = session.live.repository
    requests = h.change_requests_for(session.github, repo, session.run.base_branch)
    assert len(requests) == 1, [r["number"] for r in requests]  # never a duplicate
    request = requests[0]
    assert request["head"]["sha"] == candidate
    assert request["head"]["ref"] == execution.branch
    branch = session.github.ok("GET", f"/repos/{repo}/git/ref/heads/{execution.branch}")
    assert branch["object"]["sha"] == candidate
    files = session.github.ok("GET", f"/repos/{repo}/pulls/{request['number']}/files")
    changed = {f["filename"] for f in files}
    assert "calc.py" in changed
    assert not [p for p in changed if p.startswith(".github/")]
    frozen = subprocess.run(  # noqa: S603 - fixed argv
        [h.tool("git"), "--git-dir", str(session.coordinator_repository),
         "diff", "--name-only", "--no-renames", execution.base_revision or "", candidate],
        capture_output=True, text=True, check=True, env=h.host_environment(),
    ).stdout.split()  # fmt: skip
    assert changed == set(frozen)  # the PR shows exactly the verified change
    return request


def sandboxes_were_contained(session: Session, watcher: h.Watcher) -> dict[str, Any]:
    """The agent reached only the Anthropic API; no sandbox carried the GitHub token."""
    token, key = session.live.github_token.env, session.live.anthropic_key.env
    agents = [s for s in watcher.sandboxes() if "-verify-" not in s.name]
    verifiers = [s for s in watcher.sandboxes() if "-verify-" in s.name]
    assert agents, "the agent sandbox was never seen"
    assert verifiers, "no verification sandbox was seen"
    assert watcher.errors == []
    for seen in watcher.seen.values():
        assert token not in seen.secrets_in_definition, seen.name
        if seen not in agents:
            assert key not in seen.secrets_in_definition, seen.name
    for seen in verifiers:
        assert seen.network_mode == "none", (seen.name, seen.network_mode)
    for seen in agents:
        # Exactly one network: the run's internal one, whose only way out is the proxy.
        assert seen.networks == (f"{seen.name}-net",), (seen.name, seen.networks)
        assert watcher.internal_networks.get(seen.networks[0]) is True, seen.networks
    assert watcher.environment_leaks == set(), sorted(watcher.environment_leaks)
    proxies = watcher.proxies()
    assert len(proxies) == len(agents)
    hosts: set[str] = set()
    for proxy in proxies:
        assert proxy.allowed_hosts == (h.ANTHROPIC_HOST,), proxy.allowed_hosts
        allowed, denied = h.parse_egress(proxy.log)
        assert denied == [], denied
        assert set(allowed) <= {h.ANTHROPIC_HOST}, allowed
        hosts.update(allowed)
    assert hosts == {h.ANTHROPIC_HOST}, "Claude Code never reached the model API"
    assert watcher.argv_leaks == set(), sorted(watcher.argv_leaks)
    return {"agent_sandboxes": [s.name for s in agents], "hosts_contacted": sorted(hosts)}


def nothing_leaked(session: Session, execution: Execution, request: dict[str, Any]) -> None:
    """The credentials are in no file, log, evidence, candidate or GitHub text.

    Every search runs outside the ``assert``: pytest prints the arguments of
    calls made inside a failing assertion, which would show the searched data.
    Only the findings (paths and variable names) are ever compared or shown.
    """
    secrets = session.live.secrets
    found: dict[str, list[str]] = {}
    for where, tree in (("state", session.state), ("config", session.config.parent)):
        for path, names in h.scan_tree(tree, secrets).items():
            found[f"{where}/{path}"] = names
    for directory in {Path(tempfile.gettempdir()), Path(os.environ.get("XDG_RUNTIME_DIR", "/"))}:
        for name, names in h.secret_spool_files(directory, secrets).items():
            found[f"{directory}/{name}"] = names
    for index, output in enumerate(session.outputs):
        found[f"cli output {index}"] = h.find_secrets(output.encode(), secrets)
    with session.store() as store:
        for record in store.evidence(execution.id):
            found[f"evidence {record.attempt}"] = h.find_secrets(record.to_json().encode(), secrets)
    candidate = execution.candidate_revision or ""
    for path, content in h.git_blobs(session.coordinator_repository, candidate):
        found[f"candidate {path}"] = h.find_secrets(content, secrets)
    repo = session.live.repository
    texts = [str(request.get("title")), str(request.get("body"))]
    for path in (
        f"/repos/{repo}/issues/{request['number']}/comments",
        f"/repos/{repo}/issues/{session.run.issue}/comments",
        f"/repos/{repo}/pulls/{request['number']}/commits",
    ):
        texts += [str(item) for item in session.github.ok("GET", path)]
    found["github texts"] = h.find_secrets("\n".join(texts).encode(), secrets)
    leaks = {where: names for where, names in found.items() if names}
    assert leaks == {}


def nothing_left_running(session: Session, watcher: h.Watcher) -> None:
    podman = h.SubprocessPodman()
    assert podman.containers(session.instance) == []
    orphans = h.processes_mentioning(watcher.seen)  # e.g. a podman exec outliving its parent
    assert orphans == []
    networks = subprocess.run(  # noqa: S603 - fixed argv
        [h.tool("podman"), "network", "ls", "-q", "--filter",
         f"label=io.ilmar.owner.instance={session.instance}"],
        capture_output=True, text=True, check=True, env=h.host_environment(),
    ).stdout.split()  # fmt: skip
    assert networks == []


def labels(session: Session) -> set[str]:
    issue = session.github.ok("GET", f"/repos/{session.live.repository}/issues/{session.run.issue}")
    return {label["name"] for label in issue["labels"]}


def image_digest(image: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed argv
        [h.tool("podman"), "image", "inspect", "--format", "{{.Digest}}",
         image], capture_output=True, text=True, check=False, env=h.host_environment(),
    )  # fmt: skip
    return str(done.stdout).strip()


# -- the tests ---------------------------------------------------------------------------------


def test_preflight(checked: h.Preflight, live: h.LiveSettings) -> None:
    assert len(checked.checks) == 11
    h.write_report(live.report_dir, "preflight", {
        "repository": checked.repository, "token_expires": checked.token_expires,
        "checks": checked.checks, "at": datetime.now(UTC).isoformat(),
    }, live.secrets)  # fmt: skip


def test_a_task_is_published_verified_merged_and_completed(session: Session) -> None:
    started = time.monotonic()
    with h.Watcher(h.SubprocessPodman(), session.instance, session.live.secrets) as watcher:
        first = session.cli()
    code = first.returncode  # an int: a failed assert never prints the raw output
    assert code == 0, session.shown(first)

    execution = published(session)
    request = verified_pull_request(session, execution)
    contained = sandboxes_were_contained(session, watcher)
    nothing_leaked(session, execution, request)
    nothing_left_running(session, watcher)
    assert not session.state.joinpath("workspaces", execution.id).exists()  # deleted on success
    assert "ilmar:claimed" in labels(session)  # held while the pull request is open

    repo = session.live.repository
    merged = session.github.ok(
        "PUT", f"/repos/{repo}/pulls/{request['number']}/merge",
        {"sha": execution.candidate_revision, "merge_method": "merge"},
    )  # fmt: skip
    assert merged["merged"] is True

    second = session.cli()
    code = second.returncode  # an int: a failed assert never prints the raw output
    assert code == 0, session.shown(second)
    completed = f"task completed (change request merged): {execution.id}" in second.stdout
    assert completed, session.shown(second)
    assert "ilmar:claimed" not in labels(session)  # the claim is released
    with session.store() as store:
        (row,) = [r for r in store.resources(execution_id=execution.id)
                  if r.kind is ResourceKind.CHANGE_REQUEST]  # fmt: skip
        assert (row.status, row.note) == (ResourceStatus.RELEASED, "merged: task complete")
        assert store.unfinished_operations() == ()
    comments = session.github.ok("GET", f"/repos/{repo}/issues/{session.run.issue}/comments")
    assert any("this task is complete" in str(c.get("body")) for c in comments)
    nothing_leaked(session, execution, request)

    h.write_report(session.live.report_dir, f"end-to-end-{session.run.run_id}", {
        "run": session.run.run_id, "pull_request": request["html_url"],
        "candidate": execution.candidate_revision, "attempts": execution.attempt,
        "claude_code_version": session.live.claude_version,
        "claude_image_digest": image_digest(session.live.claude_image),
        "seconds": round(time.monotonic() - started), **contained,
        "at": datetime.now(UTC).isoformat(),
    }, session.live.secrets)  # fmt: skip


def test_a_coordinator_killed_mid_agent_recovers_with_one_pull_request(session: Session) -> None:
    """The crash drill: SIGKILL while Claude Code works; the next pass recovers safely."""
    delay = float(os.environ.get("ILMAR_LIVE_CRASH_DELAY", "2"))
    agent_intended = (OperationKind.RUN_AGENT.value, 1, OperationStatus.INTENDED.value)

    def agent_in_progress() -> bool:
        return any(
            (k, a, st) == agent_intended for k, a, st, _ in h.read_operations(session.database)
        )

    with h.Watcher(h.SubprocessPodman(), session.instance, session.live.secrets) as watcher:
        process = subprocess.Popen(  # noqa: S603 - our own CLI, fixed argv
            session.command(), env=session.environment(), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )  # fmt: skip
        try:
            # Mid-agent for real: the agent's run is recorded as in progress and
            # Claude Code has reached the model through the proxy.
            talking = h.wait_for(
                lambda: (
                    agent_in_progress()
                    and f"ALLOW host={h.ANTHROPIC_HOST}" in watcher.proxy_log_so_far()
                ),
                timeout=600,
            )
            assert talking, "Claude Code never reached the model"
            time.sleep(delay)
            assert agent_in_progress(), "the agent finished before the kill; lower the delay"
            assert process.poll() is None, "the coordinator finished before the crash"
        finally:
            os.killpg(process.pid, signal.SIGKILL)  # the coordinator dies; no cleanup runs
            output, _ = process.communicate(timeout=60)
            session.outputs.append(output)
        interrupted = [s.name for s in watcher.sandboxes() if "-verify-" not in s.name]
        assert len(interrupted) == 1
        left = subprocess.run(  # noqa: S603 - fixed argv
            [h.tool("podman"), "container", "exists", interrupted[0]],
            check=False, env=h.host_environment(),
        )  # fmt: skip
        assert left.returncode == 0, "the crash should leave the sandbox behind"
        assert h.change_requests_for(session.github, session.live.repository,
                                     session.run.base_branch) == []  # fmt: skip

        recovered = session.cli()
    code = recovered.returncode  # an int: a failed assert never prints the raw output
    assert code == 0, session.shown(recovered)

    execution = published(session)
    assert execution.attempt == 2  # a new attempt in a new sandbox (ADR-0022, decision 3)
    with session.store() as store:
        runs = sorted((o.attempt, o.status) for o in store.operations(execution.id)
                      if o.kind is OperationKind.RUN_AGENT)  # fmt: skip
    assert runs == [(1, OperationStatus.FAILED), (2, OperationStatus.SUCCEEDED)]
    request = verified_pull_request(session, execution)  # exactly one pull request
    contained = sandboxes_were_contained(session, watcher)
    assert len(contained["agent_sandboxes"]) == 2
    assert interrupted[0] in contained["agent_sandboxes"]
    nothing_leaked(session, execution, request)
    nothing_left_running(session, watcher)  # the dead run's sandbox, sidecar and network

    h.write_report(session.live.report_dir, f"crash-drill-{session.run.run_id}", {
        "run": session.run.run_id, "pull_request": request["html_url"],
        "attempts": execution.attempt, "killed_after_seconds_of_agent_work": delay,
        **contained, "at": datetime.now(UTC).isoformat(),
    }, session.live.secrets)  # fmt: skip
