ILMAR — Project Manifest

The autonomous software factory

Status: Founding specification
License: Apache License 2.0
Development model: Agent-first, open-source, self-hosted

1. Mission

Ilmar is an open-source, provider-independent software factory that transforms development tasks into verified code changes using autonomous coding agents.

Ilmar must eventually be capable of developing, testing, and maintaining its own codebase.

The project's goal is to provide reliable orchestration around existing coding agents, rather than reinvent their coding capabilities.

2. Founding Principles

1. Provider independence. Issue trackers, source-control platforms, coding agents, and container runtimes must be interchangeable.
2. Autonomy with accountability. Agents implement changes; verification and configurable approval policies determine acceptance.
3. Self-hosting. Ilmar must be capable of developing and improving its own repository.
4. Reliability before complexity. Executions must support recovery, bounded retries, cancellation, and observability.
5. Minimal infrastructure. Avoid unnecessary services, frameworks, and deployment dependencies.
6. Security by design. Untrusted workloads must not be able to bypass orchestration or approval policies.
7. Open development. Architecture, tests, development processes, and engineering decisions must be documented and reproducible.
8. Respect intellectual property. Third-party code and documentation must never be incorporated without an appropriate license and attribution.

3. Initial Technology Stack

Component| Initial implementation
Language| Python
Task management| GitHub Issues
Optional task dashboard| GitHub Projects
Source control| GitHub
Container runtime| Podman
Coding agent| Claude Code
Execution persistence| SQLite
CI| GitHub Actions

Initial development may be hosted in GitLab. Supporting self-development directly against that repository requires a working GitLab source-control adapter.

Additional planned integrations include GitLab, Jira, Docker, Codex, and other coding agents.

The core architecture must remain independent of all providers.

4. Architecture

Ilmar will use a ports-and-adapters architecture.

Core components

- Workflow state machine
- Task scheduler
- Execution coordinator
- Recovery and reconciliation
- Execution persistence
- Verification and approval policies
- Structured event logging

Provider interfaces

- "TaskProvider" — work discovery and task lifecycle
- "SourceControlProvider" — branches, PRs/MRs, and CI results
- "ContainerRuntime" — isolated execution environments
- "AgentRunner" — coding-agent invocation and results
- "ExecutionStore" — durable execution state

Adapters must be independently configurable.

Provider-specific types and dependencies must not leak into core domain logic.

All adapters must pass shared contract tests, supplemented by provider-specific integration tests.

5. Workflow

The initial end-to-end workflow is:

1. Discover an eligible task.
2. Claim the task without duplicate execution.
3. Provision an isolated workspace.
4. Invoke the configured coding agent.
5. Implement changes and run tests.
6. Validate acceptance criteria using independent checks.
7. Commit and push changes to a task branch.
8. Open or update a pull request or merge request.
9. Record execution results and verification evidence.
10. Recover safely after crashes or interruptions.

Planning and independent agent review are configurable stages. Deterministic verification must not rely solely on an agent declaring success.

6. Self-Development

Ilmar must eventually use its own orchestration to maintain the Ilmar repository.

The running factory must remain separate from candidate implementations.

Sensitive changes—including authentication, sandbox policy, approval controls, and release automation—require independent verification and authorized human approval.

Self-hosting acceptance tests must demonstrate that Ilmar can modify a disposable copy of its own repository, verify the result, and produce a reviewable change request.

The initial bootstrap may be performed by Claude Code under human supervision. Once functional, Ilmar should perform its normal development through its own workflow.

7. Intellectual Property and Licensing Policy

This section is mandatory for all human contributors and autonomous coding agents.

7.1 Original implementation

Ilmar must be independently implemented using its own architecture, source code, tests, and documentation.

Published architectural ideas, interfaces, algorithms, and engineering patterns may inform the design, subject to applicable rights and restrictions.

Architectural inspiration must be documented in Section 9.

External implementation code must not be copied into Ilmar merely because it is publicly accessible.

7.2 Third-party code

Before incorporating third-party source code or other protected material:

1. Identify the exact source repository, file, and revision.
2. Verify the license covering the specific material.
3. Check compatibility with Ilmar's Apache 2.0 distribution.
4. Preserve all required copyright and license notices.
5. Record material modifications when required.
6. Document the imported material in "THIRD_PARTY_NOTICES.md".
7. Obtain maintainer approval.

Do not assume a repository-wide license covers every file, vendored dependency, example, asset, or enterprise component.

When licensing is uncertain, do not incorporate the material.

7.3 AI-generated code

AI-generated code is subject to the same review requirements as human-written code.

Coding agents must:

- Prefer original implementations.
- Never intentionally reproduce substantial third-party code without license verification.
- Avoid copying existing repositories, specifications, documentation, or examples verbatim.
- Identify external sources materially used during implementation.
- Flag uncertain provenance for human review.
- Never remove copyright notices or license obligations.

AI-generated content must not be presumed free of third-party rights merely because it was generated by a model.

7.4 Dependencies

Dependencies must be checked for applicable licenses and redistribution obligations.

Continuous integration should include:

- Dependency inventory and license scanning.
- Detection of known incompatible licenses.
- Secret scanning.
- Checks for missing third-party notices.

Automated scans supplement, but do not replace, manual review of uncertain provenance and licensing.

7.5 Agent integrations

Ilmar may interface with proprietary coding agents through their documented, authorized interfaces.

Integration does not imply permission to redistribute their executables, proprietary source code, models, or protected assets.

Agent installation and authentication must comply with the provider's applicable terms.

7.6 Contributions

Ilmar is intended to be distributed under Apache License 2.0.

Before accepting external contributions, the project will establish a Contributor License Agreement that explicitly addresses the intended future relicensing rights.

Contributors must have the necessary rights to submit their work. A CLA does not eliminate third-party license obligations.

7.7 Branding

Third-party names may be used factually to identify supported integrations and acknowledge prior art.

Ilmar must not imply endorsement, affiliation, or sponsorship by the organizations behind referenced projects.

8. Development Protocol

Each milestone follows this process:

1. Define measurable acceptance criteria.
2. Review relevant research and prior art.
3. Document the intended architecture.
4. Implement the smallest useful increment.
5. Add and execute automated tests.
6. Perform independent verification.
7. Review security, dependencies, and IP provenance.
8. Submit a reviewable change.
9. Update documentation and architectural decisions.

External references guide design decisions but are not implementation templates to reproduce automatically.

9. References and Prior Art

The references below identify relevant research and existing implementations. Their inclusion acknowledges prior art but does not indicate that Ilmar incorporates their code.

[R1] OpenAI — Symphony

- Repository: https://github.com/openai/symphony
- Technical specification: https://github.com/openai/symphony/blob/main/SPEC.md
- License: Apache 2.0, as stated by the upstream repository.
- Relevance: Issue-driven agent orchestration, isolated workspaces, polling, recovery, and reconciliation.
- Ilmar approach: Independently implement provider-agnostic orchestration informed by these patterns.

[R2] OpenAI — Harness Engineering

- Article: https://openai.com/index/harness-engineering/
- Type: Engineering research and experience report.
- Relevance: Agent-first repository development, structured project knowledge, architectural enforcement, and continuous feedback.
- Ilmar approach: Apply the engineering principles while creating original repository instructions and enforcement mechanisms.

[R3] Anthropic — Effective Harnesses for Long-Running Agents

- Article: https://www.anthropic.com/research/effective-harnesses-for-long-running-agents
- Type: Engineering research.
- Relevance: Incremental implementation, durable progress artifacts, context recovery, and test-driven execution.
- Ilmar approach: Design original task handoff and recovery mechanisms.

[R4] LangChain — Open SWE

- Repository: https://github.com/langchain-ai/open-swe
- License: MIT for Open SWE's own code; the upstream repository excludes "vendor/" from that license.
- Relevance: End-to-end autonomous software development, sandbox execution, review, and CI feedback.
- Ilmar approach: Compare capabilities and implementation tradeoffs. No upstream code reuse is assumed.

[R5] OpenHands

- Repository: https://github.com/OpenHands/OpenHands
- License: MIT for the open-source components, subject to repository-specific exceptions.
- Relevance: Sandboxed agent execution and extensible software-engineering automation.
- Ilmar approach: Learn from adapter and execution concepts without importing implementation code.

[R6] SWE-agent — mini-SWE-agent

- Repository: https://github.com/SWE-agent/mini-swe-agent
- License: MIT, according to the upstream repository.
- Relevance: Minimal agent execution harnesses and low-complexity engineering.
- Ilmar approach: Use simplicity as an architectural benchmark.

[R7] StrongDM — Software Factory

- Article: https://factory.strongdm.ai/
- Type: Engineering report.
- Relevance: Independent behavioral verification, scenario testing, and iterative autonomous development.
- Ilmar approach: Develop original verification infrastructure. No permission to reuse article content or implementation material is presumed.

[R8] Apache Software Foundation — Apache License 2.0

- Official license: https://www.apache.org/licenses/LICENSE-2.0
- Relevance: Ilmar's intended licensing terms and third-party notice obligations.

Reference management requirements

- Record source URLs and access dates when making consequential architectural decisions.
- Record exact versions and revisions before importing any external code.
- Track third-party code separately from conceptual inspiration.
- Periodically review upstream projects for architectural developments and security implications.
- Preserve a clear history of why significant design choices were made.

10. Initial Milestones

M1 — Foundation

Provider contracts, SQLite execution store, state machine, configuration, contract tests.

M2 — End-to-end execution

GitHub issue → Podman → Claude Code → tests → pull request.

M3 — Reliability

Restart recovery, duplicate-work prevention, retries, cancellation, structured logs, and execution limits.

M4 — Self-hosting

Ilmar implements and validates changes against its own repository, with controlled release promotion.

M5 — Portability

Docker and a second task/SCM provider pass common adapter tests.

M6 — Public stability

Documented APIs, reproducible releases, security review, onboarding, and interoperability testing.

11. Long-Term Objective

Ilmar should become a widely adopted, self-hosted and provider-independent foundation for autonomous software engineering.

Its effectiveness should be measured by verified outcomes, reliability, cost efficiency, and reduced human intervention—not merely the volume of generated code.