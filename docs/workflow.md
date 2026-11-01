# Development workflow

How Ilmar development is planned, tracked and resumed. It is written so that a
new session, human or agent, can continue from the repository and GitHub alone
(ADR-0009). Later, Ilmar will run this same workflow itself through its GitHub
adapters (issue #20).

## Where things live

| Question | Source of truth |
| --- | --- |
| What must Ilmar be? | `MANIFEST.md` |
| What rules apply to changes? | `AGENTS.md` |
| Where are we right now? | `docs/STATUS.md` (on `main`) plus open PRs and `status:in-progress` issues |
| What is planned, and in which milestone? | [GitHub issues](https://github.com/Obtuse8053/Ilmar/issues) and milestones; summarised in `docs/roadmap.md` |
| Why is it built this way? | `docs/architecture/decisions/` |
| What evidence informs it? | `docs/research/` |

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
- **Status:** `status:in-progress` (a session has claimed it) and
  `status:blocked`.
- `ilmar:ready`: the issue is eligible for Ilmar itself to execute. Not used
  before M4.

## Choosing the next task

1. If `docs/STATUS.md` has a **Next up** list, take the first open issue on it
   that is not `status:in-progress` or `status:blocked`.
2. Otherwise, take the lowest-numbered open `approved` issue with the highest
   priority in the current milestone.
3. Skip issues whose prerequisites (named in the issue or in Next up) are not
   merged yet.
4. Only `approved` issues are implemented. A `proposal` waits for the
   maintainer.

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

1. Update `docs/STATUS.md` in the same PR: change log entry, in-progress state
   and Next up. Also update `docs/roadmap.md` and finding statuses if they
   changed.
2. Open the PR with the template. The body says `Closes #<issue>`. Name the
   sensitive areas it touches.
3. **Never merge on your own initiative.** Merge only when the maintainer
   explicitly asks for that specific PR to be merged, CI is green, and GitHub
   reports it as mergeable. Use a merge commit.
4. If the session ends before the work is done, push the branch and leave a
   handoff comment on the issue. Say what is done, what is next, and anything
   surprising.

## Milestone close-out

When the last issue of a milestone is merged:

1. Close the GitHub milestone and update `docs/roadmap.md`.
2. Split the next milestone's epics into issues. Open them with the
   `proposal` label for maintainer approval.
3. Record the recommended next task in `docs/STATUS.md` and in the final PR or
   a comment. Then stop and wait for the maintainer's approval.

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
