# SPDX-License-Identifier: Apache-2.0
"""Move file trees between plain directories and git objects, without git touching them.

Workspaces written by agents are *data* (ADR-0010 §1): they are read here in
Python, one component at a time through directory file descriptors opened
with ``O_NOFOLLOW``, and streamed into ``git fast-import``. No working tree,
attributes, filters, hooks or configuration from the workspace are ever
involved. The reverse direction (``materialise``) writes a commit's tree into
a fresh directory the same way, from ``ls-tree`` and ``cat-file`` output,
never through ``checkout`` or ``archive`` (which honour attributes).
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ilmar.adapters.git.runner import HardenedGit
from ilmar.domain.errors import CandidateRejectedError, ProviderError

MODE_FILE = b"100644"
MODE_EXECUTABLE = b"100755"
MODE_SYMLINK = b"120000"
MODE_GITLINK = b"160000"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True, slots=True)
class Entry:
    path: bytes  # relative, "/"-separated
    mode: bytes
    content: bytes  # file bytes, or the link target for symlinks


@dataclass(frozen=True, slots=True)
class Identity:
    name: str
    email: str


# -- reading a workspace ----------------------------------------------------------


def read_workspace(root: Path) -> list[Entry]:
    """Every file and symlink under ``root``, sorted; the top-level ``.git`` is ignored.

    Raises ``CandidateRejectedError`` for a nested repository (a ``.git``
    entry below the top level, in any letter case), a special file, or a name
    that is not valid UTF-8 or that git cannot store.
    """
    try:
        fd = os.open(root, _DIR_FLAGS)
    except OSError as error:
        raise ProviderError(f"cannot open workspace {root}: {error.strerror}") from None
    entries: list[Entry] = []
    try:
        _walk(fd, b"", entries, top=True)
    finally:
        os.close(fd)
    return sorted(entries, key=lambda e: e.path)


def _walk(fd: int, prefix: bytes, entries: list[Entry], *, top: bool) -> None:
    for name in sorted(os.listdir(fd)):
        raw = os.fsencode(name)
        path = prefix + raw
        if raw.lower() == b".git":
            if top and raw == b".git":
                continue  # the agent's own repository metadata: not content
            raise CandidateRejectedError(f"nested repository at {_show(path)}")
        _check_name(raw, path)
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            try:
                _walk(child, path + b"/", entries, top=False)
            finally:
                os.close(child)
        elif stat.S_ISLNK(info.st_mode):
            target = os.fsencode(os.readlink(name, dir_fd=fd))
            entries.append(Entry(path, MODE_SYMLINK, target))
        elif stat.S_ISREG(info.st_mode):
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as handle:
                data = handle.read()
            mode = MODE_EXECUTABLE if info.st_mode & stat.S_IXUSR else MODE_FILE
            entries.append(Entry(path, mode, data))
        else:
            raise CandidateRejectedError(f"special file at {_show(path)}")


def _check_name(raw: bytes, path: bytes) -> None:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CandidateRejectedError(f"file name is not UTF-8: {_show(path)}") from None
    if b"\n" in raw or b"\0" in raw:
        raise CandidateRejectedError(f"file name git cannot store: {_show(path)}")


def _show(path: bytes) -> str:
    return path.decode("utf-8", errors="backslashreplace")[:200]


# -- writing a commit ----------------------------------------------------------------


def commit_tree(
    git: HardenedGit,
    entries: list[Entry],
    *,
    parent: str,
    message: str,
    identity: Identity,
    timestamp: int,
) -> str:
    """Create a commit with exactly ``entries`` on top of ``parent``; return its id.

    The commit is deterministic: the same entries, parent, message, identity
    and timestamp always give the same id.
    """
    who = f"{identity.name} <{identity.email}> {timestamp} +0000".encode()
    payload = message.encode()
    with tempfile.TemporaryDirectory(dir=git.git_dir) as scratch:
        marks = Path(scratch) / "marks"
        ref = b"refs/ilmar/freeze/" + os.urandom(8).hex().encode()
        stream = b"".join(_fast_import_stream(entries, ref, who, payload, parent))
        git.run(
            "fast-import",
            "--quiet",
            "--date-format=raw",
            f"--export-marks={marks}",
            input=stream,
        )
        git.run("update-ref", "-d", ref.decode())
        for line in marks.read_text().splitlines():
            mark, _, commit = line.partition(" ")
            if mark == ":1":
                return commit
    raise ProviderError("git fast-import did not report the commit")


def _fast_import_stream(
    entries: list[Entry], ref: bytes, who: bytes, message: bytes, parent: str
) -> Iterator[bytes]:
    yield b"commit " + ref + b"\nmark :1\n"
    yield b"author " + who + b"\ncommitter " + who + b"\n"
    yield b"data " + str(len(message)).encode() + b"\n" + message + b"\n"
    yield b"from " + parent.encode() + b"\n"
    yield b"deleteall\n"
    for entry in entries:
        yield b"M " + entry.mode + b" inline " + _quote(entry.path) + b"\n"
        yield b"data " + str(len(entry.content)).encode() + b"\n" + entry.content + b"\n"
    yield b"\n"


def _quote(path: bytes) -> bytes:
    """C-style quoting, which fast-import always accepts."""
    out = bytearray(b'"')
    for byte in path:
        if byte in (0x22, 0x5C):
            out += b"\\" + bytes([byte])
        elif byte < 0x20 or byte >= 0x7F:
            out += b"\\%03o" % byte
        else:
            out.append(byte)
    return bytes(out + b'"')


# -- reading a commit ------------------------------------------------------------------


def tree_entries(git: HardenedGit, revision: str) -> list[Entry]:
    """The files and symlinks of ``revision``'s tree, with their content."""
    listing = git.run("ls-tree", "-r", "-z", "--full-tree", revision).stdout
    records: list[tuple[bytes, bytes, bytes]] = []
    for record in filter(None, listing.split(b"\0")):
        meta, _, path = record.partition(b"\t")
        mode, kind, oid = meta.split(b" ")
        if mode == MODE_GITLINK or kind != b"blob":
            raise ProviderError(f"{revision} contains a submodule at {_show(path)}")
        if mode not in (MODE_FILE, MODE_EXECUTABLE, MODE_SYMLINK):
            raise ProviderError(f"unsupported mode {mode.decode()} at {_show(path)}")
        _check_tree_path(path)
        records.append((path, mode, oid))
    contents = _read_blobs(git, [oid for _, _, oid in records])
    return [Entry(path, mode, contents[oid]) for path, mode, oid in records]


def _check_tree_path(path: bytes) -> None:
    for part in path.split(b"/"):
        if part in (b"", b".", b"..") or part.lower() == b".git":
            raise ProviderError(f"unsafe path in tree: {_show(path)}")


def _read_blobs(git: HardenedGit, oids: list[bytes]) -> dict[bytes, bytes]:
    if not oids:
        return {}
    unique = list(dict.fromkeys(oids))
    output = git.run("cat-file", "--batch", input=b"\n".join(unique) + b"\n").stdout
    contents: dict[bytes, bytes] = {}
    position = 0
    for oid in unique:
        header_end = output.index(b"\n", position)
        header = output[position:header_end].split(b" ")
        if len(header) != 3 or header[0] != oid or header[1] != b"blob":
            raise ProviderError(f"unexpected cat-file output for {oid.decode()}")
        size = int(header[2])
        start = header_end + 1
        contents[oid] = output[start : start + size]
        position = start + size + 1
    return contents


# -- writing a directory -----------------------------------------------------------------


def materialise(entries: list[Entry], destination: Path) -> None:
    """Write ``entries`` into ``destination``, which must not exist.

    Every component is created relative to a directory descriptor with
    ``O_NOFOLLOW`` and ``O_EXCL``, so nothing is ever written through a
    symlink, including one created earlier from the same tree.
    """
    try:
        os.mkdir(destination, 0o755)
    except FileExistsError:
        raise ProviderError(f"destination already exists: {destination}") from None
    except OSError as error:
        raise ProviderError(f"cannot create {destination}: {error.strerror}") from None
    root = os.open(destination, _DIR_FLAGS)
    try:
        for entry in entries:
            _check_tree_path(entry.path)
            *dirs, leaf = entry.path.split(b"/")
            fd = root
            opened: list[int] = []
            try:
                for part in dirs:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, 0o755, dir_fd=fd)
                    fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                    opened.append(fd)
                _write_leaf(fd, leaf, entry)
            finally:
                for handle in reversed(opened):
                    os.close(handle)
    finally:
        os.close(root)


def _write_leaf(fd: int, leaf: bytes, entry: Entry) -> None:
    if entry.mode == MODE_SYMLINK:
        os.symlink(entry.content, leaf, dir_fd=fd)
        return
    mode = 0o755 if entry.mode == MODE_EXECUTABLE else 0o644
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    file_fd = os.open(leaf, flags, mode, dir_fd=fd)
    with os.fdopen(file_fd, "wb") as handle:
        os.fchmod(handle.fileno(), mode)  # independent of the process umask
        handle.write(entry.content)
