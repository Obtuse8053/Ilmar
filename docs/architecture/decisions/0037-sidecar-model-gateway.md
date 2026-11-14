# ADR-0037: A model gateway sidecar, with estimated per-request reservations and halts

- Status: Proposed
- Date: 2026-11-12; reconciled with the implementation 2026-11-14
- Sources: [R14], [R21], [R29] · Findings: F-019
- Amends (by addition; no accepted text is edited):
  - **ADR-0029:** per-attempt spending control in front of the provider.
  - **ADR-0006:** narrows its single exception: the model key no longer
    enters the agent sandbox.
  - **ADR-0036:** closes the routes it leaves open.
- Supersedes for Mode 2: #111's TLS-terminating proxy and #94's LiteLLM
  gateway (deferred; ADR-0032 stays reserved).
- Asked for by the maintainer, 2026-11-12: conditional approval of the
  accelerated roadmap, amendments 1 and 2.
- Built in #118 (P4 part 1), #122 and #124 (P5 part 2a); spend settlement is
  ADR-0039 (#127). The maintainer's beta and shape decisions of 2026-11-13
  are recorded in §2.

## Changes from the first proposal (PR #115)

The first draft promised more than the gateway can prove. This version
states what was built:

- **No proven dollar bound.** The draft called each request's reservation a
  *worst case* and the per-attempt, daily and monthly figures *proven upper
  bounds*. Only the output term is a hard bound. The input term is an
  estimate (§2). A request can cost more than its reservation; that is
  charged in full and halts the attempt (§3). The provider's own spend cap is
  the hard backstop (§4).
- **A separate sidecar, not the existing egress proxy.** A `GATEWAY` sandbox
  gets a gateway sidecar *instead of* the egress proxy, on an `--internal`
  network.
- **The key's hand-over** is a FIFO in the sidecar's tmpfs, written through
  `podman exec` standard input. It is not a runtime secret.
- **Every non-200 response, and every untrusted usage report, halts the
  attempt.** The draft passed errors through and kept charging per request.
- **Settlement in the ledger, crash recovery and spending review** are
  ADR-0039. The draft's "accounting record" only said one would exist.
- **No dependency on #112.**

## Context and problem

Mode 2 runs unattended. It needs two things the earlier design does not give:

1. **The real model key outside the sandbox.** With the key in the sandbox,
   the agent can read it, and can use any other credential it finds against
   the allow-listed model host (ADR-0036's open routes).
2. **Spending control in front of the provider, per attempt.** A reservation
   (ADR-0029 §2) only decides whether an attempt may start. A token counter
   alone does not bound dollars: models, usage types and price tiers differ,
   and a response's usage is only known after it is paid for.

## Considered options

1. **LiteLLM (#94).** It brings Postgres and Redis failure modes, licence
   questions and an evaluation that may fail. Not needed for one repository
   with one execution at a time. **Deferred.**
2. **A TLS-terminating proxy (#111 as written).** It needs a CA the sandbox
   trusts. Unnecessary: the pinned Claude Code accepts a plain-HTTP gateway
   URL on a private network (C0 and C1, #111).
3. **A per-sandbox gateway sidecar written in Ilmar, standard library only
   (chosen).** It reuses Ilmar's sandbox network isolation and lifetime and
   adds no external service.

## Decision

### 1. Topology and credential

- **The sandbox's only route.** A `GATEWAY` sandbox sits alone with its
  gateway sidecar on a private `--internal` network without DNS. The sidecar
  is its only peer and has a second leg to the provider. There is no egress
  proxy and no route from the sandbox to the provider or anywhere else.
- **The throwaway token.** The runtime makes a random per-attempt token and
  gives the sidecar only its SHA-256. The token lives in the memory of the
  runtime that started the sandbox and reaches the sandbox only through the
  agent-run step's environment (`ANTHROPIC_API_KEY`, with
  `ANTHROPIC_BASE_URL` pointing at the sidecar). A restarted coordinator
  cannot reuse it. The gateway accepts only that token, compared in constant
  time. It drops every credential header and adds the real key.
- **The real key.** It reaches only the sidecar: written by the coordinator
  with `podman exec` on standard input into a FIFO in the sidecar's tmpfs,
  read once into memory, the FIFO removed. It never exists in a file, a
  command line, an environment, a log or `repr`.
- **HTTPS rule.** `ClaudeCodeSettings` still requires HTTPS for an operator
  gateway. Plain HTTP is allowed only for the address the runtime provides.

### 2. Admission: strict shapes and an estimated reservation

- **Attempt budget `B`:** the attempt's ledger reservation (ADR-0029 §2),
  passed per sandbox (ADR-0039 §1).
- **The rule:** a request is forwarded only while the attempt is not halted,
  and only if *settled + in flight + this request's reservation ≤ B*.
- **The reservation** = output bound + estimated input.
  - **Output is a hard bound.** The gateway lowers `max_tokens` to
    `max_output_tokens`, then to the largest value that fits `B`, never below
    `min_output_tokens`; below that it refuses (`budget_exceeded`). Thinking
    counts within it.
  - **Input is an estimate.** The body's bytes plus per-message, tool-prompt
    and (if attested) per-image allowances, at the dearest input-side rate,
    scaled for long context. The provider adds its own prompts and
    tokenizes in its own way, so no one outside it can prove an input bound.
- **Allowed models:** only those with an attested price (micro-dollars per
  million tokens for input, output, cache writes and reads). Unknown models
  are refused.
- **Strict shapes.** Every top-level and nested field is checked against an
  allow-list: `output_config`, `context_management`, system messages, tool
  additions and removals, `thinking.display`. Refused: server tools in any
  shape; documents, URL images, search results and other content types
  outside the list; images without an attested bound; `service_tier` other
  than `standard_only`; `mcp_servers` and `container`; a thinking budget not
  below `max_tokens`.
- **Betas.** Every `anthropic-beta` header value is read. A value on the
  operator's allow-list is kept. One unused and unsupported is dropped. One
  that is used but not allowed, or in a separately billed family, refuses
  the request. `advanced-tool-use` is denied. The maintainer approved seven
  candidates on 2026-11-13, plus `thinking-token-count` conditionally, since
  its estimate is not billable.
- **`count_tokens`** is checked the same way and not charged. A failed
  counting request halts the attempt without a charge.
- **Limits:** request body size, `max_in_flight` and an upstream timeout.

### 3. Settlement and halts

- **A complete, valid usage report** is charged at the attested prices, in
  full.
- **A report above the reservation** is charged in full and recorded as an
  overrun, and the attempt **halts**.
- **A missing, partial, malformed or unrecognised report**, a stream cut off
  or with an unknown event, any non-200 status, or an unreachable provider is
  charged the whole reservation, and the attempt **halts**.
- A halted attempt forwards nothing more.
- Each metered request has `ADMIT` and `CHARGE` lines in the sidecar's log
  (amounts, reason, halt; never content or credentials). ADR-0039 makes that
  log a complete, checked record and settles it into the ledger.

### 4. What is and is not bounded

- **Spend per attempt** is at most `B` plus:
  - the overruns of requests in flight when the attempt halts;
  - whatever the provider bills for requests whose usage could not be
    trusted.

  With `max_in_flight` 1, that is one request. Neither term has a bound the
  gateway can prove.
- **The ledger** records what the gateway settled. ADR-0039 records an
  uncertain settlement as such and holds new work until a person reviews it.
- **The provider's own spend cap**, set by the operator, is the external
  hard backstop (ADR-0029). It is not optional, and it is tested on its own
  (the Gate 1 cap drill).
- **What the estimates rest on:**
  - the operator's price table being at least the real prices;
  - the provider metering the documented usage types;
  - the sidecar being the only route, which the network design gives and a
    real-Podman test checks.

### 5. Failure behaviour (fail closed)

- **Sidecar gone:** it lives and dies with its sandbox. The agent loses its
  route, and the gateway is never restarted mid-attempt. A restarted gateway
  would be a second `READY` line in its log, which ADR-0039 treats as an
  untrusted record.
- **Malformed or refused requests** are answered locally, in the provider's
  error shape. Nothing reaches the provider.
- **Provider refusals** (spend limit, billing, authentication) reach the
  runner's classification (ADR-0029 §5) and suspend.

## Consequences

- **Closes ADR-0036's open routes.** The only credential in the sandbox is
  the throwaway token, worthless outside the attempt. A credential found in
  the repository cannot reach the provider: there is no route, and the
  gateway takes only its own token.
- **#110's secret scan** stays as defence in depth.
- **Some agent requests are refused.** The compatibility records (C0, C1 on
  #111) show the pinned Claude Code needs none of them.
- **The operator maintains an attested price table.** A price that is too low
  makes the estimate wrong. That is charged and halts, but it is not
  prevented.
- **Gateway-backed operation stays disabled in configuration** until its
  acceptance gates pass (#111).

## Tests required (built)

- **Compatibility:** `test_claude_code_gateway_compat.py` through the real
  sidecar (C0, C1).
- **Unit:** shapes, betas, server tools in every shape, lowering of
  `max_tokens`, budget refusal, metering, untrusted reports and non-200
  halts, counting halts without a charge, concurrency limits, and a log free
  of credentials.
- **Real Podman:** no route but the sidecar; no key in the sandbox or in
  either container's definition; token-only access; removal of sidecar and
  network; the key hand-over leaving no copy after a crash.
- **Real API:** `test_gateway_live_api.py` through `gateway-live.yml` (#126),
  behind a protected environment and a spend-capped key.

## Revisit when

- More than one concurrent execution or repository is needed: LiteLLM or a
  shared gateway (ADR-0032).
- The provider publishes a provable input bound, or signs usage reports.
- The provider adds usage types or price tiers that Claude Code uses.
- A pinned version fails the compatibility check: the HTTPS variant (the
  sidecar serving a per-sandbox certificate trusted through
  `NODE_EXTRA_CA_CERTS`).
