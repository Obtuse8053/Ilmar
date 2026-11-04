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
| Evidence, and which finding led to which issue | `docs/research/findings.md`. `docs/roadmap.md` lists the same links, and a test keeps the two consistent |

Issue numbers are stable IDs and may be cited anywhere. Issue *state* may not.

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

1. Update `docs/STATUS.md` in the same PR: a change log entry, plus the plan or
   recommended next task if they changed. Do not record issue state there.
   Update finding statuses and `docs/roadmap.md` together if they changed.
2. Open the PR with the template. The body says `Closes #<issue>`. Name the
   sensitive areas it touches.
3. **Never merge on your own initiative.** Merge only when the maintainer
   explicitly asks for that specific PR to be merged, CI is green, and GitHub
   reports it as mergeable. Just before merging, change any ADR the PR
   introduces from `Proposed` to `Accepted` (the maintainer's approval is the
   acceptance). Use a merge commit.
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
