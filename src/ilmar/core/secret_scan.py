# SPDX-License-Identifier: Apache-2.0
"""Scan outgoing content for the deployment's own secrets (#110, ADR-0038).

A :class:`SecretScanner` holds the values of the secrets this process read
(the source-control token, the model credential and any other configured
secret) and looks for them, byte for byte, in a fixed set of encodings:

- raw;
- base64, standard and URL-safe, at each of the three byte alignments;
- hexadecimal, lower and upper case;
- percent-encoding (``quote`` and ``quote_plus``);
- JSON string escaping.

This is a deterministic guard against the plain and common ways a value is
copied into a file or a message. It does **not** detect arbitrary
transformations: a value split, reversed, re-wrapped, compressed or
encrypted passes. Findings name the secret's variable name and the encoding,
never the value.
"""

from __future__ import annotations

import base64
import errno
import json
import os
import stat
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

MIN_SECRET_LENGTH = 8
"""Shorter values match by chance; they are refused when the scanner is built."""

CHUNK_BYTES = 1 << 20
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class SecretTooShortError(ValueError):
    """A configured secret is too short to be scanned for reliably."""


@dataclass(frozen=True, slots=True)
class SecretFinding:
    """Where a secret was found. ``where`` never contains the value.

    ``where`` is a changed path, ``"symlink target of <path>"``, ``"a changed
    path's name"`` or a caller's label such as ``"the change request"``. A path
    whose own name holds a secret is never repeated: :meth:`SecretScanner.shown_path`
    replaces it.
    """

    name: str
    encoding: str
    where: str

    def describe(self) -> str:
        return f"{self.name} ({self.encoding}) in {self.where}"


@dataclass(frozen=True, slots=True)
class _Needle:
    name: str
    encoding: str
    data: bytes


class SecretScanner:
    """Looks for configured secret values in bytes, text and candidate files."""

    __slots__ = ("_longest", "_names", "_needles")

    def __init__(self, secrets: Mapping[str, str]) -> None:
        needles: list[_Needle] = []
        for name, value in sorted(secrets.items()):
            if len(value.encode()) < MIN_SECRET_LENGTH:
                raise SecretTooShortError(
                    f"secret {name} is shorter than {MIN_SECRET_LENGTH} bytes and cannot be "
                    "scanned for reliably"
                )
            seen: set[bytes] = set()
            for encoding, data in _encodings(value.encode()):
                if data and data not in seen:
                    seen.add(data)
                    needles.append(_Needle(name, encoding, data))
        self._names = tuple(sorted(secrets))
        self._needles = tuple(needles)
        self._longest = max((len(n.data) for n in needles), default=0)

    @classmethod
    def empty(cls) -> SecretScanner:
        return cls({})

    def __repr__(self) -> str:  # never the values
        return f"SecretScanner(names={list(self._names)!r})"

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    def __bool__(self) -> bool:
        return bool(self._needles)

    # -- scanning ---------------------------------------------------------------------

    def scan_bytes(self, data: bytes, *, where: str) -> list[SecretFinding]:
        return self._match(data, where)

    def scan_text(self, text: str, *, where: str) -> list[SecretFinding]:
        return self._match(text.encode("utf-8", "surrogateescape"), where)

    def scan_stream(self, chunks: Iterable[bytes], *, where: str) -> list[SecretFinding]:
        """Scan a stream read in chunks; a value split across chunks is still found."""
        found: dict[tuple[str, str], SecretFinding] = {}
        keep = max(self._longest - 1, 0)
        tail = b""
        for chunk in chunks:
            window = tail + chunk
            for finding in self._match(window, where):
                found.setdefault((finding.name, finding.encoding), finding)
            tail = window[-keep:] if keep else b""
        return list(found.values())

    def scan_candidate(self, root: Path, paths: Iterable[str]) -> list[SecretFinding]:
        """Scan the changed ``paths`` of a checkout at ``root``.

        For each path: its name, its symlink target if it is a symlink, and its
        bytes if it is a regular file. Nothing is followed: a path under a
        symlinked directory, or one that no longer exists (a deletion), has no
        content to scan. Directories (such as submodules) are skipped.
        """
        findings: list[SecretFinding] = []
        root_fd = os.open(root, _DIR_FLAGS)
        try:
            for path in paths:
                findings += self.scan_text(path, where="a changed path's name")
                findings += self._scan_entry(root_fd, path)
        finally:
            os.close(root_fd)
        return _unique(findings)

    def shown_path(self, path: str) -> str:
        """``path`` as it may be recorded: itself, or a placeholder naming only the
        variables when the name holds a secret. Classification always uses the real
        path; only what is written down changes."""
        found = self.scan_text(path, where="")
        if not found:
            return path
        return f"[a path withheld: its name holds {', '.join(sorted({f.name for f in found}))}]"

    # -- internals --------------------------------------------------------------------

    def _match(self, data: bytes, where: str) -> list[SecretFinding]:
        return [
            SecretFinding(needle.name, needle.encoding, where)
            for needle in self._needles
            if needle.data in data
        ]

    def _scan_entry(self, root_fd: int, path: str) -> list[SecretFinding]:
        *parents, leaf = path.split("/")
        fd = os.dup(root_fd)
        try:
            for part in parents:
                try:
                    child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                        return []  # deleted, or under a symlink: nothing here to read
                    raise
                os.close(fd)
                fd = child
            try:
                info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return []  # deleted by the candidate
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(leaf, dir_fd=fd)
                return self.scan_text(target, where=f"symlink target of {self.shown_path(path)}")
            if not stat.S_ISREG(info.st_mode):
                return []
            file_fd = os.open(leaf, _FILE_FLAGS, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as handle:
                return self.scan_stream(_chunks(handle.read), where=self.shown_path(path))
        finally:
            os.close(fd)


def _chunks(read: Callable[[int], bytes]) -> Iterator[bytes]:
    while True:
        chunk = read(CHUNK_BYTES)
        if not chunk:
            return
        yield chunk


def _unique(findings: list[SecretFinding]) -> list[SecretFinding]:
    return list(dict.fromkeys(findings))


def _encodings(value: bytes) -> Iterator[tuple[str, bytes]]:
    yield "raw", value
    for label, encode in (("base64", base64.b64encode), ("base64url", base64.urlsafe_b64encode)):
        for offset in range(3):
            yield label, _aligned(encode, value, offset)
    yield "hex", value.hex().encode()
    yield "hex", value.hex().upper().encode()
    text = value.decode("utf-8", "surrogateescape")
    yield "percent-encoded", urllib.parse.quote(text, safe="").encode()
    yield "percent-encoded", urllib.parse.quote_plus(text, safe="").encode()
    yield "json-escaped", json.dumps(text)[1:-1].encode()
    yield "json-escaped", json.dumps(text, ensure_ascii=False)[1:-1].encode()


def _aligned(encode: Callable[[bytes], bytes], value: bytes, offset: int) -> bytes:
    """The base64 characters that depend only on ``value`` when it starts
    ``offset`` bytes into a 3-byte group: present in any base64 text that
    encodes ``value`` at that alignment, whatever surrounds it."""
    encoded = encode(b"\0" * offset + value)
    first = -(-8 * offset // 6)  # ceil: the first character with no prefix bits
    last = (8 * (offset + len(value)) - 6) // 6  # the last with no suffix bits
    return encoded[first : last + 1]
