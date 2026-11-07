# SPDX-License-Identifier: Apache-2.0
"""Run #44 and the live end-to-end tests (#11d) inside the manual ``live`` workflow.

    python3 scripts/live_ci.py --guard            # guard job: context, environment, main
    python3 scripts/live_ci.py --prepare DIR      # prepare job: wheels and the agent image
    python3 scripts/live_ci.py --guard-context    # live job, before any secret
    python3 scripts/live_ci.py --install-wheels WHEELS SITE
    python3 scripts/live_ci.py --load-images TAR  # live job, before any secret
    python3 scripts/live_ci.py                    # live job: the tests (the secrets)
    python3 scripts/live_ci.py --post DIR         # post job: records only

The GitHub Actions counterpart of ``scripts/live_run.py``. It runs the same
stages, record checks and posting. The only difference is where the
credentials come from: the protected ``live`` environment's secrets, which
Actions puts in this step's environment and masks in its logs. They are never
prompted for, printed or written.

It refuses to run outside GitHub Actions. Locally, ``scripts/live_run.py``
with its terminal-only prompt is the way.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from live_run import (
    ANTHROPIC_KEY_ENV,
    GITHUB_TOKEN_ENV,
    RunError,
    check_prerequisites,
    new_records,
    post_records,
    run_stages,
    run_start,
    test_environment,
    validate_credentials,
)

VERSION = re.compile(r"\d+\.\d+\.\d+")
SHA = re.compile(r"[0-9a-f]{40}")
ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
ENVIRONMENT = "live"
PROXY_IMAGE = (
    "docker.io/library/python:3.12-alpine"
    "@sha256:213242b537a1c166a0c53e903461fc18d5518e7196c93dbecda452660a848918"
)
"""Pinned by digest: it runs the egress proxy and the verification commands."""
LOCK = ROOT / "requirements" / "live.txt"
WORKFLOW_FILE = ".github/workflows/live.yml"
SITE_MARKER = ".ilmar-live-lock"
IMAGE_ARCHIVE = "claude-code.tar"
SUMMARY = "run-summary.json"
RECORDS = re.compile(
    r"(issue-44-smoke-test|preflight|end-to-end-[a-z0-9-]+|crash-drill-[a-z0-9-]+|run-summary)\.json"
)
CREDENTIAL_SHAPES = re.compile(r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}"
                               r"|sk-ant-[A-Za-z0-9_-]{20,}")  # fmt: skip
MAX_RECORD_BYTES = 64 * 1024


# -- the dispatch context and the protected environment (fail closed) -------------------


def check_context(env: Mapping[str, str]) -> list[str]:
    """Started by hand, on main, at exactly the commit the maintainer named."""
    problems = []
    if env.get("GITHUB_ACTIONS") != "true":
        problems.append("not running in GitHub Actions")
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        problems.append("not started by workflow_dispatch")
    if env.get("GITHUB_REF") != "refs/heads/main":
        problems.append("not running on refs/heads/main")
    expected = env.get("ILMAR_EXPECTED_SHA", "")
    if not SHA.fullmatch(expected):
        problems.append("expected_sha must be the full 40-character commit")
    elif env.get("GITHUB_SHA") != expected:
        problems.append("the workflow is not running the expected commit")
    elif env.get("GITHUB_WORKFLOW_SHA") != expected:
        problems.append("the workflow file is not from the expected commit")
    repository = env.get("GITHUB_REPOSITORY", "")
    if env.get("GITHUB_WORKFLOW_REF") != f"{repository}/{WORKFLOW_FILE}@refs/heads/main":
        problems.append(f"this is not {WORKFLOW_FILE} on main")
    if not VERSION.fullmatch(env.get("ILMAR_TEST_CLAUDE_VERSION", "")):
        problems.append("claude_version must be x.y.z")
    return problems


def check_environment(environment: Mapping[str, Any], policies: Mapping[str, Any]) -> list[str]:
    """The `live` environment must be set up exactly as docs/operations/live-testing.md says.

    GitHub releases environment secrets to any job its rules allow, so the
    rules are the boundary: required reviewers who cannot approve their own
    runs, main only, no admin bypass.
    """
    problems = []
    rules = environment.get("protection_rules") or []
    reviewers = [r for r in rules if r.get("type") == "required_reviewers"]
    if not reviewers or not any(r.get("reviewers") for r in reviewers):
        problems.append("the environment has no required reviewers")
    elif not all(r.get("prevent_self_review") is True for r in reviewers):
        # Whoever starts a run (a person, or automation acting as one) must
        # not be able to approve it: the approval has to come from someone else.
        problems.append("'Prevent self-review' is not enabled for the required reviewers")
    if environment.get("can_admins_bypass") is not False:
        problems.append("administrators can bypass the environment's rules")
    branch = environment.get("deployment_branch_policy") or {}
    if branch != {"protected_branches": False, "custom_branch_policies": True}:
        problems.append("deployments are not limited to selected branches")
    allowed = [(p.get("name"), p.get("type", "branch")) for p in
               policies.get("branch_policies") or []]  # fmt: skip
    if allowed != [("main", "branch")]:
        problems.append(f"the allowed branches must be exactly main, not {allowed}")
    return problems


MAIN_RULES = frozenset({"pull_request", "non_fast_forward", "deletion"})


def check_main_rules(rules: Sequence[Mapping[str, Any]]) -> list[str]:
    """`main` changes only through pull requests: a workflow pushed there directly
    would receive the secrets legitimately. Read from the branch's rulesets."""
    present = {str(rule.get("type")) for rule in rules}
    if missing := sorted(MAIN_RULES - present):
        return [f"main lacks the ruleset rules {', '.join(missing)} "
                "(require a pull request; block force pushes and deletion)"]  # fmt: skip
    return []


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> urllib.request.Request | None:
        return None  # a redirect is an error here, never followed with the token


def github_get(path: str, token: str) -> Any:
    request = urllib.request.Request(  # noqa: S310 - fixed https origin
        API + path,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ilmar-live-guard",
        },
    )
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RunError(f"GET {path}: HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise RunError(f"GET {path}: {type(error).__name__}") from None


def guard(env: Mapping[str, str], get: Callable[[str, str], Any] = github_get) -> list[str]:
    """Everything that must hold before a job may ask for the secrets."""
    problems = check_context(env)
    repository = env.get("GITHUB_REPOSITORY", "")
    token = env.get("GH_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repository) or not token:
        return [*problems, "GITHUB_REPOSITORY and GH_TOKEN are needed to read the environment"]
    try:
        environment = get(f"/repos/{repository}/environments/{ENVIRONMENT}", token)
        policies = get(
            f"/repos/{repository}/environments/{ENVIRONMENT}/deployment-branch-policies", token
        )
        rules = get(f"/repos/{repository}/rules/branches/main", token)
    except RunError as error:
        return [*problems, f"cannot read the '{ENVIRONMENT}' environment or main's rules: {error}"]
    if not isinstance(rules, list):
        return [*problems, "main's rules could not be read"]
    return [
        *problems,
        *check_environment(environment, policies),
        *check_main_rules(rules),
        *check_main_head(env, get),
    ]


def check_main_head(env: Mapping[str, str], get: Callable[[str, str], Any]) -> list[str]:
    """Asked of GitHub now, not taken from the event: main still points at the
    commit the maintainer approved. A later push to main (even a merged pull
    request) refuses the run; dispatch it again for the new commit."""
    repository = env.get("GITHUB_REPOSITORY", "")
    token = env.get("GH_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repository) or not token:
        return ["GITHUB_REPOSITORY and GH_TOKEN are needed to read main"]
    try:
        ref = get(f"/repos/{repository}/git/ref/heads/main", token)
    except RunError as error:
        return [f"cannot read main: {error}"]
    head = ref.get("object", {}).get("sha") if isinstance(ref, dict) else None
    if not isinstance(head, str) or head != env.get("ILMAR_EXPECTED_SHA"):
        return ["main no longer points at the expected commit"]
    return []


def checked_out_commit(root: Path = ROOT, run: Callable[..., Any] = subprocess.run) -> str:
    done = run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
               check=False, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})  # fmt: skip
    return str(done.stdout).strip()


# -- dependencies: exact versions and digests, unpacked by this script ---------------------

LOCK_LINE = re.compile(
    r"([a-z0-9][a-z0-9_.-]*)==([0-9][0-9a-z.]*)((?: --hash=sha256:[0-9a-f]{64})+)"
)


def parse_lock(text: str) -> dict[str, tuple[str, frozenset[str]]]:
    """requirements/live.txt: one ``name==version --hash=sha256:…`` per line, nothing else
    (no ranges, URLs, indexes, includes or options)."""
    lock: dict[str, tuple[str, frozenset[str]]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        match = LOCK_LINE.fullmatch(line.strip())
        if match is None:
            raise RunError(f"requirements/live.txt:{number}: not an exact, hashed requirement")
        name, version, hashes = match.groups()
        if name in lock:
            raise RunError(f"requirements/live.txt:{number}: {name} is listed twice")
        lock[name] = (version, frozenset(re.findall(r"[0-9a-f]{64}", hashes)))
    if not lock:
        raise RunError("requirements/live.txt lists nothing")
    return lock


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_wheels(wheels: Path, site: Path, lock_text: str) -> list[str]:
    """Unpack exactly the locked pure-Python wheels into SITE, after checking each digest.

    No pip and no build step: nothing from a wheel runs while it is installed.
    Any wheel too many or too few, any digest or file name that differs, any
    platform-specific or data-carrying wheel and any path leaving SITE stops it
    with SITE untouched.
    """
    lock = parse_lock(lock_text)
    expected = {f"{name}-{version}-py3-none-any.whl": (name, hashes)
                for name, (version, hashes) in lock.items()}  # fmt: skip
    present = sorted(p.name for p in wheels.iterdir()) if wheels.is_dir() else []
    if present != sorted(expected):
        raise RunError(f"the wheels are not exactly the locked ones: {present}")
    plans: list[tuple[Path, list[zipfile.ZipInfo]]] = []
    for file_name, (_name, hashes) in sorted(expected.items()):
        wheel = wheels / file_name
        if wheel.is_symlink() or not wheel.is_file():
            raise RunError(f"{file_name} is not a regular file")
        if sha256_of(wheel) not in hashes:
            raise RunError(f"{file_name} does not match its locked sha256")
        with zipfile.ZipFile(wheel) as archive:
            members = archive.infolist()
        for member in members:
            parts = Path(member.filename).parts
            if member.filename.startswith("/") or ".." in parts or "\\" in member.filename:
                raise RunError(f"{file_name}: {member.filename} leaves the install directory")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise RunError(f"{file_name}: {member.filename} is a symbolic link")
            if parts and parts[0].endswith(".data"):
                raise RunError(f"{file_name} installs files outside site-packages")
        plans.append((wheel, members))
    if site.exists():
        raise RunError(f"{site} already exists")
    site.mkdir(parents=True)
    for wheel, members in plans:
        with zipfile.ZipFile(wheel) as archive:
            for member in members:
                archive.extract(member, site)
    (site / SITE_MARKER).write_text(hashlib.sha256(lock_text.encode()).hexdigest() + "\n")
    return sorted(expected)


def check_site(site: Path, lock_text: str) -> None:
    """The run uses only what install_wheels unpacked from this commit's lock."""
    marker = site / SITE_MARKER
    wanted = hashlib.sha256(lock_text.encode()).hexdigest()
    if not marker.is_file() or marker.read_text().strip() != wanted:
        raise RunError("the hash-checked dependencies are not installed (--install-wheels)")


# -- the agent image: built in the prepare job, loaded by digest in the live job ----------


def podman_output(run: Callable[..., Any], *argv: str) -> str:
    done = run(["podman", *argv], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RunError(f"podman {argv[0]} failed")
    return str(done.stdout).strip()


def prepare(out: Path, env: Mapping[str, str], run: Callable[..., Any]) -> dict[str, str]:
    """In the prepare job (no secrets): every download and build of the run happens here.

    Wheels come only by exact version and digest; the agent image is built from
    the digest-pinned base and saved, so the live job loads exactly these bytes.
    """
    version = env.get("ILMAR_TEST_CLAUDE_VERSION", "")
    if not VERSION.fullmatch(version):
        raise RunError("ILMAR_TEST_CLAUDE_VERSION must be a pinned x.y.z version")
    out.mkdir(parents=True, exist_ok=False)
    pip = [sys.executable, "-m", "pip", "download", "--require-hashes", "--no-deps",
           "--only-binary=:all:", "--no-cache-dir", "--dest", str(out / "wheels"),
           "--requirement", str(LOCK)]  # fmt: skip
    if run(pip, check=False).returncode != 0:
        raise RunError("the locked wheels could not be downloaded against their hashes")
    tag = f"localhost/ilmar-claude-code:{version}"
    context = str(ROOT / "images" / "claude-code")
    build = ["podman", "build", "--pull=missing", "--build-arg",
             f"CLAUDE_CODE_VERSION={version}", "--tag", tag, context]  # fmt: skip
    if run(build, check=False).returncode != 0:
        raise RunError("the agent image did not build")
    image_id = podman_output(run, "image", "inspect", "--format", "{{.Id}}", tag)
    archive = out / IMAGE_ARCHIVE
    if run(["podman", "save", "--format", "docker-archive", "--output", str(archive), tag],
           check=False).returncode != 0:  # fmt: skip
        raise RunError("the agent image could not be saved")
    return {"image_id": image_id, "archive_sha256": sha256_of(archive)}


def load_images(archive: Path, env: Mapping[str, str], run: Callable[..., Any]) -> None:
    """In the live job, before any secret: the prepared image, checked by digest and
    ID, and the proxy image by its pinned digest. The tests then never pull
    (the runtime passes --pull=never and the prerequisites check both exist)."""
    version = env.get("ILMAR_TEST_CLAUDE_VERSION", "")
    image_id = env.get("ILMAR_IMAGE_ID", "")
    archive_sha = env.get("ILMAR_IMAGE_SHA256", "")
    if not VERSION.fullmatch(version):
        raise RunError("ILMAR_TEST_CLAUDE_VERSION must be a pinned x.y.z version")
    if not re.fullmatch(r"(sha256:)?[0-9a-f]{64}", image_id) or not re.fullmatch(
        r"[0-9a-f]{64}", archive_sha
    ):
        raise RunError("the prepare job's image ID and archive digest are missing")
    if archive.is_symlink() or not archive.is_file() or sha256_of(archive) != archive_sha:
        raise RunError("the agent image archive is not the one the prepare job built")
    podman_output(run, "load", "--quiet", "--input", str(archive))
    tag = f"localhost/ilmar-claude-code:{version}"
    podman_output(run, "tag", image_id.removeprefix("sha256:"), tag)
    loaded = podman_output(run, "image", "inspect", "--format", "{{.Id}}", tag)
    if loaded.removeprefix("sha256:") != image_id.removeprefix("sha256:"):
        raise RunError("the loaded agent image is not the one the prepare job built")
    podman_output(run, "pull", "--quiet", PROXY_IMAGE)


def write_outputs(env: Mapping[str, str], outputs: Mapping[str, str]) -> None:
    path = env.get("GITHUB_OUTPUT", "")
    if not path:
        raise RunError("GITHUB_OUTPUT is not set")
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


# -- records ------------------------------------------------------------------------------


def write_summary(reports: Path, summary: Mapping[str, Any], secrets: Sequence[str]) -> Path:
    """The run's outcome, bound to its commit; posting requires it to say "passed"."""
    text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if any(value and value in text for value in secrets) or CREDENTIAL_SHAPES.search(text):
        raise RunError("a credential reached the run summary; nothing was written")
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / SUMMARY
    path.write_text(text)
    return path


def checked_records(directory: Path, commit: str) -> list[Path]:
    """The records the post job may publish, or RunError: never anything unexpected."""
    records = sorted(directory.glob("*"))
    if not records:
        raise RunError(f"no records in {directory}")
    for record in records:
        if not record.is_file() or record.is_symlink() or not RECORDS.fullmatch(record.name):
            raise RunError(f"unexpected file in the records: {record.name}")
        data = record.read_bytes()
        if len(data) > MAX_RECORD_BYTES:
            raise RunError(f"{record.name} is too large")
        if CREDENTIAL_SHAPES.search(data.decode("utf-8", "replace")):
            raise RunError(f"{record.name} looks like it holds a credential; nothing was posted")
        json.loads(data)
    summary = (
        json.loads((directory / SUMMARY).read_text()) if (directory / SUMMARY).exists() else {}
    )
    if summary.get("outcome") != "passed":
        raise RunError("the run did not pass; its records are not posted")
    if summary.get("commit") != commit:
        raise RunError("the records are not from this commit")
    return records


def ci_settings(env: Mapping[str, str]) -> dict[str, str]:
    """The non-secret settings, from the workflow's environment."""
    if env.get("GITHUB_ACTIONS") != "true":
        raise RunError("live_ci.py runs only in GitHub Actions; locally use scripts/live_run.py")
    owner = env.get("GITHUB_REPOSITORY_OWNER", "")
    version = env.get("ILMAR_TEST_CLAUDE_VERSION", "")
    if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise RunError("GITHUB_REPOSITORY_OWNER is not set")
    if not VERSION.fullmatch(version):
        raise RunError("ILMAR_TEST_CLAUDE_VERSION must be a pinned x.y.z version")
    temp = env.get("RUNNER_TEMP", "")
    if not temp:
        raise RunError("RUNNER_TEMP is not set")
    return {
        "ILMAR_LIVE_REPO": f"{owner}/ilmar-disposable",
        "ILMAR_LIVE_OUTSIDE_REPO": f"{owner}/ilmar-outside-canary",
        "ILMAR_TEST_CLAUDE_VERSION": version,
        "ILMAR_TEST_CLAUDE_IMAGE": f"localhost/ilmar-claude-code:{version}",
        "ILMAR_TEST_PODMAN_PROXY_IMAGE": PROXY_IMAGE,
        "ILMAR_LIVE_REPORT_DIR": str(Path(temp) / "live-reports"),
        # pytest and its dependencies, only as install_wheels unpacked them.
        "PYTHONPATH": str(Path(temp) / "live-site"),
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",  # no plugin from the runner's system Python
    }


def ci_credentials(env: Mapping[str, str]) -> dict[str, str]:
    missing = [name for name in (GITHUB_TOKEN_ENV, ANTHROPIC_KEY_ENV) if not env.get(name)]
    if missing:
        raise RunError(
            f"secrets not set in the 'live' environment: {', '.join(missing)} "
            "(Settings → Environments → live → Environment secrets)"
        )
    return validate_credentials(env[GITHUB_TOKEN_ENV].strip(), env[ANTHROPIC_KEY_ENV].strip())


def cleanup(env: Mapping[str, str]) -> list[str]:
    """After a failure or cancellation: close and delete what live runs left in the
    disposable repository. Only `ilmar-live/*` branches, `ilmar-live:*` labels,
    their issues and the pull requests into those branches; only in a repository
    the guard accepts (name, privacy, topic)."""
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]  # Ilmar is not installed
    import live_harness as h
    from ilmar.adapters.github.client import urllib_transport

    settings = ci_settings(env)
    repo = settings["ILMAR_LIVE_REPO"]
    github = h.LiveGitHub(h.Secret(GITHUB_TOKEN_ENV, env[GITHUB_TOKEN_ENV]), urllib_transport)
    data = github.ok("GET", f"/repos/{repo}")
    if (
        data.get("name") != h.REPOSITORY_NAME
        or data.get("private") is not True
        or (h.REPOSITORY_TOPIC not in (data.get("topics") or []))
    ):
        raise RunError(f"{repo} is not the disposable repository; nothing was cleaned")
    done: list[str] = []
    refs = github.ok("GET", f"/repos/{repo}/git/matching-refs/heads/ilmar-live/") or []
    labels = github.ok("GET", f"/repos/{repo}/labels?per_page=100") or []
    for label in [x["name"] for x in labels if str(x.get("name", "")).startswith("ilmar-live:")]:
        quoted = urllib.parse.quote(label, safe="")
        issues = github.ok("GET", f"/repos/{repo}/issues?state=open&labels={quoted}") or []
        for issue in issues:
            github.call("PATCH", f"/repos/{repo}/issues/{issue['number']}", {"state": "closed"})
        github.call("DELETE", f"/repos/{repo}/labels/{quoted}")
        done.append(f"label {label} ({len(issues)} issues closed)")
    for ref in refs:
        base = str(ref["ref"]).removeprefix("refs/heads/")
        for request in h.change_requests_for(github, repo, base):
            if request.get("state") == "open":
                github.call("PATCH", f"/repos/{repo}/pulls/{request['number']}",
                            {"state": "closed"})  # fmt: skip
            head = request.get("head", {}).get("ref", "")
            if head.startswith("ilmar/"):
                github.call("DELETE", f"/repos/{repo}/git/refs/heads/{head}")
        github.call("DELETE", f"/repos/{repo}/git/refs/heads/{base}")
        done.append(f"branch {base}")
    return done


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    run: Callable[..., Any] = subprocess.run,
    get: Callable[[str, str], Any] = github_get,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--guard", action="store_true", help="check the context and environment")
    mode.add_argument(
        "--guard-context", action="store_true", help="check the context and the checked-out commit"
    )
    mode.add_argument("--cleanup", action="store_true", help="remove leftovers of live runs")
    mode.add_argument("--prepare", metavar="DIR", help="download the wheels, build the image")
    mode.add_argument("--install-wheels", nargs=2, metavar=("WHEELS", "SITE"),
                      help="unpack the locked wheels after checking their digests")  # fmt: skip
    mode.add_argument("--load-images", metavar="TAR", help="load the prepared images by digest")
    mode.add_argument("--post", metavar="DIR", help="post the records in DIR on #44 and #11")
    args = parser.parse_args(argv)
    source = dict(os.environ if environ is None else environ)
    try:
        if args.install_wheels:  # also run by ci.yml, outside the live workflow
            wheels, site = (Path(p) for p in args.install_wheels)
            for name in install_wheels(wheels, site, LOCK.read_text()):
                print(f"installed {name}")
            return 0
        if args.guard:
            if problems := guard(source, get):
                for problem in problems:
                    print(f"refused: {problem}", file=sys.stderr)
                print("See docs/operations/live-testing.md, 'GitHub settings'.", file=sys.stderr)
                return 1
            print("The context and the 'live' environment are as required.")
            return 0
        if problems := check_context(source):
            raise RunError("; ".join(problems))
        if checked_out_commit(run=run) != source["ILMAR_EXPECTED_SHA"]:
            raise RunError("the checked-out tree is not the expected commit")
        if args.guard_context:
            if problems := check_main_head(source, get):
                raise RunError("; ".join(problems))
            print("Running the expected commit, and main still points at it.")
            return 0
        if args.prepare:
            outputs = prepare(Path(args.prepare), source, run)
            write_outputs(source, outputs)
            print(f"Prepared the wheels and image {outputs['image_id']}.")
            return 0
        if args.load_images:
            load_images(Path(args.load_images), source, run)
            print("Loaded the prepared agent image and the pinned proxy image.")
            return 0
        if args.post:
            records = checked_records(Path(args.post), source["GITHUB_SHA"])
            post_records(run, records)
            print(f"Posted {len(records)} records on #44 and #11.")
            return 0
        if args.cleanup:
            ci_credentials(source)  # shape-checked before use
            for item in cleanup(source):
                print(f"removed {item}")
            return 0
        settings = ci_settings(source)
        check_site(Path(settings["PYTHONPATH"]), LOCK.read_text())
        check_prerequisites(settings, run)
        credentials = ci_credentials(source)
    except RunError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    base = {k: v for k, v in source.items() if k not in credentials}
    env = test_environment(base, settings, credentials)
    secrets = list(credentials.values())
    credentials.clear()
    reports = Path(settings["ILMAR_LIVE_REPORT_DIR"])
    started = run_start(reports)
    code = 1
    try:
        code = run_stages(env, reports, started, run)
    finally:
        env.clear()
        write_summary(reports, {
            "outcome": "passed" if code == 0 else "failed",
            "commit": source["ILMAR_EXPECTED_SHA"],
            "claude_code_version": settings["ILMAR_TEST_CLAUDE_VERSION"],
            "workflow_run": source.get("GITHUB_RUN_ID", ""),
            "at": datetime.now(UTC).isoformat(),
        }, secrets)  # fmt: skip
        secrets.clear()
    if code == 0:
        print(f"{len(new_records(reports, started))} records ready for upload.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
