# ADR-0044: Configuring the model gateway: opt-in, manual mode only, key outside the sandbox

- Status: Proposed
- Date: 2026-11-16
- Sources: ADR-0006, ADR-0029, ADR-0030, ADR-0036, ADR-0037, ADR-0039 · Findings: none new
- Issues: #111 (gateway), #96 (Gate 2). LiteLLM (#94) stays deferred (ADR-0037).

## Context and problem

ADR-0037 built a gateway sidecar for each agent sandbox:

- the real model key reaches only the sidecar;
- the sandbox holds a throwaway token;
- every request is admitted against a reservation;
- every charge is settled into the ledger (ADR-0039).

The core already runs `GATEWAY` sandboxes. **No configuration can choose
one:** a unit test forbids it until the reservation (ADR-0037 §2, P5) exists.
P5 and ADR-0039 have landed. Mode 2 (#96) needs the gateway, so a decision is
needed now. It must not change Gate 1, whose evidence is for the environment
route: the key in the sandbox, behind an allow-list proxy.

## Considered options

1. **Keep it unreachable until a Mode 2 ADR.** This is the safest, but the
   wiring could then be tested only in the same change that enables
   unattended operation.
2. **One switch, allowed in every mode.** This would change Gate 1's
   configuration surface and evidence in the middle of the gate.
3. **Opt-in, in manual mode only, with every precondition checked at load
   (chosen).** The wiring is real and testable. A person runs each pass.
   Gate 1 and the loop cannot use it until a later ADR allows it for Mode 2.

## Decision

### 1. Off unless asked for, in two places

- **The runner.** `[adapters.agent_runner] model_route = "gateway"`. The
  default is `"environment"`, which is today's route, unchanged.
- **The runtime.** `[adapters.container_runtime]` names the gateway's
  `gateway_image`, `gateway_key_env` and `gateway_policy`.
- **Both or neither.** A runner asking for the gateway with a runtime that
  has none (or the reverse) is a configuration error.

### 2. The key never reaches the sandbox, or the runner

- **Only the runtime reads it.** The runtime plan alone reads
  `gateway_key_env`, through `[secrets]` (environment or systemd
  credentials). The key reaches only the sidecar (ADR-0037 §1).
- **The runner gets no model credential at all.** In a `GATEWAY` sandbox it
  uses the per-attempt throwaway token and the sidecar's address. A runner
  without a key refuses to run anywhere else.
- **The key is not a sandbox secret.** It is not "entering the sandbox"
  (ADR-0006), so the sandbox secret checks do not count it. The secret
  scanner still scans every candidate for its value.

### 3. Manual mode only, for now

- **Refused under `[autonomy] mode = "supervised"`.** Gate 1, its trial and
  the loop keep the route their evidence is for.
- **Later modes need their own decision.** Modes that do not parse yet (Mode
  2) need an ADR before they may use it.

### 4. Spending preconditions, checked at load

- **`[budget]` is required.** Each attempt's reservation comes from it
  (ADR-0039), and `provider_cap_attested_usd` must be set. The provider's
  workspace cap stays the independent hard backstop (ADR-0029 §5,
  ADR-0037 §4).
- **The price policy.** `gateway_policy` is a JSON file of operator-attested
  prices and allowed betas. It is read once at load, and parsed and checked
  exactly as the sidecar will (`gateway_admission`). It must be a regular
  file owned by the operator and not writable by group or others (as the
  configuration file).
- **Concurrency.** `gateway_max_in_flight` defaults to **1**, so that the
  overrun bound is one request (ADR-0037 §4). It may be at most 4.
- **The image.** `gateway_image` must be pinned by digest (`@sha256:`): it
  runs the process that holds the key.
- **The upstream.** `gateway_upstream` defaults to
  `https://api.anthropic.com`, and it must be `https`. A plain-HTTP upstream
  exists for tests only and is never available from configuration.

## Consequences

- Good: the gateway route is testable end to end with fakes and a fake
  provider, and no credential is needed to test it.
- Good: Gate 1 is untouched; its configuration still loads and behaves as it
  did.
- Good: a configuration that would run the gateway without a budget, a
  policy or a pinned image never loads.
- Bad: manual mode means one pass per invocation. Unattended use waits for
  the Mode 2 ADR, which is intended.
- Bad: the policy's prices are attested by a person. A wrong price makes
  reservations wrong. The workspace cap is the backstop, and an overrun
  halts.

## Tests required

- **The default configuration** builds the environment route, unchanged.
- **The gateway route** builds a runtime holding the key and a runner holding
  none, with a `GATEWAY` agent sandbox.
- **Refusals:** supervised mode; no `[budget]`; only one of the two sides; a
  missing, unparsable or group-writable policy; an image without a digest; a
  plain-HTTP upstream; `gateway_max_in_flight` outside 1 to 4.
- **The key** never appears in the runner, in `repr` or in the sandbox's
  environment, and the secret scanner knows its value.
- **A runner with no key** refuses a non-`GATEWAY` sandbox.

## Revisit when

- The Mode 2 ADR decides whether the gateway route becomes mandatory there.
- ADR-0037's revisit conditions hold: more than one concurrent execution, or
  a provable input bound.
