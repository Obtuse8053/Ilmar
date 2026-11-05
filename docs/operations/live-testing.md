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
- **Restricted credentials, checked before anything is written:**
  - the token is fine-grained (`github_pat_…`) and expires within 7 days;
  - it cannot see a private repository outside the disposable one;
  - it has no Administration and no Actions permission;
  - it cannot write workflow files. This is probed with one attempted write
    to a throwaway branch, deleted right away.
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
- **The agent reaches only the model API:**
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
`ILMAR_LIVE_CRASH_DELAY=<seconds>` (default 8).

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
