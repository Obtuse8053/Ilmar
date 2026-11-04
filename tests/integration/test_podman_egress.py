# SPDX-License-Identifier: Apache-2.0
"""ADR-0011 §9 negative network suite against real rootless Podman (#40).

Opt-in (``pytest -m integration``). Besides the sandbox image it needs a proxy
image with ``python3`` (3.11+), and permission to bind ports 53, 80 and 443 on
the host, because the required targets are self-hosted echo services on the
host's own IPv4 address:

    podman pull docker.io/library/alpine:3.20 docker.io/library/python:3.12-alpine
    sudo sysctl -w net.ipv4.ip_unprivileged_port_start=0
    ILMAR_TEST_PODMAN_IMAGE=docker.io/library/alpine:3.20 \\
    ILMAR_TEST_PODMAN_PROXY_IMAGE=docker.io/library/python:3.12-alpine \\
        pytest -m integration tests/integration/test_podman_egress.py -rs

Outcomes are described in ``podman_support``. Required cases use self-hosted
targets, and an inconclusive required case fails. Cases that depend on outside
targets are optional; ``-rs`` lists the inconclusive ones. The only positive
case is the proxied ``CONNECT`` to the allowed host.
"""

from __future__ import annotations

import secrets
import socket
import threading
from collections.abc import Iterator

import pytest

from ilmar.adapters.podman import EgressSettings, PodmanRuntime
from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.errors import ProviderError
from ilmar.domain.workspace import ContainerSpec, NetworkPolicy, ResourceOwner

from integration.podman_support import (
    IMAGE,
    PODMAN,
    PROXY_IMAGE,
    EchoServer,
    Sandbox,
    attempt,
    classify,
    expect_isolated,
    host_ipv4,
    identity_of,
    inside,
    make_workspace,
    podman,
    remove_everything,
    require,
    sandbox_tcp,
    sandbox_udp,
    sh,
    tcp_control,
    udp_control,
)

pytestmark = pytest.mark.integration

ALLOWED = "allowed.ilmar.test"
UNLISTED = "unlisted.ilmar.test"
OWNER = ResourceOwner(instance_id="it", execution_id="egress", attempt=1, token="tok-egress")
NAMES = ("ilmar-it-allow", "ilmar-it-allow2", "ilmar-it-none")
CONNECTED = "ILMAR-CONNECTED"


# -- environment -------------------------------------------------------------------


@pytest.fixture(scope="module")
def host_ip() -> str:
    require(("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE))
    require(("ILMAR_TEST_PODMAN_PROXY_IMAGE", PROXY_IMAGE))
    address = host_ipv4()
    if address is None:
        pytest.fail("required targets need a non-loopback host IPv4 address")
    return address


@pytest.fixture(scope="module")
def servers(host_ip: str) -> Iterator[dict[str, EchoServer]]:
    """Self-hosted targets on the host's own address, each with its own nonce."""
    wanted = {"tcp443": (443, False), "tcp80": (80, False), "udp443": (443, True)}
    wanted |= {"udp53": (53, True), "tcp53": (53, False)}
    started: dict[str, EchoServer] = {}
    try:
        for key, (port, udp) in wanted.items():
            try:
                started[key] = EchoServer(host_ip, port, secrets.token_hex(8), udp=udp)
            except PermissionError:
                pytest.fail(
                    f"cannot bind {key} on the host; allow it with "
                    "sysctl net.ipv4.ip_unprivileged_port_start=0"
                )
        yield started
    finally:
        for server in started.values():
            server.close()


@pytest.fixture(scope="module")
def layout(tmp_path_factory: pytest.TempPathFactory) -> WorkspaceLayout:
    layout = WorkspaceLayout.under(tmp_path_factory.mktemp("egress").resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    return layout


@pytest.fixture(scope="module")
def runtime(layout: WorkspaceLayout, host_ip: str) -> Iterator[PodmanRuntime]:
    remove_everything(*NAMES)
    # Both names resolve to the host, so a lookup of the unlisted one *would*
    # succeed: the proxy must refuse it before resolving.
    egress = EgressSettings(
        proxy_image=PROXY_IMAGE, extra_hosts={ALLOWED: host_ip, UNLISTED: host_ip}
    )
    yield PodmanRuntime(layout=layout, egress=egress)
    remove_everything(*NAMES)


def start(
    runtime: PodmanRuntime, layout: WorkspaceLayout, name: str, network: NetworkPolicy
) -> Sandbox:
    workspace = make_workspace(layout, name.removeprefix("ilmar-it-"))
    spec = ContainerSpec(
        image=IMAGE,
        host_workspace=workspace,
        name=name,
        owner=OWNER,
        workspace_identity=identity_of(workspace),
        network=network,
        allowed_hosts=(ALLOWED,) if network is NetworkPolicy.ALLOWLIST else (),
    )
    return runtime, runtime.start(spec)


@pytest.fixture(scope="module")
def allow(
    runtime: PodmanRuntime, layout: WorkspaceLayout, servers: dict[str, EchoServer]
) -> Sandbox:
    return start(runtime, layout, "ilmar-it-allow", NetworkPolicy.ALLOWLIST)


@pytest.fixture(scope="module")
def none(
    runtime: PodmanRuntime, layout: WorkspaceLayout, servers: dict[str, EchoServer]
) -> Sandbox:
    return start(runtime, layout, "ilmar-it-none", NetworkPolicy.NONE)


@pytest.fixture(scope="module")
def proxy_ip(allow: Sandbox) -> str:
    url = inside(allow, ["printenv", "HTTPS_PROXY"]).strip()
    assert url.startswith("http://")
    assert url.endswith(":3128")
    return url.removeprefix("http://").removesuffix(":3128")


def through_proxy(sandbox: Sandbox, proxy: str, request: str) -> str:
    """Send a raw request (``\\r\\n`` escapes allowed) to the proxy; return the reply."""
    return attempt(sandbox, sh('printf "%b" "$1" | nc -w 5 "$2" 3128', request, proxy))


def can_connect(sandbox: Sandbox, host: str, port: int) -> bool:
    out = attempt(
        sandbox,
        sh(f'nc -w 3 "$1" "$2" </dev/null >/dev/null 2>&1 && echo {CONNECTED}', host, str(port)),
    )
    return CONNECTED in out


def proxy_logs() -> str:
    done = podman("logs", "ilmar-it-allow-egress")
    return done.stdout + done.stderr


# -- the only positive case ---------------------------------------------------------------


def test_allowed_host_is_reachable_through_the_proxy(
    allow: Sandbox, proxy_ip: str, servers: dict[str, EchoServer], host_ip: str
) -> None:
    target = servers["tcp443"]
    assert tcp_control(host_ip, 443, target.nonce), "control: the allowed target is down"
    reply = through_proxy(
        allow, proxy_ip, f"CONNECT {ALLOWED}:443 HTTP/1.1\\r\\nHost: {ALLOWED}:443\\r\\n\\r\\n"
    )
    assert "200 Connection established" in reply
    assert target.nonce in reply, "the tunnel to the allowed host did not carry the target's reply"
    assert f"ilmar-egress ALLOW host={ALLOWED}" in proxy_logs()


# -- structure ------------------------------------------------------------------------------


def test_allowlist_sandbox_structure(allow: Sandbox) -> None:
    assert sorted(inside(allow, ["ls", "/sys/class/net"]).split()) == ["eth0", "lo"]
    assert inside(allow, sh("cat /proc/net/if_inet6 2>/dev/null; true")).strip() == ""
    assert inside(allow, sh("cat /proc/sys/net/ipv6/conf/all/disable_ipv6")).strip() == "1"
    assert "nameserver" not in inside(allow, sh("cat /etc/resolv.conf 2>/dev/null; true"))
    hosts = inside(allow, sh("cat /etc/hosts 2>/dev/null; true"))
    assert "host.containers.internal" not in hosts
    assert "host.docker.internal" not in hosts
    status = dict(
        line.split(":", 1) for line in inside(allow, ["cat", "/proc/self/status"]).splitlines()
    )
    assert int(status["CapEff"].strip(), 16) == 0
    assert int(status["CapBnd"].strip(), 16) == 0
    # No ICMP sockets: no CAP_NET_RAW, and unprivileged ping is disabled.
    runtime, handle = allow
    ping = runtime.run(handle, ["ping", "-c", "1", "-W", "2", "127.0.0.1"], timeout_seconds=30)
    assert ping.exit_code != 0
    assert "permission denied" in (ping.stdout + ping.stderr).lower()
    assert not inside(allow, sh("ls /run/podman /var/run/docker.sock 2>/dev/null; true")).strip()


def test_sandbox_address_is_inside_its_internal_network(allow: Sandbox) -> None:
    network = podman(
        "network", "inspect", "ilmar-it-allow-net", "--format", "{{.Internal}} {{.DNSEnabled}}"
    )
    assert network.stdout.split() == ["true", "false"]


# -- direct connections, NONE and ALLOWLIST alike -------------------------------------------------


@pytest.mark.parametrize("policy", ["none", "allow"])
@pytest.mark.parametrize(
    ("key", "udp", "port"),
    [
        ("tcp443", False, 443),  # the allowed host's resolved address, bypassing the proxy
        ("tcp80", False, 80),
        ("udp443", True, 443),  # QUIC
        ("udp53", True, 53),  # a DNS server on the host
        ("tcp53", False, 53),
    ],
)
def test_direct_connections_to_the_host_fail(
    request: pytest.FixtureRequest,
    policy: str,
    key: str,
    udp: bool,
    port: int,
    servers: dict[str, EchoServer],
    host_ip: str,
) -> None:
    sandbox: Sandbox = request.getfixturevalue(policy)
    server = servers[key]
    control = (udp_control if udp else tcp_control)(host_ip, port, server.nonce)
    seen = (sandbox_udp if udp else sandbox_tcp)(sandbox, host_ip, port)
    outcome, reason = classify(control, seen, server.nonce)
    expect_isolated(outcome, reason, f"{policy} {key} {host_ip}", required=True)


# -- proxy misuse -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "request_text", "target"),
    [
        ("ip-literal", "CONNECT {ip}:443 HTTP/1.1\\r\\n\\r\\n", "tcp443"),
        ("wrong-port", f"CONNECT {ALLOWED}:80 HTTP/1.1\\r\\n\\r\\n", "tcp80"),
        (
            "absolute-form",
            f"GET http://{ALLOWED}/ HTTP/1.1\\r\\nHost: {ALLOWED}\\r\\n\\r\\n",
            "tcp80",
        ),
        (
            "host-mismatch",
            f"CONNECT {ALLOWED}:443 HTTP/1.1\\r\\nHost: {UNLISTED}\\r\\n\\r\\n",
            "tcp443",
        ),
        ("unlisted-host", f"CONNECT {UNLISTED}:443 HTTP/1.1\\r\\n\\r\\n", "tcp443"),
        ("ipv6-literal", "CONNECT [::ffff:{ip}]:443 HTTP/1.1\\r\\n\\r\\n", "tcp443"),
    ],
)
def test_proxy_refuses_misuse(
    allow: Sandbox,
    proxy_ip: str,
    servers: dict[str, EchoServer],
    host_ip: str,
    case: str,
    request_text: str,
    target: str,
) -> None:
    server = servers[target]
    control = tcp_control(host_ip, server.port, server.nonce)
    reply = through_proxy(allow, proxy_ip, request_text.replace("{ip}", host_ip))
    outcome, reason = classify(control, reply, server.nonce)
    expect_isolated(outcome, reason, f"proxy {case}", required=True)
    assert " 403 " in reply, f"{case}: the proxy did not refuse explicitly: {reply!r}"


def test_unlisted_names_are_refused_without_resolution(allow: Sandbox, proxy_ip: str) -> None:
    through_proxy(allow, proxy_ip, f"CONNECT {UNLISTED}:443 HTTP/1.1\\r\\n\\r\\n")
    logs = proxy_logs()
    assert f"DENY reason=host_not_listed target={UNLISTED}:443" in logs
    assert f"RESOLVE host={UNLISTED}" not in logs


def test_only_the_proxy_port_is_open(allow: Sandbox, proxy_ip: str) -> None:
    junk = through_proxy(allow, proxy_ip, "NONSENSE\\r\\n\\r\\n")
    assert " 400 " in junk, "control: the proxy port must answer"
    for port in (22, 53, 80, 443, 3129, 8080, 9090):
        assert not can_connect(allow, proxy_ip, port), f"proxy port {port} is reachable"


def test_the_internal_gateway_offers_nothing(allow: Sandbox) -> None:
    gateway = podman(
        "network",
        "inspect",
        "ilmar-it-allow-net",
        "--format",
        "{{range .Subnets}}{{.Gateway}}{{end}}",
    ).stdout.strip()
    assert gateway, "the internal network reports no gateway"
    for port in (22, 53, 80, 443, 3128):
        assert not can_connect(allow, gateway, port), f"gateway {gateway}:{port} is reachable"


# -- neighbours ------------------------------------------------------------------------------------


def test_another_executions_sandbox_is_unreachable(
    allow: Sandbox, runtime: PodmanRuntime, layout: WorkspaceLayout
) -> None:
    second = start(runtime, layout, "ilmar-it-allow2", NetworkPolicy.ALLOWLIST)
    nonce = secrets.token_hex(8)
    listener_errors: list[BaseException] = []

    def listen() -> None:
        try:
            runtime.run(
                second[1],
                sh('for i in 1 2 3; do printf "%s\\n" "$1" | nc -l -p 9000; done', nonce),
                timeout_seconds=40,
            )
        except ProviderError as error:  # the container is stopped at the end
            listener_errors.append(error)

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    try:
        address = podman(
            "inspect",
            "ilmar-it-allow2",
            "--format",
            '{{(index .NetworkSettings.Networks "ilmar-it-allow2-net").IPAddress}}',
        ).stdout.strip()
        # Control: a peer on the second sandbox's own network reaches it.
        control = podman(
            "exec",
            "ilmar-it-allow2-egress",
            "python3",
            "-c",
            "import socket,sys,time\n"
            "for _ in range(20):\n"
            "    try:\n"
            "        s = socket.create_connection((sys.argv[1], 9000), 3); break\n"
            "    except OSError: time.sleep(0.5)\n"
            "print(s.recv(100).decode())",
            address,
            check=False,
        )
        outcome, reason = classify(
            nonce in control.stdout, sandbox_tcp(allow, address, 9000), nonce
        )
        expect_isolated(outcome, reason, f"neighbour {address}:9000", required=True)
    finally:
        runtime.stop(second[1], owner=OWNER)
        thread.join(timeout=60)


# -- optional: outside targets ------------------------------------------------------


def external_control(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


@pytest.mark.parametrize("policy", ["none", "allow"])
@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("1.1.1.1", 53),  # public resolver over TCP
        ("8.8.8.8", 53),
        ("1.1.1.1", 443),  # DNS over HTTPS / a public IP
        ("169.254.169.254", 80),  # cloud metadata
    ],
)
def test_outside_targets_are_unreachable(
    request: pytest.FixtureRequest, policy: str, host: str, port: int
) -> None:
    """Optional: no nonce is possible here, so the control is a host-side TCP connect."""
    sandbox: Sandbox = request.getfixturevalue(policy)
    control = external_control(host, port)
    connected = can_connect(sandbox, host, port)
    if connected:
        pytest.fail(f"{policy} reached {host}:{port}")
    if not control:
        pytest.skip(f"{policy} {host}:{port}: inconclusive (optional): host-side control failed")
