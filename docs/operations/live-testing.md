# Live testing: the #44 smoke test and the #11d end-to-end run

These tests use real services: GitHub, rootless Podman and Claude Code with a
real model key. They are **opt-in**: they never run in CI and they skip unless
every variable below is set. A maintainer runs them locally, attended.
**Unattended operation stays disabled.** `ilmar run` refuses to start without
`--once` until #44 has passed and #20 is designed and approved.

Order: build the images, run **#44** and record it, then run the **live
end-to-end tests** (#11d).

## What the tests guarantee

- **Only the disposable repository.**
  - The guard refuses to run unless the target is `<owner>/ilmar-disposable`,
    private, with the `ilmar-disposable` topic.
  - Each run creates its own base branch (`ilmar-live/<run>`), label and
    issue. Nothing existing is rewritten or force-pushed.
  - Everything a run made is closed or deleted afterwards. To keep it for
    inspection, set `ILMAR_LIVE_KEEP=1`.
- **Restricted credentials, checked before anything is written.** Too
  much permission and too little are both refused, and an unclear answer
  is never taken as "not granted". The token must:
  - be fine-grained (`github_pat_…`) and expire within 7 days;
  - be unable to see the private canary repository;
  - have no Administration, Actions, Secrets or Webhooks permission
    (each probe must get 403 or 404; anything else stops the run);
  - be able to read issues and pull requests;
  - on a throwaway branch, deleted afterwards: write a harmless file and
    open (then close) a pull request, and have a workflow-file write
    refused. A refusal counts only after the harmless write succeeded;
  - have Issues write: create a probe issue, create a label, add it (and
    GitHub must keep it) and comment, as seeding a run and claiming its
    task do. The probe issue is closed and its label deleted afterwards.
    Creating the issue comes first, because Pull requests write alone also
    allows the label calls.
- **Credentials never leak:**
  - Credentials are read only from the environment, wrapped so that they
    never print, and handed to the CLI only through its environment.
  - The tests fail if either credential appears, raw or encoded, in:
    - the state directory (database, coordinator repository, workspaces);
    - the generated configuration;
    - the CLI output, the evidence, or the frozen candidate's files;
    - the pull request, its commits, or the issue and its comments;
    - any process's command line during the run.
  - They also fail if the GitHub token is in any container's definition, or
    the model key in any container other than the agent's.
  - They also fail if any process other than the coordinator and git holds
    the GitHub token in its environment.
  - 20-character fragments of each credential count as leaks too. Tools
    record keys that way: Claude Code keeps the last 20 characters of
    approved keys.
- **Nothing is left behind, even on failure.** After every run, passed or
  failed:
  - every container and network of the run's instance is removed, so no
    sandbox keeps a key in memory;
  - any file found holding a credential is deleted and the test fails;
  - the GitHub branch, label, issue and pull request are cleaned up.

  After a pass, no process may still refer to the run's containers (for
  example an orphaned `podman exec`).
- **The agent reaches only the model API:**
  - the agent's sandbox is attached to exactly one network, which is
    internal;
  - the egress proxy allows exactly `api.anthropic.com`;
  - its log shows only that host, with no denials;
  - verification sandboxes have no network at all.
- **Verified is published:** the pull request's head equals the branch head,
  the frozen candidate and the evidence's candidate. Its files are exactly
  the verified change.
- **Completion:** the test merges the pull request into the run's base
  branch. The next `ilmar run --once` reports the task complete, removes the
  claim label and comments on the issue.
- **Crash drill:**
  - the coordinator is killed with `SIGKILL` while Claude Code works;
  - the next pass removes the dead run's sandbox, sidecar and network;
  - it starts attempt 2 in a new sandbox and publishes exactly one pull
    request.

Each passing test writes a JSON record, without credentials, to
`$ILMAR_LIVE_REPORT_DIR`.

## Quick start: two commands (your own Linux machine)

Linux with rootless Podman, Python 3.11+, git, and the `gh` CLI logged in as
you (`gh auth login`). From a checkout of this repository:

```sh
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

python3 scripts/live_setup.py --claude-version <x.y.z>   # once
python3 scripts/live_run.py --post                       # each time
```

**`live_setup.py`** never sees a credential. It:
- checks the host;
- creates `<you>/ilmar-disposable` (private, with the topic) and the private
  canary `<you>/ilmar-outside-canary` if missing. It refuses, and never
  changes, an existing public one;
- pulls the proxy image and builds the pinned Claude Code image;
- writes the non-secret settings to `~/.config/ilmar-live/settings.env`
  (mode 0600);
- prints a **pre-filled link** for the GitHub token and the steps for the
  Anthropic key. In the token form, the only thing to choose is
  *Repository access → Only select repositories → `ilmar-disposable`*;
  GitHub cannot pre-fill that.

**`live_run.py`**:
- first checks that Podman is rootless and both images exist, so nothing is
  asked for, or touched, if the host isn't ready;
- must be run in an interactive terminal. It refuses to read credentials
  from a pipe or a file, where `getpass` would echo them;
- asks for the two credentials with hidden input. They exist only in its
  memory and in the environment of the test process: never in argv, a file,
  the shell history or the output;
- runs #44, then, only if #44 passed, the live end-to-end tests. A stage
  passes only if its tests passed *and* wrote their records, so a skipped
  test never counts as a pass;
- with `--post`, posts the credential-free records on #44 and #11 using
  your `gh` login;
- ignores `PYTEST_ADDOPTS` and the project's pytest options, so nothing can
  add `--showlocals`.

Afterwards, revoke the token and the key, or let them expire.

The rest of this page explains each step, for review or a manual run.

## Or: run it in GitHub Actions (no Linux machine needed)

The manual workflow `.github/workflows/live.yml` runs the same stages on a
GitHub-hosted Ubuntu runner, with rootless Podman. You don't need a Linux
machine, Podman or a local build, and you never type a credential anywhere
but GitHub's secret form.

**One-time setup:** in a browser, or with `gh` on any OS.

1. **Create the two private repositories:**
   ```sh
   gh repo create ilmar-disposable --private --add-readme
   gh repo edit "$(gh api user --jq .login)/ilmar-disposable" --add-topic ilmar-disposable
   gh repo create ilmar-outside-canary --private --add-readme
   ```
   They must belong to the same owner as Ilmar's repository (the workflow
   derives their names from it).
2. **Create the GitHub token and the Anthropic key** as in steps 2 and 3
   below. The token is fine-grained, only for `ilmar-disposable`, expires
   within 7 days, and has Contents, Issues and Pull requests read/write.
   The key is in a spend-capped `ilmar-live` workspace.
3. **Configure the GitHub settings below.** The workflow's `guard` job reads
   them, without any secret, and **refuses to run** unless they are exactly
   so.

### GitHub settings (checked by the guard, fail closed)

In `Obtuse8053/Ilmar` → **Settings**:

1. **Rules → Rulesets → New ruleset → New branch ruleset**:
   - **Name:** `main`
   - **Enforcement status:** Active
   - **Bypass list:** empty
   - **Target branches:** *Add target → Include default branch*
   - **Rules:** tick **Restrict deletions**, **Block force pushes** and
     **Require a pull request before merging** (0 required approvals is
     enough for a single maintainer)
   - **Create**

   Why: a workflow changed directly on `main` would receive the secrets
   legitimately. With this, `main` changes only through pull requests,
   including changes by automation such as Claude sessions.

   With 0 required approvals, anyone who can merge (including automation
   acting as you) can still merge their own pull request. The approval in
   step 2 is what keeps a run from starting without a person who did not
   start it.
2. **Environments → New environment** named `live`:
   - **Required reviewers:** add a GitHub account that **no automation can
     use**: not the account Claude (or any other agent, bot or CI token)
     acts as. If Claude's GitHub connection acts as your main account,
     that means a second account of your own. Give that account only the
     repository access it needs to approve.
   - **Prevent self-review:** **ticked**. The guard refuses to run without
     it. The account that starts a run then cannot approve it, so a
     dispatch by automation acting as you still needs the reviewer account.
   - **Allow administrators to bypass configured protection rules:**
     **unticked**.
   - **Deployment branches and tags:** *Selected branches and tags* → *Add
     deployment branch or tag rule* → Ref type **Branch**, pattern **`main`**.
     That must be the only rule.
   - **Environment secrets:** `ILMAR_LIVE_GITHUB_TOKEN` (the token) and
     `ANTHROPIC_API_KEY` (the key). Add no environment variables.
3. **Actions → General:**
   - keep **Workflow permissions** at *Read repository contents and
     packages permissions*;
   - leave **Allow GitHub Actions to create and approve pull requests**
     **unticked**.

   The workflow sets its own minimal permissions either way.

What the guard cannot see, so you must keep it true yourself:
- **Who can approve:** only people you trust are required reviewers, and
  no reviewer account is reachable by Claude or other automation (no
  connected app, token, SSH key or saved session for it in an agent's
  environment).
- **Administration and approval are separate.** An account with
  administration access to this repository can change rulesets and
  environments, merge, and dispatch. It must not also be able to approve
  its own live deployment. Keep "Prevent self-review" on, and keep the
  reviewer an account other than the one that holds administration (and
  that automation acts as). If an administrator account is also a
  reviewer, it can approve runs started by someone else, so it must be one
  no automation can use.
- **No other workflow** uses `environment: live`. Changing one would need a
  pull request into `main`.
- **No repository-wide or organisation-wide secrets** hold these credentials.
  Only the `live` environment does.

**Each run.** Review what is on `main`, then pin that exact commit:

```sh
SHA=$(gh api repos/Obtuse8053/Ilmar/commits/main --jq .sha)   # the commit you reviewed
gh workflow run live.yml --repo Obtuse8053/Ilmar --ref main \
  -f expected_sha="$SHA" -f claude_version=<x.y.z> -f post_records=true
```

Then:
1. Wait for `guard` and `prepare` to pass, and approve the pending `live`
   deployment **in the browser, signed in as the reviewer account**. Do not merge anything into `main` in between: the `live`
   job checks again that `main` still points at `expected_sha`, and refuses
   if not. Then dispatch it again with the new commit.
2. `prepare` (no secrets) downloads the hash-locked wheels
   (`requirements/live.txt`) and builds the Claude Code image (never
   published) from its digest-pinned base. `live` checks out exactly
   `expected_sha`, checks the commit, the wheels' digests and the image's
   digest before any secret, and runs #44 and then the live tests. Nothing
   is installed or downloaded after that.
3. After a failure or cancellation, it removes `ilmar-live/*` leftovers from
   `ilmar-disposable` and every container.
4. It uploads only credential-free records (artifact `live-records`, kept 7
   days), plus `run-summary.json`, which binds the outcome to the commit.
5. With `post_records`, a separate job without any secret posts the records
   on #44 and #11, but only if the run passed, at that commit, and every
   record passes its checks.

**If a run says GitHub did not label or list the seeded issue,** the
coordinator would have found no task. The harness creates the run's label,
adds it to the issue through endpoints that fail loudly, and waits up to
60 s for the issue to appear in the coordinator's own listing. A refused
label means the token lacks Issues write. A listing that never shows the
issue is GitHub's side; dispatch again later.

**Do not re-run with "Enable debug logging"** unless you need it. Masked
values stay masked, but debug output also prints request details and
signed URLs into the run's log, and this repository's logs are public.

**Afterwards:** delete the two environment secrets, and revoke the token and
the key (or let them expire).

**Changing a pin** happens only in a reviewed pull request:
- the agent image's base: the digest in `images/claude-code/Containerfile`;
- the proxy image: `PROXY_IMAGE` in `scripts/live_ci.py`;
- the wheels: a version and its sha256 in `requirements/live.txt`. Take
  the sha256 from PyPI's file page; CI's `pip download --require-hashes`
  fails closed and prints the digest it got.

**Not pinned by digest** (built in `prepare`, with no secrets, and then
fixed by the archive's digest):
- the Debian packages the Containerfile installs;
- Claude Code's npm dependencies below the version you name.

They run with the Anthropic key inside the sandbox, as the agent always
does.

## One-time setup

You need:
- Linux with rootless Podman (as in CI);
- Python 3.11 or newer, git, and the `gh` CLI (optional, for the
  repository steps);
- a checkout of this repository.

### 1. The disposable repository and a private "outside" repository

`Obtuse8053/Ilmar` is public, so it cannot prove that the token is limited to
one repository. Create a small private canary that the token must **not**
reach:

```sh
gh repo create ilmar-disposable --private --add-readme \
  --description "Disposable repository for Ilmar's live tests; safe to delete"
gh repo edit "$(gh api user --jq .login)/ilmar-disposable" --add-topic ilmar-disposable
gh repo create ilmar-outside-canary --private --add-readme \
  --description "Must not be reachable by Ilmar's live-test token"
```

### 2. The GitHub token (fine-grained, 7 days at most)

On GitHub, go to **Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token** and set:

- **Token name:** `ilmar-live`
- **Expiration:** custom, at most 7 days from today
- **Resource owner:** your account
- **Repository access:** *Only select repositories* → `ilmar-disposable`
- **Repository permissions:**
  - **Contents:** Read and write
  - **Issues:** Read and write
  - **Pull requests:** Read and write
  - **Metadata:** Read-only (added automatically)
  - everything else **No access**, in particular **Workflows**, **Actions**,
    **Administration**, **Secrets**, **Environments** and **Webhooks**
- **Account permissions:** none

Copy the token once. Do not save it in a file, and do not paste it into a
chat.

### 3. The Anthropic key (dedicated, spend-capped)

In the Anthropic Console:
1. Create a workspace named `ilmar-live`.
2. Set a low monthly **spend limit** on it, for example USD 5. A full run
   needs four short Claude Code sessions: one for #44, one end to end, and
   two for the crash drill.
3. Create an API key in that workspace.

### 4. Images

Pick a Claude Code version and keep it pinned (you will record it on #44):

```sh
cd /path/to/Ilmar
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

export ILMAR_TEST_CLAUDE_VERSION=<x.y.z>          # the Claude Code version to pin
podman pull docker.io/library/python:3.12-alpine  # egress proxy and verification
podman build --build-arg CLAUDE_CODE_VERSION="$ILMAR_TEST_CLAUDE_VERSION" \
  -t "localhost/ilmar-claude-code:$ILMAR_TEST_CLAUDE_VERSION" images/claude-code
```

Build the Claude Code image locally under Anthropic's terms, and never
publish it.

## Each session: set the environment

Use a **new terminal** and close it when you are done. `read -rs` keeps the
values off the screen and out of the shell history:

```sh
cd /path/to/Ilmar && . .venv/bin/activate
read -rs -p "GitHub token: " ILMAR_LIVE_GITHUB_TOKEN && echo && export ILMAR_LIVE_GITHUB_TOKEN
read -rs -p "Anthropic key: " ANTHROPIC_API_KEY && echo && export ANTHROPIC_API_KEY

export ILMAR_LIVE_REPO="$(gh api user --jq .login)/ilmar-disposable"
export ILMAR_LIVE_OUTSIDE_REPO="$(gh api user --jq .login)/ilmar-outside-canary"
export ILMAR_TEST_CLAUDE_VERSION=<x.y.z>
export ILMAR_TEST_CLAUDE_IMAGE="localhost/ilmar-claude-code:$ILMAR_TEST_CLAUDE_VERSION"
export ILMAR_TEST_PODMAN_PROXY_IMAGE=docker.io/library/python:3.12-alpine
export ILMAR_LIVE_REPORT_DIR="$HOME/ilmar-live-reports"
export ILMAR_LIVE_REQUIRED=1     # fail, rather than skip, if anything is missing
```

Do not pass `-l` / `--showlocals` to pytest in these sessions.

## Run #44: the real Claude Code smoke test

```sh
pytest -m integration -v -rs --tb=short tests/integration/test_claude_code_live.py
```

It must pass with no `DENY` in the proxy log and only `api.anthropic.com`
contacted. Then post the record, which contains no credentials, on #44:

```sh
gh issue comment 44 --repo Obtuse8053/Ilmar --body "$(printf '#44 smoke test passed.\n\n```json\n%s\n```\n' "$(cat "$ILMAR_LIVE_REPORT_DIR/issue-44-smoke-test.json")")"
```

## Run the live end-to-end tests (#11d)

Check the guard first. It touches nothing except one refused workflow write
on a throwaway branch:

```sh
pytest -m integration -v -rs --tb=short tests/integration/test_live_end_to_end.py -k preflight
```

Then run everything: the preflight, the end-to-end run, and the crash drill.
This takes about 5 to 20 minutes:

```sh
pytest -m integration -v -rs --tb=short tests/integration/test_live_end_to_end.py
```

To give Claude Code more time before the kill, set
`ILMAR_LIVE_CRASH_DELAY=<seconds>`. The drill kills the coordinator this many seconds
(default 2) after Claude Code first reaches the model, while the agent run is still in
progress.

Post the records on #11:

```sh
for f in "$ILMAR_LIVE_REPORT_DIR"/preflight.json "$ILMAR_LIVE_REPORT_DIR"/end-to-end-*.json \
         "$ILMAR_LIVE_REPORT_DIR"/crash-drill-*.json; do
  gh issue comment 11 --repo Obtuse8053/Ilmar \
    --body "$(printf 'Live test record `%s`:\n\n```json\n%s\n```\n' "$(basename "$f")" "$(cat "$f")")"
done
```

#11 is closed only after these records are on the issue. A later pull
request also records them in the research access log ([R13]).

## Afterwards

- Close the terminal, so the variables are gone.
- Delete the `ilmar-live` token on GitHub, and the key in the Anthropic
  Console (or let them expire).
- If a run failed and left things behind, its branch is `ilmar-live/<run>`
  and its label `ilmar-live:<run>` in `ilmar-disposable`. The repository
  can be deleted at any time.

## If something fails

- **The guard refuses:** the message names the precondition, never a value.
  Fix the repository or token settings and rerun.
- **A failed assertion:** the message shows redacted CLI output. Rerun
  with `ILMAR_LIVE_KEEP=1` to inspect the branch, issue and pull request.
  pytest's temporary directory (printed with `-rs`) holds the state
  directory; it contains no credentials, and the test checks this.
- **Leftover containers:** list them with
  `podman ps -a --filter label=io.ilmar.managed=true` and remove them with
  `podman rm -f`.
