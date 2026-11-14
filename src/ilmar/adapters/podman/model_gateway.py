# SPDX-License-Identifier: Apache-2.0
"""Ilmar's model gateway: the sandbox's only route to the model (ADR-0037 §1).

This file runs on its own inside the gateway sidecar container
(``python3 -I model_gateway.py …``), so it uses only the standard library and
never imports ``ilmar``. The Podman adapter mounts it read-only.

What it does, for each request from the sandbox:

* serves only clients on the sandbox's private network;
* accepts only ``POST /v1/messages`` and ``POST /v1/messages/count_tokens``
  (optionally ``?beta=true``), with a ``Content-Length`` of at most
  ``--max-request-bytes`` and no other transfer encoding;
* accepts only the per-attempt throwaway token (``x-api-key`` or
  ``Authorization: Bearer``), compared by SHA-256 digest in constant time. The
  gateway is given the digest, never the token;
* forwards an allow-listed set of headers, drops every credential the sandbox
  sent, and adds the real key, which it holds only in memory. It receives the
  key through a FIFO in its own tmpfs (``--key-fifo``): the coordinator writes
  it there with ``podman exec`` on standard input, so the key never exists in
  a file, on the host or in the container, at any moment;
* allows at most ``--max-in-flight`` requests at once;
* admits each request against the attempt's budget (``gateway_admission``,
  loaded from beside this file): strict request shapes, every
  ``anthropic-beta`` header checked, ``max_tokens`` lowered to what the budget
  and the policy allow, and a reservation taken before anything is sent;
* streams the response back unchanged, with an allow-listed set of headers,
  meters the usage it reports, settles the reservation, then closes the
  connection. A report that is missing, incomplete or not understood, an
  error response, or a provider that could not be reached charges the whole
  reservation and halts the attempt: nothing more is forwarded.

Everything refused is answered here in the provider's error shape, and
nothing reaches the provider. The log has one line per request (method, path,
status, sizes) and, for a metered request, one ``CHARGE`` line (micro-dollars,
reason, halt); never a header value, a body or a credential.

Its log is the attempt's spending record (ADR-0039): one ``READY`` line when
it starts serving, an ``ADMIT`` line before a metered request may reach the
provider and a ``CHARGE`` line when it is settled. On ``SIGUSR1`` it closes:
it admits nothing more, waits up to ``--drain-seconds`` for requests in
flight, then writes one ``CLOSED`` line with its own totals, so the
coordinator can tell a complete record from an empty or cut-off one.
"""

from __future__ import annotations

import argparse
import email.message
import hashlib
import hmac
import http.client
import importlib.util
import ipaddress
import json
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:  # for the type checker only: at run time it is loaded from a file
    from ilmar.adapters.podman import gateway_admission as ga

PATHS = frozenset({"/v1/messages", "/v1/messages/count_tokens"})
QUERIES = frozenset({"", "beta=true"})
REQUEST_HEADERS = frozenset({"content-type", "accept", "anthropic-version", "user-agent"})
"""Forwarded as sent, along with ``x-stainless-*`` (SDK metadata). Nothing else.
``anthropic-beta`` is rebuilt from the values admission kept."""
MESSAGES = "/v1/messages"
_ADMIT = re.compile(r"ADMIT ticket=(\d{1,12}) reservation=(\d{1,16})")
_CHARGE = re.compile(
    r"CHARGE ticket=(\d{1,12}) charged=(\d{1,16}) reservation=(\d{1,16}) "
    r"overrun=(\d{1,16}) reason=([A-Za-z0-9.:_-]{1,80}) spent=\d{1,16} halted=(true|false)"
)
READY = "ilmar-gateway READY"
_CLOSED = re.compile(
    r"CLOSED admitted=(\d{1,12}) settled=(\d{1,12}) charged=(\d{1,16}) open=(\d{1,12})"
)


def parse_spend(lines: list[str]) -> dict[str, Any]:
    """What this gateway's own log says an attempt's model requests cost.

    Read by the runtime from the sidecar's log (ADR-0039). ``READY``,
    ``ADMIT``, ``CHARGE`` and ``CLOSED`` lines count; every other line is
    ignored. The record is *not trusted* (``ValueError``) when:

    * a line starts like one of those but does not parse;
    * ``READY`` appears more than once (the gateway restarted and lost its
      state) or an ``ADMIT`` comes before it;
    * a ticket is admitted twice, or charged without its admission;
    * a ``CLOSED`` line's totals differ from the lines before it (lines are
      missing), it appears twice, or an ``ADMIT`` follows it.

    ``closed`` says whether the final record is there. Without it the record
    may be incomplete, and the caller must not take it as the whole cost.
    ``uncertain`` is the first reason the true cost is unknown even so: a
    settlement that was not a trusted report, or a request admitted but never
    settled. A ``CHARGE`` after ``CLOSED`` (a request that outlasted the drain)
    still counts."""
    admitted: dict[int, int] = {}
    charged = overrun = settled = admits = 0
    ready = closed = False
    left_open = 0
    uncertain: str | None = None
    for line in lines:
        if line.startswith("ilmar-gateway READY"):
            if line != READY or ready:
                raise ValueError("the gateway started more than once: its record is incomplete")
            ready = True
        elif line.startswith("ADMIT"):
            match = _ADMIT.fullmatch(line)
            if match is None or int(match[1]) in admitted or not ready or closed:
                raise ValueError("a gateway ADMIT line is not understood")
            admitted[int(match[1])] = int(match[2])
            admits += 1
        elif line.startswith("CHARGE"):
            match = _CHARGE.fullmatch(line)
            if match is None or admitted.pop(int(match[1]), None) is None:
                raise ValueError("a gateway CHARGE line is not understood")
            settled += 1
            charged += int(match[2])
            overrun += int(match[4])
            if match[5] != "reported" and uncertain is None:
                uncertain = f"a report the gateway did not trust ({match[5]})"
        elif line.startswith("CLOSED"):
            match = _CLOSED.fullmatch(line)
            if match is None or closed or not ready:
                raise ValueError("a gateway CLOSED line is not understood")
            if (int(match[1]), int(match[2]), int(match[3])) != (admits, settled, charged):
                raise ValueError("the gateway's record is missing lines")
            closed, left_open = True, int(match[4])
    if left_open and uncertain is None:
        uncertain = f"{left_open} request(s) still in flight when the gateway closed"
    if admitted and uncertain is None:
        uncertain = f"{len(admitted)} request(s) admitted but never settled"
    return {
        "requests": settled + len(admitted), "charged": charged,
        "unsettled": sum(admitted.values()), "overrun": overrun, "uncertain": uncertain,
        "closed": closed,
    }  # fmt: skip


ADMISSION_MODULE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway_admission.py")


def load_admission(path: str = ADMISSION_MODULE) -> ModuleType:
    """The admission module, loaded from its file: this script never imports
    ``ilmar``, so the sidecar needs only the two files."""
    spec = importlib.util.spec_from_file_location("ilmar_gateway_admission", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load the admission module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


RESPONSE_HEADERS = frozenset(
    {"content-type", "request-id", "retry-after", "x-should-retry", "cache-control"}
)
"""Returned as received, along with ``anthropic-*``. Nothing else."""
CREDENTIAL_HEADERS = ("x-api-key", "authorization")
UPSTREAM_TIMEOUT = 600.0
CHUNK = 65536
_TOKEN_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PRINTABLE = re.compile(r"[^\x21-\x7e]")
_REASON = re.compile(r"[^A-Za-z0-9.:-]")


@dataclass(frozen=True, slots=True)
class Upstream:
    scheme: str
    host: str
    port: int

    @classmethod
    def parse(cls, value: str, *, allow_plain_http: bool) -> Upstream:
        parts = urllib.parse.urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("the upstream must be an http(s) URL with a host")
        if parts.scheme == "http" and not allow_plain_http:
            raise ValueError("the upstream must use https")
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise ValueError("the upstream is a scheme, host and port only")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return cls(parts.scheme, parts.hostname, port)


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    token_sha256: str
    key: str
    upstream: Upstream
    clients: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    policy: str
    """The admission policy, as JSON (prices and limits; no secret)."""
    budget: int
    """The attempt's model budget ``B``, in micro-dollars."""
    max_request_bytes: int = 4 * 1024 * 1024
    max_in_flight: int = 4

    def __post_init__(self) -> None:
        if not _TOKEN_DIGEST.match(self.token_sha256):
            raise ValueError("token_sha256 must be 64 lower-case hex digits")
        if not self.key or any(c in self.key for c in "\r\n\0"):
            raise ValueError("the key must be one non-empty line")
        if not self.clients:
            raise ValueError("at least one client network is needed")
        if self.max_request_bytes < 1 or self.max_in_flight < 1 or self.budget < 1:
            raise ValueError("limits must be positive")

    def __repr__(self) -> str:  # never the key
        return f"GatewayConfig(upstream={self.upstream!r}, clients={self.clients!r})"


class Gateway:
    """The server. ``serve_forever`` until ``shutdown``."""

    def __init__(
        self,
        listen: tuple[str, int],
        config: GatewayConfig,
        *,
        log: IO[str] | None = None,
        admission: ModuleType | None = None,
    ) -> None:
        self.config = config
        self.admission: Any = admission if admission is not None else load_admission()
        self.budget: ga.Budget = self.admission.Budget(
            limit=config.budget, policy=self.admission.policy_from_json(config.policy)
        )
        self._log = log if log is not None else sys.stdout
        self._log_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(config.max_in_flight)
        # ADR-0039: admission and closing exclude each other, so no request is
        # admitted once closing has begun, and the totals match the log.
        self._state = threading.Condition()
        self._closing = False
        self._open: set[int] = set()
        self._admits = self._settled = self._charged = 0
        gateway = self

        class Handler(_Handler):
            owner = gateway

        self._server = ThreadingHTTPServer(listen, Handler)
        self._server.daemon_threads = True

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def serve_forever(self) -> None:
        self.log(READY)
        self._server.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def log(self, line: str) -> None:
        with self._log_lock:
            self._log.write(line + "\n")
            self._log.flush()

    def client_allowed(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in network for network in self.config.clients)

    def authenticated(self, headers: email.message.Message) -> bool:
        offered: list[str] = []
        if (key := headers.get("x-api-key")) is not None:
            offered.append(key)
        if (authorization := headers.get("authorization")) is not None:
            scheme, _, token = authorization.partition(" ")
            offered.append(token if scheme.lower() == "bearer" else "")
        if not offered or len(headers.get_all("x-api-key") or []) > 1:
            return False
        expected = self.config.token_sha256.encode()
        return all(
            hmac.compare_digest(hashlib.sha256(o.encode()).hexdigest().encode(), expected)
            for o in offered
        )

    def try_acquire(self) -> bool:
        return self._slots.acquire(blocking=False)

    @property
    def closing(self) -> bool:
        with self._state:
            return self._closing

    def record_admit(self, ticket: int, reservation: int) -> bool:
        """Put a metered request on record, unless the gateway is closing."""
        with self._state:
            if self._closing:
                return False
            self._open.add(ticket)
            self._admits += 1
            self.log(f"ADMIT ticket={ticket} reservation={reservation}")
            return True

    def record_charge(self, ticket: int, charged: int, line: str) -> None:
        with self._state:
            self._settled += 1
            self._charged += charged
            self.log(line)

    def finished(self, ticket: int) -> None:
        """The request's handling has ended, charged or not."""
        with self._state:
            self._open.discard(ticket)
            self._state.notify_all()

    def close(self, drain: float, *, clock: Any = time.monotonic) -> None:
        """Admit nothing more; wait up to ``drain`` seconds for requests in flight;
        then write the ``CLOSED`` line, once. A second call does nothing."""
        with self._state:
            if self._closing:
                return
            self._closing = True
            deadline = clock() + drain
            while self._open and (left := deadline - clock()) > 0:
                self._state.wait(min(left, 0.2))
            self.log(
                f"CLOSED admitted={self._admits} settled={self._settled} "
                f"charged={self._charged} open={len(self._open)}"
            )

    def release(self) -> None:
        self._slots.release()


class _Handler(BaseHTTPRequestHandler):
    owner: Gateway
    protocol_version = "HTTP/1.1"
    server_version = "ilmar-gateway"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        """Replaced by one line per request in ``_finish``."""

    def do_GET(self) -> None:
        self._refuse(405, "invalid_request_error", "method not allowed")

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_GET  # noqa: N815

    def do_POST(self) -> None:
        gateway = self.owner
        config = gateway.config
        if not gateway.client_allowed(self.client_address[0]):
            return self._refuse(403, "permission_error", "client not allowed")
        path, _, query = self.path.partition("?")
        if path not in PATHS or query not in QUERIES:
            return self._refuse(404, "not_found_error", "not served by this gateway")
        if not gateway.authenticated(self.headers):
            return self._refuse(401, "authentication_error", "invalid x-api-key")
        if self.headers.get("transfer-encoding") is not None:
            return self._refuse(400, "invalid_request_error", "chunked bodies are refused")
        try:
            length = int(self.headers.get("content-length", ""))
        except ValueError:
            return self._refuse(411, "invalid_request_error", "content-length required")
        if length < 0 or len(self.headers.get_all("content-length") or []) != 1:
            return self._refuse(400, "invalid_request_error", "bad content-length")
        if length > config.max_request_bytes:
            return self._refuse(413, "request_too_large", "request body too large")
        body = self.rfile.read(length)
        if len(body) != length:
            return self._refuse(400, "invalid_request_error", "incomplete body")
        if gateway.closing:
            return self._refuse(503, "overloaded_error", "the gateway is closed")
        betas = self.headers.get_all("anthropic-beta") or []
        admission = gateway.admission
        ticket: int | None = None
        try:
            if path == MESSAGES:
                ticket, admitted = gateway.budget.admit(body, betas)
            else:  # counting tokens: checked the same way, not billed, nothing reserved
                admitted = gateway.budget.check(body, betas)
        except admission.RequestRefused as refused:
            return self._refuse(refused.status, refused.kind, str(refused))
        if not gateway.try_acquire():
            if ticket is not None:
                gateway.budget.release(ticket)
            return self._refuse(429, "rate_limit_error", "too many requests in flight")
        if ticket is not None and not gateway.record_admit(ticket, admitted.reservation):
            gateway.budget.release(ticket)  # closed meanwhile: never sent, never on record
            gateway.release()
            return self._refuse(503, "overloaded_error", "the gateway is closed")
        try:  # from here on it may reach the provider: on record
            self._forward(path, query, admitted, ticket)
        finally:
            gateway.release()
            if ticket is not None:
                gateway.finished(ticket)

    # -- forwarding -------------------------------------------------------------------

    def _forward(self, path: str, query: str, admitted: ga.Admitted, ticket: int | None) -> None:
        upstream = self.owner.config.upstream
        body = admitted.body
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() in REQUEST_HEADERS or name.lower().startswith("x-stainless-")
        }
        if admitted.betas:
            headers["anthropic-beta"] = ",".join(admitted.betas)
        headers["x-api-key"] = self.owner.config.key
        headers["content-length"] = str(len(body))
        headers["host"] = (
            upstream.host if upstream.port in (80, 443) else (f"{upstream.host}:{upstream.port}")
        )
        target = path + ("?" + query if query else "")
        meter = self.owner.admission.UsageMeter(streamed=admitted.streamed)
        connection: http.client.HTTPConnection
        if upstream.scheme == "https":
            connection = http.client.HTTPSConnection(
                upstream.host, upstream.port, timeout=UPSTREAM_TIMEOUT,
                context=ssl.create_default_context(),
            )  # fmt: skip
        else:
            connection = http.client.HTTPConnection(
                upstream.host, upstream.port, timeout=UPSTREAM_TIMEOUT
            )
        try:
            try:
                connection.request("POST", target, body=body, headers=headers)
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                # It may have reached the provider: billing unknown, so the whole
                # reservation is charged and the attempt halts.
                self._settle(ticket, admitted, meter, "provider unreachable")
                return self._refuse(502, "api_error", "the provider could not be reached")
            if response.status != 200:
                # Any other status, whatever its body says, is not a billing report
                # the gateway trusts: the whole reservation, and the attempt halts.
                meter.reject(f"provider status {response.status}")
            self.send_response(response.status)
            for name, value in response.getheaders():
                lower = name.lower()
                if lower in RESPONSE_HEADERS or lower.startswith("anthropic-"):
                    self.send_header(name, value)
            self.send_header("connection", "close")
            self.end_headers()
            sent = 0
            try:
                while chunk := response.read1(CHUNK):
                    meter.feed(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    sent += len(chunk)
            except (OSError, http.client.HTTPException):
                self.close_connection = True  # the client sees a cut-off stream
                meter.reject("stream cut off")
            meter.finish()
            counting_failed = ticket is None and response.status != 200
            failure = f"provider status {response.status}" if counting_failed else None
            self._settle(ticket, admitted, meter, failure)
            self._finish(response.status, len(body), sent, dropped=admitted.dropped,
                         capped=admitted.capped_from)  # fmt: skip
        finally:
            connection.close()
            self.close_connection = True

    def _settle(
        self, ticket: int | None, admitted: ga.Admitted, meter: ga.UsageMeter, failure: str | None
    ) -> None:
        """Charge a metered request and log it. A counting request is not billed;
        its provider failing, or being unreachable, still halts the attempt."""
        gateway = self.owner
        if ticket is None:
            if failure is not None:
                gateway.budget.halt(failure)
                reason = _REASON.sub("_", failure)[:80]
                gateway.log(f"HALT counting reason={reason} charged=0 halted=true")
            return
        admission = gateway.admission
        reservation = admitted.reservation
        if failure is not None:
            settled = admission.Settled(reservation, reservation, failure)
        else:
            price = gateway.budget.policy.prices[admitted.model]
            settled = admission.charge(meter, price, reservation)
        gateway.budget.settle(ticket, settled)
        reason = _REASON.sub("_", settled.reason)[:80]
        gateway.record_charge(
            ticket, settled.charged,
            f"CHARGE ticket={ticket} charged={settled.charged} reservation={reservation} "
            f"overrun={settled.overrun} reason={reason} "
            f"spent={gateway.budget.spent()} halted={str(gateway.budget.halted).lower()}",
        )  # fmt: skip

    def _refuse(self, status: int, kind: str, message: str) -> None:
        payload = json.dumps(
            {"type": "error", "error": {"type": kind, "message": message}}
        ).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True
        # Why, in this gateway's own words (field and feature names, no values),
        # so a compatibility gap can be found from the log alone.
        self._finish(status, 0, len(payload), refused=kind, why=_REASON.sub("_", message)[:100])

    def _finish(
        self,
        status: int,
        received: int,
        sent: int,
        refused: str = "",
        *,
        dropped: tuple[str, ...] = (),
        capped: int | None = None,
        why: str = "",
    ) -> None:
        path = _PRINTABLE.sub("?", self.path.partition("?")[0])[:100]
        line = f"REQUEST {self.command} {path} {status} in={received} out={sent}"
        if refused:
            line += f" refused={refused}" + (f" why={why}" if why else "")
        if capped is not None:
            line += f" max_tokens_from={capped}"
        if dropped:  # names only, already validated as [a-z0-9.-]
            line += " dropped_betas=" + ",".join(dropped)
        self.owner.log(line)


DRAIN_SECONDS = 20.0
"""How long a closing gateway waits for requests in flight (ADR-0039)."""
AWAITING_KEY = "ilmar-gateway AWAITING-KEY"
_MAX_KEY_BYTES = 4096


def read_key(path: str) -> str:
    """Read the key handed over through the FIFO at ``path``, then remove it.

    The FIFO is created here (mode 0600) and the gateway announces
    ``AWAITING_KEY``; opening it blocks until the coordinator's writer opens the
    other end. A pipe holds data only in the kernel's buffer: nothing is ever
    written to a file system."""
    os.mkfifo(path, 0o600)
    try:
        print(AWAITING_KEY, flush=True)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(fd, 1024):
                size += len(chunk)
                if size > _MAX_KEY_BYTES:
                    raise ValueError("the key is too long")
                chunks.append(chunk)
        finally:
            os.close(fd)
    finally:
        os.unlink(path)
    key = b"".join(chunks).decode("utf-8").strip()
    if not key or any(c in key for c in "\r\n\0"):
        raise ValueError("the key must be one non-empty line")
    return key


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--listen", default="0.0.0.0:8080")
    parser.add_argument("--token-sha256", required=True)
    parser.add_argument("--key-fifo", required=True)
    parser.add_argument("--upstream", default="https://api.anthropic.com")
    parser.add_argument("--allow-plain-http-upstream", action="store_true")
    parser.add_argument("--clients", required=True, help="comma-separated client networks")
    parser.add_argument("--max-request-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--max-in-flight", type=int, default=4)
    parser.add_argument("--policy", required=True, help="the admission policy, as JSON")
    parser.add_argument("--budget", type=int, required=True, help="B, in micro-dollars")
    parser.add_argument("--admission-module", default=ADMISSION_MODULE)
    parser.add_argument("--drain-seconds", type=float, default=DRAIN_SECONDS)
    args = parser.parse_args(argv)
    # ADR-0039: SIGUSR1 closes the gateway. Handled from the start, so an early
    # signal never ends the process (its default action) and is never lost.
    closer: list[Gateway] = []
    asked = threading.Event()

    def on_close(signum: int, frame: object) -> None:
        asked.set()
        for gateway in closer:
            threading.Thread(target=gateway.close, args=(args.drain_seconds,), daemon=True).start()

    signal.signal(signal.SIGUSR1, on_close)
    admission = load_admission(args.admission_module)
    admission.policy_from_json(args.policy)  # refuse a bad policy before taking the key
    config = GatewayConfig(
        token_sha256=args.token_sha256,
        key=read_key(args.key_fifo),
        upstream=Upstream.parse(args.upstream, allow_plain_http=args.allow_plain_http_upstream),
        clients=tuple(ipaddress.ip_network(n, strict=False) for n in args.clients.split(",")),
        policy=args.policy,
        budget=args.budget,
        max_request_bytes=args.max_request_bytes,
        max_in_flight=args.max_in_flight,
    )
    host, _, port = args.listen.rpartition(":")
    socket.setdefaulttimeout(None)
    gateway = Gateway((host, int(port)), config, admission=admission)
    closer.append(gateway)
    if asked.is_set():
        threading.Thread(target=gateway.close, args=(args.drain_seconds,), daemon=True).start()
    gateway.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
