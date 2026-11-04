# ADR-0011: Sandbox isolation profile and egress allow-listing

- Status: Proposed
- Date: 2026-11-04
- Sources: [R13], [R14], [R15], [R16], [R18] · Findings: F-018, F-019, F-020
- Amends: ADR-0006 (resolves its "Revisit when: egress allow-listing")

## Context and problem

ADR-0006 keeps tracker and SCM credentials out of the sandbox, but left the
details to M2: network allow-listing, how the model credential is delivered,
and which mounts and privileges the sandbox gets. The coding agent needs HTTPS
access to its model endpoint ([R13]). Anthropic's reference devcontainer
enforces egress with a firewall inside the container, which needs extra
network capabilities ([R16]).

## Considered options

For egress:

1. A firewall inside the container (the reference devcontainer approach). It
   needs `NET_ADMIN`/`NET_RAW` and root at start-up.
2. An internal-only container network whose only route out is a
   coordinator-controlled HTTPS proxy that allows listed hosts.
3. No network at all, with model calls relayed by the host (a host-side
   gateway).
4. Unrestricted network.

## Decision

Chosen option: 2, with option 3's gateway as the planned evolution for
credentials. The profile below is provider-neutral; the Podman mapping is
implemented and tested in #5.

1. **Privileges.**
   - Rootless runtime, non-root user inside the container.
   - All capabilities dropped, `no-new-privileges`, default seccomp profile.
   - Read-only root filesystem, with tmpfs for `/tmp` and the agent's home
     directory.
   - Limits on process count, memory and CPU.
   - The container name is derived from execution id and attempt (ADR-0012).
2. **Mounts.** Exactly one host directory, the sandbox's workspace, mounted
   read-write. Never mounted:
   - container-runtime sockets;
   - the host home directory or SSH agent;
   - credential stores;
   - the coordinator's git directories or state directory.
3. **Environment.** Built only from an explicit allow-list. The host
   environment and runtime-injected proxy variables are not inherited. For
   Podman this means `--http-proxy=false`, to be verified in #5.
4. **Credentials.** Never tracker or SCM credentials. In M2 the agent sandbox,
   and only the agent sandbox, receives the model credential: the single
   exception in ADR-0006. The planned evolution is a host-side LLM gateway
   that issues per-execution, budget-limited, revocable tokens, so the real
   key never enters the sandbox ([R14], F-019, tracked in #18).
5. **Network.** `NetworkPolicy` has three modes:
   - `NONE`: the default, and always used for verification sandboxes.
   - `ALLOWLIST(hosts)`: for the agent sandbox.
   - `UNRESTRICTED`: explicit opt-in only, logged as a warning.

   `ALLOWLIST` is enforced outside the sandbox. Each execution's container
   sits on **its own** internal network with no external route, so it cannot
   reach other sandboxes. The only peer on that network is a filtering HTTPS
   proxy. The sandbox gets `HTTPS_PROXY`. No network capabilities are needed
   inside the sandbox ([R18], F-020). The proxy:
   - allows only `CONNECT <listed-hostname>:443`. It rejects IP literals
     (including the IPs of listed hosts), other ports, and absolute-form
     plain-HTTP requests;
   - resolves DNS itself, and only for listed hostnames, so the sandbox needs
     no resolver and DNS lookups cannot carry data out;
   - connects upstream over IPv4 or IPv6 as the host allows. The sandbox
     network itself has no IPv6 route.
6. **Claude Code compatibility** ([R13], [R15], F-018).
   - Claude Code is preinstalled in the image at a pinned version, so there
     is no install or update traffic at run time.
   - Non-essential traffic is disabled
     (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`).
   - Authentication uses an API key or gateway token, so no OAuth hosts are
     needed.
   - The allow-list is the model endpoint: `api.anthropic.com`, or the gateway
     host.
   - It runs in non-interactive mode with structured JSON output.
   - An opt-in integration test in #8 records the hosts actually contacted,
     because the documented list changes. The monthly research review watches
     [R13].
7. **Verification sandbox.** A separate container from the agent's, network
   `NONE` and no model credential. Dependencies come from the image or are
   vendored. A verification policy may explicitly opt in to an allow-list.
8. **Host-side git.** As ADR-0010 specifies: never inside a sandbox holding
   credentials, and hardened on the host.
9. **Required negative integration tests.** These are opt-in
   (`pytest -m integration`), run in CI on a runner with the container
   runtime, and **must pass before #5 merges**. Each attempt runs from inside
   a sandbox and must fail. Each target also gets a positive control that
   reaches it *from the host*, so a failure inside the sandbox proves the
   isolation rather than a missing network. The suite runs for `NONE` and for
   `ALLOWLIST`, with the allowed host's proxied `CONNECT` as the only positive
   case.
   - **DNS:**
     - no resolver is configured or reachable;
     - UDP and TCP queries to public resolvers on port 53 fail;
     - DNS-over-HTTPS to a resolver that is not listed fails;
     - asking the proxy to `CONNECT` to a hostname that is not listed is
       rejected *without* the proxy resolving it (checked in the proxy log).
   - **IPv6:**
     - no global IPv6 address or route exists;
     - TCP to an IPv6 literal (public, or the host's link-local or ULA
       address) fails;
     - `CONNECT [ipv6-literal]:443` through the proxy is rejected.
   - **Direct IP:**
     - TCP 80 and 443 to a public IPv4 address fail;
     - TCP to the *resolved IP of an allowed host*, bypassing the proxy,
       fails;
     - UDP 443 (QUIC) and ICMP fail.
   - **Proxy bypass and misuse:**
     - with `HTTPS_PROXY` unset, direct connections fail;
     - `CONNECT` to an IP literal is rejected, even for an allowed host's IP;
     - `CONNECT` to an allowed host on a port other than 443 is rejected;
     - absolute-form `GET http://…` through the proxy is rejected;
     - a request carrying a `Host` header that is not listed is rejected;
     - the proxy's own management or other ports are unreachable.
   - **Host and neighbours:**
     - the host gateway address, `host.containers.internal`, the
       container-runtime API and cloud metadata (`169.254.169.254`) are
       unreachable;
     - a second, concurrently running sandbox on another execution's network
       is unreachable.

Domain changes, made in #5: `ContainerSpec` gains `name`, and `NetworkPolicy`
gains `ALLOWLIST` with `allowed_hosts`.

## Consequences

- Good: the agent cannot reach tracker or SCM credentials and can only talk to
  its model endpoint. No privileged capabilities are needed. The profile works
  with any OCI runtime that supports internal networks.
- Bad: a proxy component must be run and tested. The model key remains inside
  the agent sandbox until the gateway exists.
- Residual risk the tests cannot remove: a tunnel through an *allowed* host
  can carry any TLS traffic to that host's service. The proxy sees only the
  `CONNECT` target, not the TLS SNI or content. Keep the allow-list to the
  model endpoint only. Tests that need the network at
  verification time need an explicit policy opt-in.

## Revisit when

- The LLM gateway lands (#18). Then remove the model-key exception.
- Container runtimes offer native, hostname-based egress allow-lists.
- Claude Code's connectivity requirements change ([R13] is on the research
  watch list).
