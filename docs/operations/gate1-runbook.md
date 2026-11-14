# Gate 1 runbook: the first supervised loop (#93)

This runbook takes the maintainer from nothing to a recorded Gate 1. Steps
marked **(you)** need your credentials, accounts or judgement. Everything else
is a command to copy.

**Order matters.** Each step's output is the next step's input, and the
permanent gate record (step 9) comes last, after the evidence is merged.

Rules for every step:

- **Never paste a credential** into an issue, a PR, a chat or a file. Ilmar
  reads credentials only from environment variables set in the terminal that
  runs it.
- **systemd stays off.** Mode 1 never restarts itself.
- **Nothing is merged by Ilmar.** You merge nothing it opens on the disposable
  repository during the trial either; the evidence is that it opened PRs and
  stopped.

## 0. Prerequisites (you)

- A Linux host with rootless Podman, `git`, `gh` (logged in as the
  maintainer) and Python 3.11 or later.
- An Ilmar checkout at `main`, with #132 (trial authorization) and #133 (the
  credential-free pinned check) merged:

      git clone https://github.com/Obtuse8053/Ilmar && cd Ilmar
      python3 -m venv .venv && . .venv/bin/activate && pip install -e .

## 1. The Anthropic workspace (you)

1. In the Anthropic Console, create a **dedicated, non-default workspace** for
   Ilmar with a **monthly spend limit**, for example USD 30.
2. Create **one** API key scoped to that workspace. Keep it in your password
   manager only.
3. Record the limit on #93: the amount and the workspace name. Never record the
   key.
4. In step 5, set `provider_cap_attested_usd` to that limit or lower.

## 2. The provider cap drill (you; before any trial)

Purpose: show that a capped workspace's refusal is classified as
`provider-refused` and suspends Ilmar (`provider-cap`), never retried.

**The exact refusal text has never been observed** (ADR-0029). Ilmar matches
it by pattern (spend, usage, credit, billing and quota limits). This drill is
what establishes it.

1. Create a **scratch** workspace with the smallest monthly limit the Console
   allows, and one key for it. Exhaust the limit, or set it below what was
   already spent, until a request is refused.
2. With the disposable repository and configuration from steps 3 and 5, but
   the scratch key, label one task `ilmar:ready` and run one supervised pass:

       read -rs ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY
       read -rs ILMAR_GATE1_GITHUB_TOKEN && export ILMAR_GATE1_GITHUB_TOKEN
       ilmar run --once --config ~/.config/ilmar/ilmar.gate1.toml; echo "exit $?"
       ilmar budget --config ~/.config/ilmar/ilmar.gate1.toml; echo "exit $?"

3. **Expected:**
   - the run's result is `[provider-refused]`;
   - `ilmar budget` shows `suspended: provider-cap` and exits 78;
   - the task did not move to a retry.
4. **Record on #93:** the result line and `ilmar budget`'s output. The summary
   contains the provider's error text, never the key.
5. **If the result is anything else** (for example `agent-failed` or a
   retry): stop. Open an issue with the result line, so that the classification
   can be fixed before Gate 1.
6. Then clean up:

       ilmar resume --config ~/.config/ilmar/ilmar.gate1.toml --as <you> --provider-cap-resolved
       unset ANTHROPIC_API_KEY

   Delete the scratch key in the Console.

## 3. The disposable repository

1. **Create it (you).** This creates `Obtuse8053/ilmar-disposable` (private,
   topic `ilmar-disposable`) and the private canary, and builds the pinned
   image `localhost/ilmar-claude-code:2.1.318`. It never sees a credential:

       python3 scripts/live_setup.py --claude-version 2.1.318

2. **Seed it** with the trusted template (`deploy/gate1/disposable/`: a tiny
   module and standard-library tests; no workflows, no settings, no secrets):

       tmp=$(mktemp -d) && git clone https://github.com/Obtuse8053/ilmar-disposable "$tmp/d"
       cp -R deploy/gate1/disposable/. "$tmp/d/" && cd "$tmp/d"
       git add -A && git commit -m "Gate 1 template" && git push origin HEAD:main && cd -

3. **Labels:**

       for l in ilmar:ready ilmar:claimed status:in-progress needs:maintainer status:blocked; do
         gh label create "$l" --repo Obtuse8053/ilmar-disposable --force; done

4. **Check it**, before every trial. It reads every object in every ref, the
   paths and the repository settings, and prints the immutable ID:

       git clone --mirror https://github.com/Obtuse8053/ilmar-disposable.git /tmp/d.git
       python3 scripts/gate1_repo_check.py --mirror /tmp/d.git \
         --repository Obtuse8053/ilmar-disposable --report gate1-repo-check.json

   It must exit 0 and report `"passed": true`. There is no option to skip a
   check, and any missing or malformed GitHub answer fails it. It also proves
   the mirror is of this repository as it is now, so make a **fresh** mirror
   each time. Keep `gate1-repo-check.json` for the evidence.

   **It cannot prove the repository holds no secrets.** It finds only the
   credential shapes it knows, and it does not read issue or PR comments.
   Review the repository yourself as well, and say so in the evidence.

5. **Trusted tasks.** Create them as issues, then add `ilmar:ready` only when
   the trial starts:

       gh issue create --repo Obtuse8053/ilmar-disposable \
         --title "Add subtract(a, b) to calc.py" \
         --body "Add a function subtract(a, b) that returns a - b, with a unittest in tests/test_calc.py."
       gh issue create --repo Obtuse8053/ilmar-disposable \
         --title "divide should refuse a zero divisor" \
         --body "calc.divide(a, 0) must raise ValueError('division by zero'). Add a test."
       gh issue create --repo Obtuse8053/ilmar-disposable \
         --title "Document calc.py in the README" \
         --body "List each function in calc.py in README.md with one example each."

## 4. Pinned-version evidence (2.1.318)

1. **Sealed settings (no credential):** in GitHub Actions, run *Gateway
   compatibility (manual, no secrets)* on `main` with
   `claude_version = 2.1.318`. Download `gateway-compat-records`. Review
   `issue-90-pinned-sealed-run.json`: the outcome is `provider-refused` and no
   repository `apiKeyHelper` ran.
2. **The passed live run and the recorded stream fixture (you, the step 1
   key).** `ilmar gate record --live-test` takes a GitHub Actions run URL, so
   use the *Live tests (manual)* workflow (`live.yml`) on `main`, with
   `claude_version = 2.1.318`. Its one-time setup (the protected `live`
   environment, its two secrets and a required reviewer) is in
   `docs/operations/live-testing.md`. The run's artifacts include
   `issue-13-stream-event-types.json` (`provenance: recorded`,
   `claude_code_version: 2.1.318`). Review it. Then add it in a PR as
   `tests/fixtures/claude_code_streams/2.1.318-recorded.json`, and merge it
   after CI passes. Keep the run's URL for step 9.

   `python3 scripts/live_run.py` runs the same tests on your own machine, but
   it gives no Actions run URL to record.
3. **SIGKILL test (#90):** already passed in CI on real Podman (see #93).
   Review it.

## 5. The Gate 1 configuration

    mkdir -p ~/.config/ilmar && cp deploy/gate1/ilmar.gate1.toml ~/.config/ilmar/
    # edit: [autonomy] repository_id = <the ID from step 3.4>
    #       provider_cap_attested_usd = <at most the step 1 limit>
    ilmar config validate ~/.config/ilmar/ilmar.gate1.toml --check-environment

The GitHub token is fine-grained and expires within 7 days, with access to
**only** `ilmar-disposable`:

- contents: read and write;
- pull requests: read and write;
- issues: read and write;
- no workflow permission.

`live_setup.py` prints a pre-filled link to create it.

## 6. Authorize the trial (you)

Run the repository check (3.4) again, then:

    ilmar gate trial --config ~/.config/ilmar/ilmar.gate1.toml --as <you> \
      --repository-id <ID> --repository Obtuse8053/ilmar-disposable \
      --agent-version 2.1.318 --max-executions 3 --max-spend-usd 6 --hours 8 \
      --purpose https://github.com/Obtuse8053/Ilmar/issues/93 --disposable
    ilmar gate show --config ~/.config/ilmar/ilmar.gate1.toml

The trial is not a gate record. It authorizes only `ilmar run --loop --trial`,
on this repository, within these limits, until it expires.

## 7. The supervised run (you, watching)

Add `ilmar:ready` to the three issues. Then, in one terminal:

    read -rs ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY
    read -rs ILMAR_GATE1_GITHUB_TOKEN && export ILMAR_GATE1_GITHUB_TOKEN
    ilmar run --loop --trial --config ~/.config/ilmar/ilmar.gate1.toml; echo "exit $?"

Watch the output, the disposable repository's PRs and issues, and, in a
second terminal:

    ilmar budget --config ~/.config/ilmar/ilmar.gate1.toml

**Expected:**

- tasks are claimed one at a time;
- PRs open for verified candidates, and none is merged by Ilmar;
- the loop stops with exit 3 when the trial's executions or spending are used
  up, or when it expires.

## 8. Kill switch and revocation drills (you, during step 7)

1. **Kill switch.** While an agent runs:

       touch /var/tmp/ilmar-gate1-KILL

   **Expected:** the agent is stopped, the attempt goes to retry-wait
   (`suspension-interrupted`), the loop exits 78, and `ilmar budget` shows
   `kill-switch`. Then:

       rm /var/tmp/ilmar-gate1-KILL
       ilmar resume --config ~/.config/ilmar/ilmar.gate1.toml --as <you>

   Restart the loop.
2. **Revocation.**

       ilmar gate end-trial --config ~/.config/ilmar/ilmar.gate1.toml --as <you>

   **Expected:** the loop exits 3 at its next check.
3. **Configuration change.** Edit the configuration while the loop waits
   between passes. **Expected:** exit 3 before another pass.

## 9. Evidence and the gate record (you)

1. Collect, with no credentials:
   - `ilmar gate show`, `ilmar budget`;
   - `ilmar events --config … --format text`;
   - `ilmar events verify --config …` (exit 0);
   - the loop's terminal output;
   - the disposable repository's PR list (`gh pr list --state all`), which
     shows nothing merged by Ilmar;
   - `gate1-repo-check.json`;
   - the records from steps 2 and 4.
2. Commit them in a documentation PR to Ilmar (for example
   `docs/evidence/gate1/`) and merge it. Note its merge commit SHA.
3. Verify by hand, against that commit: the live run passed, every #93
   criterion holds, and the fixture is the reviewed one. Ilmar does not check
   these (ADR-0040).
4. Record the gate:

       ilmar gate record --config ~/.config/ilmar/ilmar.gate1.toml --as <you> \
         --mode supervised --repository-id <ID> --repository Obtuse8053/ilmar-disposable \
         --agent-version 2.1.318 --evidence https://github.com/Obtuse8053/Ilmar/pull/<n> \
         --evidence-commit <merge SHA> \
         --live-test https://github.com/Obtuse8053/Ilmar/actions/runs/<run> \
         --fixture tests/fixtures/claude_code_streams/2.1.318-recorded.json \
         --gate1 https://github.com/Obtuse8053/Ilmar/issues/93

## If anything is unexpected

Stop and suspend:

    ilmar suspend --config ~/.config/ilmar/ilmar.gate1.toml --as <you> --note "<why>"

Then keep the output and open an issue. Never edit the store by hand.

**A quarantine** stops the loop with exit 2 (#59, ADR-0042; a Gate 1
dependency, #135 and #136):

1. List quarantined resources:

       ilmar quarantine list --config ~/.config/ilmar/ilmar.gate1.toml

2. Inspect what remains with `podman ps -a` and `podman network ls`. Remove
   only this sandbox's own parts by hand; never anything with another owner.
3. Record the resolution:

       ilmar quarantine resolve --config ~/.config/ilmar/ilmar.gate1.toml \
         --execution <e> --resource <key> --as <you> --action removed-by-hand

   It refuses while any part remains. The next pass finishes the execution,
   and the event goes into the evidence.
