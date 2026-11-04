# ADR-0015: Egress allow-list as a per-execution proxy sidecar

- Status: Proposed
- Date: 2026-11-04
- Sources: [R18], [R21] · Findings: F-020, F-025
- Amends: ADR-0011 §5 (where the filtering proxy runs). Implements ADR-0011 §5
  and §9 for Podman (#40).

## Context and problem

ADR-0011 §5 puts each sandbox on its own internal network whose only peer is
a filtering HTTPS proxy "on the host". Under rootless Podman, internal bridge
networks live inside the user's rootless network namespace, not the host's.
A process on the host is not on that network. The only ways to reach it from
there are the slirp4netns or pasta host-loopback forwards, which expose all of
the host's loopback services. So "host-side" has to be made concrete.

The proxy also needs a resolver and an outbound route, which the sandbox
must not have.

## Considered options

1. **A host process, reached through the rootless network's host-loopback
   forward.** This makes every loopback service on the host reachable from
   the internal network unless each one is filtered, which is a large new
   attack surface.
2. **A proxy sidecar container per execution, joined to two networks.** It
   sits on the execution's `--internal` network and on an egress network. It
   is created, owned and removed together with the sandbox.
3. **One shared proxy container for all executions.** Fewer containers, but
   the proxy would join every internal network, which bridges executions to
   each other and couples their lifetimes.
4. **The rootless network stack's built-in filtering.** Neither slirp4netns
   nor pasta filters by hostname.

## Decision

Chosen option: 2.

- **Topology per `ALLOWLIST` sandbox `<name>`:**
  - The network `<name>-net` is created with `--internal` and
    `--disable-dns`, IPv4 only. It carries the ownership labels and
    `io.ilmar.role=egress-network`.
  - The proxy container `<name>-egress` is on `<name>-net` and on the egress
    network (default: Podman's `podman` network).
  - The sandbox `<name>` is on `<name>-net` only, with `--dns=none`,
    `--no-hosts`, IPv6 disabled by sysctl and ICMP sockets disabled
    (`ping_group_range=1 0`). Its environment gets
    `HTTPS_PROXY=http://<proxy-ip>:3128`.
- **The proxy** (`egress_proxy.py`) is standard-library Python. It is mounted
  read-only and run with `python3 -I` from an operator-provisioned image.
  - It accepts only `CONNECT <listed-hostname>:443`.
  - It refuses IP literals (including a listed host's address), other ports,
    absolute-form requests and mismatching `Host` headers.
  - It resolves a name only after the name passes the policy, and logs every
    lookup.
  - It serves only clients from `<name>-net`'s subnet.
  - The container runs as a non-root user (65534) with no capabilities,
    `no-new-privileges`, a read-only root, no host environment, and
    process and memory limits.
- **`EgressSettings`** carries the proxy image, the egress network, the port
  and optional `extra_hosts`. `extra_hosts` pins listed names to addresses in
  the proxy only, for example a private LLM gateway; it never widens the
  allow-list. Without `EgressSettings`, `ALLOWLIST` is refused.
- **Lifecycle and ownership (ADR-0012):**
  - Sidecar resources carry the sandbox's owner labels.
  - They are removed after the sandbox on `stop`, on a timeout, on
    replacement, and on a failed start.
  - Leftovers owned by the same owner, from a crashed start, are cleaned
    before a new start.
  - Resources with any other owner are never touched.
- **Podman defaults overridden:** Podman's default `containers.conf` allows
  unprivileged ICMP sockets for group 0 (`ping_group_range "0 0"`). The §9
  structural check surfaced this. The sandbox user (gid 1000) was never
  covered by it, so this is defence in depth, not a fix for an exposure.
  `NONE` and `ALLOWLIST` sandboxes set `1 0`. A behavioural test (ping to
  loopback is refused) checks the result.
- **Verification (ADR-0011 §9):** the real-Podman CI job runs the §9 suite
  with nonce controls against self-hosted targets on the host's IPv4 address
  (TCP 53/80/443, UDP 53/443). Required cases:
  - the proxied `CONNECT` to the allowed host (the only positive case);
  - direct TCP and UDP to the host from `NONE` and `ALLOWLIST`, including the
    allowed host's own address and QUIC;
  - six forms of proxy misuse, each answered with 403 and no nonce;
  - unlisted names refused without resolution (checked in the proxy log);
  - only the proxy port open on the proxy;
  - nothing reachable on the internal gateway;
  - another execution's sandbox unreachable (the control comes from that
    sandbox's own network);
  - structural assertions: interfaces, no IPv6, no resolver, no
    `host.containers.internal`, no capabilities, no ICMP sockets.

  Optional cases: public resolvers, DNS over HTTPS and a public IP, and cloud
  metadata. These have a host-side TCP connect control and are reported
  when inconclusive.

## Consequences

- Good:
  - No host service is exposed to the internal network.
  - The allow-list and DNS live outside the sandbox.
  - Each execution is isolated from the others.
  - Every resource is owned and cleaned up with its sandbox.
  - The proxy is small, dependency-free and unit-tested in-process.
- Bad:
  - Each `ALLOWLIST` execution costs a second container and a network.
  - The proxy image is another trusted, operator-provisioned artefact.
  - The proxy shares the egress network with other containers on it; it
    refuses clients outside its own subnet.
  - The residual risk from ADR-0011 remains: a tunnel to an allowed host can
    carry any TLS traffic to that host's service.
- IPv6 on the sandbox side is simply off. The proxy connects upstream over
  whatever the egress network provides.

## Revisit when

- Podman or netavark offers hostname-based egress filtering for internal
  networks.
- Many concurrent executions make per-execution proxies costly. Then
  consider a shared proxy with per-network listeners and a new ADR.
- The LLM gateway (#18) lands. Then `extra_hosts` and the allow-list shrink
  to the gateway only.
- Podman's default sysctls change (the ICMP override is tested).
