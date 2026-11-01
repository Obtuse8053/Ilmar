# Status

The resume point for any session. Update it in every PR, following
`docs/workflow.md`. The live state is also in open PRs and in issues labelled
`status:in-progress`.

## Current milestone

**M2 End-to-end execution:** GitHub issue → Podman → Claude Code → tests → pull
request ([milestone](https://github.com/Obtuse8053/Ilmar/milestone/2)).

M1 Foundation is complete (PR #1, merged 2026-11-01).

## In progress

- #2 Session-resume workflow: this PR.

## Next up

In order. Each item lists its prerequisites.

1. #3 Contract suites and fakes for `SourceControlProvider` and `AgentRunner`
2. #4 Safe workspace path derivation (after #3)
3. #6 GitHub `TaskProvider` (after #3). Can run in parallel with #4 and #5.
4. #5 Podman `ContainerRuntime` and the network-policy ADR (after #4)
5. #7 GitHub `SourceControlProvider` (after #3, #4)
6. #8 Claude Code `AgentRunner` (after #3, #5)
7. #9 Verification stage (after #5)
8. #10 Adapter registry (any time before #11)
9. #11 Coordinator loop and end-to-end test (after all of the above). Closes M2.

Maintenance, any time: #26 (LICENSE check), #28 (secret scanning), and the
other issues without a milestone.

## Decisions needed from the maintainer

- #26: verify `LICENSE` against apache.org (the agent could not reach it).
- #27: correct the R3 URL in `MANIFEST.md` §9.
- #28: enable secret scanning and push protection in the repository settings.
- Confirm the copyright holder named in `NOTICE` ("The Ilmar Authors").

## Change log

- 2026-11-01: M1 merged (#1). Backlog approved and opened as issues #2–#31
  with milestones and labels. Started #2.
