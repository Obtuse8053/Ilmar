# Source registry

This is the registry of external sources that inform Ilmar's architecture.
R1–R8 are the founding references from MANIFEST §9; this file adds what we
verified and new sources from R9 onwards. IDs are permanent: never renumber or
reuse them. Cite them as `[R<n>]` in findings and ADRs.

**Access** records how the information was obtained:

- **primary**: the source itself was read.
- **primary (snippet)**: search-engine extracts of the source's own page,
  without opening the full page.
- **secondary**: reports about the source by third parties.

Claims resting only on secondary access must be re-checked against the primary
source before they decide anything. **License** gives what the source states,
with the access level of that statement. Inclusion here grants no permission to
copy; see MANIFEST §7 and `THIRD_PARTY_NOTICES.md`.

## Registry

| ID | Source | Published / last activity | License (as stated) | Access (date) | Relevance to Ilmar | Watch for |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | OpenAI Symphony: [repository](https://github.com/openai/symphony), [SPEC.md](https://github.com/openai/symphony/blob/main/SPEC.md) | Repo created 2026-02-26; announced publicly in spring 2026 | Apache-2.0, covering spec and Elixir reference implementation (repo metadata, primary snippet) | primary (snippet) + secondary (2026-11-01) | Tracker-driven orchestration, per-issue workspaces, hooks, retries/backoff, stall detection, reconciliation. README calls it an engineering preview for trusted environments | SPEC.md revisions; trust/sandbox sections; maintenance status |
| R2 | OpenAI, [Harness engineering](https://openai.com/index/harness-engineering/) (R. Lopopolo) | 2026-02-11 | All rights reserved (default); ideas only | primary (snippet) (2026-11-01) | Repository knowledge as system of record, short AGENTS.md as a map, mechanically enforced layering, agent-legible app and telemetry, agent-to-agent review | Follow-up posts |
| R3 | Anthropic, Effective harnesses for long-running agents (J. Young) | 2025-11-26 | All rights reserved (default); ideas only | secondary (2026-11-01) | Initializer and coding-agent split, progress files, feature lists where the agent may only flip pass flags, one increment per session, verify before marking done | **URL check needed:** the article is listed under anthropic.com/engineering, not the `/research/` path in MANIFEST §9 |
| R4 | LangChain, [Open SWE](https://github.com/langchain-ai/open-swe) | Relaunched 2026-03-17 on the Deep Agents library; active | MIT (secondary). The `vendor/` exclusion noted in MANIFEST §9 was **not verified** | secondary (2026-11-01) | Ticket-to-PR flow, pluggable sandbox providers, mid-run human messages, "open PR if needed" safety net, small curated tool set | License file and `vendor/`; sandbox interface |
| R5 | [OpenHands](https://github.com/OpenHands/OpenHands) | Active (PyPI `openhands-ai` 1.x) | MIT, **except `enterprise/`**, which is source-available and needs a paid license beyond one month (README, primary snippet) | primary (snippet) (2026-11-01) | Agent SDK, agent-server, runtime abstraction, tracker integrations | License boundary changes; SDK and runtime APIs |
| R6 | SWE-agent, [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | v2 line; active | MIT (repo metadata, primary snippet) | primary (snippet) (2026-11-01) | Minimal bash-only agent, linear history, stateless `subprocess` actions that are easy to run in Docker/Podman. Self-reported >74% on SWE-bench Verified | Major versions; benchmark claims |
| R7 | StrongDM, [Software Factory](https://factory.strongdm.ai/) | 2026-02 | All rights reserved (default); no reuse permission presumed | secondary + primary (snippet) of StrongDM blog (2026-11-01) | Scenarios kept as holdout sets outside the agent's view, probabilistic "satisfaction", digital twins of third-party services, no human code review | New posts; see R12 |
| R8 | [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0) | 2004 | The license text itself | not re-accessed (apache.org unreachable from the build environment) | Ilmar's license and notice obligations | — |
| R9 | Anthropic, Harness design for long-running application development | 2026-03-24 | All rights reserved (default); ideas only | secondary (2026-11-01) | Planner, generator and evaluator roles. Agents grade their own output too generously, so evaluation needs a separate role. Revisit harness parts as models improve | Follow-ups |
| R10 | [MADR](https://adr.github.io/madr/), Markdown Architectural Decision Records | Active since 2017 | MIT OR CC0-1.0 (repo README, primary snippet) | primary (snippet) (2026-11-01) | Basis for our lightweight ADR format. No template text copied | Major versions |
| R11 | Community Symphony implementations, for example [zack-nova/symphony](https://github.com/zack-nova/symphony) (GitHub label-state tracker) and Rust ports on crates.io (`opensymphony`, `symphony-cli`) | 2026 | Varies; zack-nova fork states Apache-2.0 | primary (snippet) (2026-11-01) | Evidence of how others map R1 to GitHub Issues (one `state:` label per issue, or Projects v2) and to Claude Code. Spec-alignment docs, workspace path-safety rules | Treat as **experimental** evidence |
| R12 | StrongDM, Attractor (`github.com/strongdm/attractor`), the coding agent behind R7 | 2026 | **Unknown, not verified** | secondary (2026-11-01) | Possible reference for scenario validation | Verify license before any use |

## Access log

Add a row whenever a source is consulted for a decision or a review.

| Date | IDs | Purpose | Notes |
| --- | --- | --- | --- |
| 2026-11-01 | R1–R7, R9–R12 | Initial verification of the MANIFEST §9 registry (M1) | GitHub, openai.com and anthropic.com could not be fetched directly from the build environment. Information came from search snippets of the primary pages and from secondary reports. Findings in `findings.md` carry evidence levels that reflect this. |
