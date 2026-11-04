# Development workflow

How Ilmar development is planned, tracked and resumed. It is written so that a
new session, human or agent, can continue from the repository and GitHub alone
(ADR-0009). Later, Ilmar will run this same workflow itself through its GitHub
adapters (issue #20).

## Sources of truth

Each fact has exactly one owner. Other places may *link* to it, but must not
restate it.

| Fact | Owner |
| --- | --- |
| What Ilmar must be | `MANIFEST.md` |
| Rules for changes | `AGENTS.md` |
| Issue state: open/closed, priority, labels, milestone, who is working on it | **GitHub issues** |
| Work in progress | Open PRs and `status:in-progress` issues on **GitHub** |
| Planned order and dependencies of the current milestone, recommended next task | `docs/STATUS.md` |
| Which milestone is current | `docs/STATUS.md` (GitHub milestones show open or closed) |
| Milestone scope | `MANIFEST.md` §10 and each milestone's description on GitHub |
| Why something is built this way | `docs/architecture/decisions/` |
| Evidence, and which finding led to which issue | `docs/research/findings.md`. `docs/roadmap.md` lists the same links, and a test keeps the two consistent with each other |
| History of individual changes | Git history and merged PRs on GitHub |

Issue numbers are stable IDs and may be cited anywhere. Issue *state* may not.

**Limits.** The documentation tests run offline. They check that the documents
agree with *each other* and that `STATUS.md` does not copy issue state. They
cannot see GitHub. Agreement with live issue state comes from the session
protocol: at the start of each session, compare the plan with GitHub, and fix
`STATUS.md` if they differ. Proposal #35 would automate that check.

## Labels and milestones

- **Milestones** `M1`–`M6` follow MANIFEST §10. Maintenance issues have no
  milestone.
- **Priority:** `priority:p0` means do next and blocks the milestone;
  `priority:p1` is important; `priority:p2` can wait.
- **Type:** `type:feature`, `type:epic` (split into issues when its milestone
  starts), `type:docs`, `type:chore`, `type:security`.
- **Process:**
  - `proposal`: awaiting the maintainer's decision.
  - `approved`: the maintainer approved it.
  - `research`: a review or investigation.
  - `sensitive`: touches an AGENTS.md §6 area and needs explicit human approval.
- **Status:** `status:in-progress` (a session has claimed it),
  `status:blocked`, and `needs:maintainer` (waiting for a maintainer action or
  decision).
- `ilmar:ready`: the issue is eligible for Ilmar itself to execute. Not used
  before M4.

## Choosing the next task

1. Take the first step of **Planned order** in `docs/STATUS.md` whose issue is
   open on GitHub, is not `status:in-progress`, `status:blocked` or
   `needs:maintainer`, and whose dependencies are closed.
2. If no step qualifies, take an open `approved` maintenance issue (no
   milestone), highest priority first.
3. Only `approved` issues are implemented. A `proposal` waits for the
   maintainer.
4. If the maintainer has not yet approved the recommended next task, ask before
   starting it.

## Session protocol

**Start**

1. `git fetch origin main:refs/remotes/origin/main` and read `docs/STATUS.md`.
   Check its Planned order against live issue state on GitHub. If they differ,
   GitHub is right; correct `STATUS.md` in your next PR.
2. List open PRs and in-progress issues:
   `gh api "repos/Obtuse8053/Ilmar/pulls?state=open"` and
   `gh api "repos/Obtuse8053/Ilmar/issues?labels=status:in-progress"`.
   Address review feedback on open PRs before starting new work.
3. Read the most recent handoff comment on the issue you are resuming.
4. Claim the issue by adding the `status:in-progress` label.
5. Branch from `main`: `m<N>/<issue>-<topic>`, for example `m2/6-github-tasks`.
   Maintenance work uses `chore/<issue>-<topic>`.

**Work.** Follow AGENTS.md: acceptance criteria come from the issue; make the
smallest useful increment; add tests; write an ADR for any decision; record
sources and findings for research.

**Finish**

1. Edit `docs/STATUS.md` **only** if the current milestone, the planned order
   or the recommended next task changed, and then add a dated change-log entry.
   Routine PRs leave it alone; their history is the git log. This keeps
   parallel PRs from conflicting there. Never record issue state in it. Update
   finding statuses and `docs/roadmap.md` together if they changed.
2. Open the PR with the template. The body says `Closes #<issue>`. Name the
   sensitive areas it touches.
3. **Never merge on your own initiative.** Merge only when the maintainer
   explicitly asks for that specific PR to be merged. Then:
   1. If the PR introduces ADRs, push one commit that changes only their
      status from `Proposed` to `Accepted` (the maintainer's approval is the
      acceptance).
   2. Wait until CI passes on the PR's current head commit and GitHub reports
      the PR as mergeable.
   3. Merge with a merge commit, pinned to that head:
      `gh api -X PUT repos/Obtuse8053/Ilmar/pulls/<n>/merge -f merge_method=merge -f sha=<head-sha>`.
      GitHub refuses the merge if the head moved, so the merged code is exactly
      what CI verified.
   4. If anything other than the status flip changed after approval, ask the
      maintainer again.
4. If the session ends before the work is done, push the branch and leave a
   handoff comment on the issue. Say what is done, what is next, and anything
   surprising.

## Milestone close-out

When the last issue of a milestone is merged:

1. Close the GitHub milestone and update `docs/roadmap.md`.
2. Split the next milestone's epics into issues. Open them with the
   `proposal` label for maintainer approval.
3. Replace **Planned order** in `docs/STATUS.md` with the next milestone's plan
   and record the recommended next task. Then stop and wait for the
   maintainer's approval.

## Notes for Claude Code cloud sessions

These were observed on 2026-11-01. Verify them again if behaviour differs.

- GitHub GraphQL is blocked, so `gh pr create` and `gh issue create` fail. Use
  the REST API through `gh api`, for example
  `gh api repos/Obtuse8053/Ilmar/pulls --input pr.json`.
- The session's clone is shallow and may contain only one branch. Fetch other
  branches explicitly.
- PyPI may be unreachable. pytest, ruff and mypy are preinstalled; mypy may
  need `--python-executable` to see pytest.
- Direct web access to github.com, openai.com and anthropic.com may be
  blocked. Record the actual access level in `docs/research/sources.md`.
