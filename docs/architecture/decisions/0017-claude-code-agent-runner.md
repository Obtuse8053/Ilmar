# ADR-0017: Claude Code agent runner

- Status: Accepted (PR #43, 2026-11-04)
- Date: 2026-11-04
- Sources: [R13], [R15], [R19] · Findings: F-018, F-021
- Implements: ADR-0011 §4 and §6 for Claude Code (#8)

## Context and problem

#8 runs Claude Code inside the agent sandbox. MANIFEST §7.5 requires:
- documented, authorized interfaces only;
- no redistribution of the agent;
- installation and authentication that follow the provider's terms.

ADR-0006 and ADR-0011 §4 allow exactly one secret in the agent sandbox, the
model credential. ADR-0011 §6 asks for a pinned, preinstalled version, no
non-essential traffic, API-key or gateway authentication, non-interactive JSON
output, and a live test that records the hosts contacted. F-021 warns that
the exit status alone may not reflect failure.

## Considered options

Invocation:
1. Interactive mode driven through a pseudo-terminal. Not a documented
   automation interface.
2. **Print mode with `--output-format json`**: one documented result object
   ([R15]).
3. The Agent SDK. That would be a library dependency in the coordinator, and
   the agent would run outside the sandbox.

Permissions inside the sandbox:
- `acceptEdits`: shell commands would still need approval, which nobody can
  give non-interactively.
- `dontAsk`: unapproved tools are refused.
- **`bypassPermissions`**: documented for isolated environments. The sandbox
  is the security boundary (ADR-0011).

Credential delivery:
- In the container's environment: every command, and every child process,
  would see it for the container's whole life.
- **Per command**: only the agent command's environment holds it.

## Decision

- **Invocation:**

  ```
  claude -p "Ilmar task <key>: <title>\n\n<instructions>" --output-format json
      --permission-mode <mode> --no-session-persistence --strict-mcp-config
      [--max-turns N] [--model M]
  ```

  - The default mode is `bypassPermissions`; `acceptEdits` and `dontAsk`
    are allowed alternatives.
  - The prompt starts with fixed text, so it can never be parsed as an
    option.
  - `--strict-mcp-config` without a config loads no MCP servers, so there
    are no extra network destinations.
- **Environment of that command only:**
  - `ANTHROPIC_API_KEY`;
  - `ANTHROPIC_BASE_URL` for a gateway, which must be `https`, with its host
    on the allow-list;
  - `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, which implies no
    auto-updates, telemetry or error reporting, plus those three set
    explicitly.

  The value travels in the runtime's private env file (ADR-0014), never on a
  command line, and never in the container configuration. The version probe
  and every other command run without it. Every summary is redacted.
- **Outcome (F-021):** `COMPLETED` needs exit status 0, a parsed
  `{"type": "result"}` object, `is_error == false` and `subtype ==
  "success"`. Any other combination is `FAILED`, with the subtype and the
  redacted result text, truncated to 4,000 characters. A runtime timeout is
  `TIMED_OUT` (the runtime removes the sandbox, ADR-0014). Agent output stays
  advisory (ADR-0010).
- **Version pin:** the agent image recipe (`images/claude-code/Containerfile`)
  needs `CLAUDE_CODE_VERSION`, installs nothing at run time, and is built by
  the operator. It is never published by Ilmar. With `expected_version`
  set, `claude --version` must match before the agent starts; otherwise the
  attempt fails without running the agent.
- **Network:** the agent sandbox uses `ALLOWLIST` with the model endpoint
  (`api.anthropic.com`) or the gateway host (ADR-0011, ADR-0015).
  `HTTPS_PROXY` comes from the runtime.

## Verification

- The AgentRunner contract runs against the runner in three forms:
  - with a scripted `claude` speaking the documented result format;
  - on real Podman, with a real shell-script stand-in, which also checks the
    arguments, that the credential exists only in the agent command's
    environment (not in `podman inspect` and not in other commands), and the
    timeout;
  - in unit tests for the invocation, an option-like prompt, the environment,
    the gateway, redaction, ten outcome mappings, failure summaries,
    truncation, version pinning and settings validation.
- Mutation checks:

  | Mutation | Failing tests |
  | --- | --- |
  | Ignore the exit status | 2 |
  | Ignore `is_error` | 2 |
  | Drop redaction | 1 |

- An opt-in live test runs real Claude Code in an `ALLOWLIST` sandbox on a
  tiny task. It fails if the proxy logged any `DENY`, that is if Claude Code
  needed a host outside the allow-list, or if any host other than the
  listed one was contacted.

## Consequences

- Good:
  - Only documented interfaces are used, and nothing is redistributed.
  - The credential's exposure is limited to one process tree inside one
    sandbox.
  - F-021 is handled.
  - The live test turns the documented host list into evidence.
- Bad:
  - `bypassPermissions` relies entirely on the sandbox. Policies that
    disable that mode (managed settings) need `acceptEdits` or `dontAsk`,
    and a reduced capability.
  - Building the agent image needs npm and Anthropic's terms, outside CI.
  - The live test costs a few cents and needs a real key, so it is opt-in.
  - Maintainer decision on acceptance: the adapter is not operationally
    ready, and no unattended run may be enabled, until the live smoke test
    has passed at the pinned version (#44).

## Revisit when

- The LLM gateway (#18) lands. Then the sandbox gets a per-execution
  gateway token instead of the real key.
- Claude Code's documented output format or flags change ([R13] and [R15]
  are on the research watch list).
- A second agent runner (#24) shows which parts belong in a shared helper.
