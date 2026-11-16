# Gate 1: the operator's commands, in order

The commands only. Why each step exists, what to expect and what to do when
something is unexpected are in `gate1-runbook.md` (§ numbers below). Never
paste a credential into an issue, a file or a command line: `read -rs` keeps
it out of your shell history.

```sh
CFG=~/.config/ilmar/ilmar.gate1.toml
ME=<your short lower-case name>
```

## Done already (credential-free, on `main`)

- [x] Sealed settings and gateway compatibility with Claude Code 2.1.318,
      run 43648244399 (recorded on #93).
- [x] SIGKILL test on real Podman (#90).

## 1. Workspace `ilmar-test` (§1, Console)

- [ ] `ilmar-test`: a USD 10 monthly limit, one key in your password manager.
      Post the name and limit on #93, never the key.

## 2. Cap drill (§2; a scratch workspace and its own key)

Needs steps 3 and 5 first, but with the scratch key.

```sh
read -rs ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY             # SCRATCH key
read -rs ILMAR_GATE1_GITHUB_TOKEN && export ILMAR_GATE1_GITHUB_TOKEN
gh issue edit <n> --repo Obtuse8053/ilmar-disposable --add-label ilmar:ready
ilmar run --once --config $CFG; echo "exit $?"   # expect [provider-refused]
ilmar budget --config $CFG; echo "exit $?"       # expect provider-cap, exit 78
ilmar resume --config $CFG --as $ME --provider-cap-resolved
gh issue edit <n> --repo Obtuse8053/ilmar-disposable --remove-label ilmar:ready
unset ANTHROPIC_API_KEY                          # then delete the scratch key
```

- [ ] Post the result line and the `ilmar budget` output on #93.

## 3. Disposable repository (§3)

```sh
python3 scripts/live_setup.py --claude-version 2.1.318
tmp=$(mktemp -d) && git clone https://github.com/Obtuse8053/ilmar-disposable "$tmp/d"
cp -R deploy/gate1/disposable/. "$tmp/d/" && (cd "$tmp/d" && git add -A \
  && git commit -m "Gate 1 template" && git push origin HEAD:main)
for l in ilmar:ready ilmar:claimed status:in-progress needs:maintainer status:blocked; do
  gh label create "$l" --repo Obtuse8053/ilmar-disposable --force; done
rm -rf /tmp/d.git && git clone --mirror https://github.com/Obtuse8053/ilmar-disposable.git /tmp/d.git
python3 scripts/gate1_repo_check.py --mirror /tmp/d.git \
  --repository Obtuse8053/ilmar-disposable --report gate1-repo-check.json; echo "exit $?"
```

- [ ] Exit 0, `"passed": true`; note the printed repository ID; your own review.
- [ ] Create the three trusted issues (runbook §3.5), without `ilmar:ready`.

## 4. Live run and fixture (§4.2; the `ilmar-test` key, in the `live` environment)

```sh
gh workflow run live.yml --repo Obtuse8053/Ilmar --ref main \
  -f expected_sha="$(git rev-parse origin/main)" -f claude_version=2.1.318
```

- [ ] The run passes; keep its URL.
- [ ] Review `issue-13-stream-event-types.json`; add it as
      `tests/fixtures/claude_code_streams/2.1.318-recorded.json` in a PR; merge
      after CI.

## 5. Configuration and token (§5)

The 7-day fine-grained token gets access to `ilmar-disposable` only: contents,
pull requests and issues read/write; no workflow permission.

```sh
mkdir -p ~/.config/ilmar && cp deploy/gate1/ilmar.gate1.toml $CFG
$EDITOR $CFG            # [autonomy] repository_id = <ID from step 3>
read -rs ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY             # ilmar-test key
read -rs ILMAR_GATE1_GITHUB_TOKEN && export ILMAR_GATE1_GITHUB_TOKEN
ilmar config validate $CFG --check-environment; echo "exit $?"     # expect 0
```

## 6. Trial (§6)

```sh
rm -rf /tmp/d.git && git clone --mirror https://github.com/Obtuse8053/ilmar-disposable.git /tmp/d.git
python3 scripts/gate1_repo_check.py --mirror /tmp/d.git \
  --repository Obtuse8053/ilmar-disposable --report gate1-repo-check.json; echo "exit $?"
ilmar gate trial --config $CFG --as $ME --repository-id <ID> \
  --repository Obtuse8053/ilmar-disposable --agent-version 2.1.318 \
  --max-executions 3 --max-spend-usd 5 --hours 8 \
  --purpose https://github.com/Obtuse8053/Ilmar/issues/93 --disposable
ilmar gate show --config $CFG
```

## 7. Supervised run (§7; you watch)

```sh
for n in <the three issue numbers>; do
  gh issue edit $n --repo Obtuse8053/ilmar-disposable --add-label ilmar:ready; done
ilmar run --loop --trial --config $CFG; echo "exit $?"
ilmar budget --config $CFG                        # in a second terminal
```

- [ ] PRs open; nothing is merged by Ilmar; the loop ends with exit 3.

## 8. Safety drills (§8, during step 7)

```sh
touch /var/tmp/ilmar-gate1-KILL    # while an agent runs: exit 78, kill-switch
rm /var/tmp/ilmar-gate1-KILL && ilmar resume --config $CFG --as $ME
ilmar run --loop --trial --config $CFG; echo "exit $?"   # restart the loop
ilmar gate end-trial --config $CFG --as $ME             # revocation: exit 3
# configuration change: edit $CFG while the loop waits between passes: exit 3
```

## 9. Evidence and gate record (§9)

```sh
ilmar gate show --config $CFG; ilmar budget --config $CFG
ilmar events --config $CFG --format text > events.txt
ilmar events verify --config $CFG; echo "exit $?"        # expect 0
gh pr list --repo Obtuse8053/ilmar-disposable --state all
```

- [ ] Evidence PR to Ilmar (`docs/evidence/gate1/`), merged; note its merge SHA.
- [ ] Verify every #93 criterion by hand at that SHA. Then, and only then:

```sh
ilmar gate record --config $CFG --as $ME --mode supervised \
  --repository-id <ID> --repository Obtuse8053/ilmar-disposable \
  --agent-version 2.1.318 --evidence https://github.com/Obtuse8053/Ilmar/pull/<n> \
  --evidence-commit <merge SHA> \
  --live-test https://github.com/Obtuse8053/Ilmar/actions/runs/<run> \
  --fixture tests/fixtures/claude_code_streams/2.1.318-recorded.json \
  --gate1 https://github.com/Obtuse8053/Ilmar/issues/93
```

If anything is unexpected: `ilmar suspend --config $CFG --as $ME --note "<why>"`,
keep the output, and open an issue.
