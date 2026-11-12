# SPDX-License-Identifier: Apache-2.0
"""The candidate secret scan (#110, ADR-0038).

The scanner looks for the deployment's own secret values in a fixed set of
plain encodings. These tests pin each encoding, the chunk boundaries, what is
and is not followed on disk, and that no finding or representation ever holds
the value. They do not, and cannot, show that an arbitrary transformation of a
secret is found: it is not.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
from pathlib import Path

import pytest

from ilmar.core import secret_scan
from ilmar.core.secret_scan import MIN_SECRET_LENGTH, SecretScanner, SecretTooShortError

KEY = "sk-ant-api03-scan/test+value=Q"
"""Has ``/``, ``+`` and ``=`` so percent-encoding and JSON escaping differ from raw."""
TOKEN = "ghp_secretscanTOKEN0123456789"
SCANNER = SecretScanner({"ANTHROPIC_API_KEY": KEY, "GITHUB_TOKEN": TOKEN})


def names(data: bytes) -> set[tuple[str, str]]:
    return {(f.name, f.encoding) for f in SCANNER.scan_bytes(data, where="w")}


def test_the_raw_value_is_found_in_surrounding_text() -> None:
    assert names(f"config = '{KEY}'\n".encode()) == {("ANTHROPIC_API_KEY", "raw")}


@pytest.mark.parametrize("prefix", [b"", b"a", b"ab", b"abc", b"abcd"])
@pytest.mark.parametrize(
    ("encode", "label"),
    [(base64.b64encode, "base64"), (base64.urlsafe_b64encode, "base64url")],
)
def test_base64_is_found_at_every_alignment(prefix: bytes, encode: object, label: str) -> None:
    encoded = encode(prefix + KEY.encode() + b"-and-more")  # type: ignore[operator]
    found = names(b"x = '" + encoded + b"'")
    # Where the two alphabets agree on the value's characters, it is reported as base64.
    assert {("ANTHROPIC_API_KEY", label), ("ANTHROPIC_API_KEY", "base64")} & found


def test_url_safe_base64_is_reported_as_such_where_it_differs() -> None:
    value = "\xff\xfe\xfd-binary-ish-secret-\xfb\xff"  # encodes to ``-`` and ``_``
    scanner = SecretScanner({"K": value})
    encoded = base64.urlsafe_b64encode(value.encode())
    assert encoded != base64.b64encode(value.encode())
    found = scanner.scan_bytes(encoded, where="w")
    assert [(f.name, f.encoding) for f in found] == [("K", "base64url")]


def test_unpadded_base64_is_found() -> None:
    encoded = base64.b64encode(TOKEN.encode()).rstrip(b"=")
    assert ("GITHUB_TOKEN", "base64") in names(encoded)


@pytest.mark.parametrize("upper", [False, True])
def test_hex_is_found_in_either_case(upper: bool) -> None:
    text = KEY.encode().hex()
    assert ("ANTHROPIC_API_KEY", "hex") in names((text.upper() if upper else text).encode())


@pytest.mark.parametrize("quote", [urllib.parse.quote, urllib.parse.quote_plus])
def test_percent_encoding_is_found(quote: object) -> None:
    encoded = quote(KEY, safe="")  # type: ignore[operator]
    assert encoded != KEY
    assert ("ANTHROPIC_API_KEY", "percent-encoded") in names(f"?k={encoded}&x=1".encode())


def test_json_escaping_is_found() -> None:
    value = 'sk-ant-quote"back\\slash/end'
    scanner = SecretScanner({"K": value})
    text = json.dumps({"key": value})
    assert value not in text
    found = scanner.scan_text(text, where="w")
    assert [(f.name, f.encoding) for f in found] == [("K", "json-escaped")]


def test_other_text_and_a_near_miss_are_not_findings() -> None:
    assert names(b"nothing to see here") == set()
    assert names(KEY[:-1].encode()) == set()


def test_a_value_split_across_chunks_is_found() -> None:
    data = b"A" * 100 + KEY.encode() + b"B" * 100
    for size in (1, 7, len(KEY) - 1, 101, 112):
        chunks = [data[i : i + size] for i in range(0, len(data), size)]
        found = SCANNER.scan_stream(chunks, where="f")
        assert [(f.name, f.encoding) for f in found] == [("ANTHROPIC_API_KEY", "raw")], size


def test_a_too_short_secret_is_refused_without_its_value() -> None:
    with pytest.raises(SecretTooShortError) as caught:
        SecretScanner({"SHORT": "x" * (MIN_SECRET_LENGTH - 1)})
    assert "SHORT" in str(caught.value)
    assert "x" * (MIN_SECRET_LENGTH - 1) not in str(caught.value)


def test_an_empty_scanner_finds_nothing_and_is_false() -> None:
    empty = SecretScanner.empty()
    assert not empty
    assert empty.scan_text(KEY, where="w") == []
    assert SCANNER


def test_no_representation_or_finding_holds_a_value() -> None:
    data = KEY.encode() + base64.b64encode(TOKEN.encode())
    findings = SCANNER.scan_bytes(data, where="w")
    shown = repr(SCANNER) + repr(findings) + " ".join(f.describe() for f in findings)
    for value in (KEY, TOKEN):
        assert value not in shown
        assert base64.b64encode(value.encode()).decode()[4:-4] not in shown
    assert "ANTHROPIC_API_KEY" in repr(SCANNER)


# -- the candidate checkout -----------------------------------------------------------


def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "src").mkdir(parents=True)
    (root / "src" / "clean.py").write_text("print(1)\n")
    (root / "src" / "leak.py").write_text(f"KEY = {KEY!r}\n")
    (root / "docs").mkdir()
    (root / "docs" / "notes.md").write_bytes(base64.b64encode(TOKEN.encode()))
    return root


def test_changed_files_are_scanned_and_unchanged_ones_are_not(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    found = SCANNER.scan_candidate(root, ["docs/notes.md", "src/clean.py"])
    assert [(f.name, f.encoding, f.where) for f in found] == [
        ("GITHUB_TOKEN", "base64", "docs/notes.md")
    ]  # src/leak.py is not a changed path here


def test_a_secret_in_a_path_name_is_found_without_repeating_the_path(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    leaked = f"data/{TOKEN}.txt"
    (root / "data").mkdir()
    (root / leaked).write_text("x\n")
    found = SCANNER.scan_candidate(root, [leaked])
    assert [(f.name, f.where) for f in found] == [("GITHUB_TOKEN", "a changed path's name")]
    assert TOKEN not in " ".join(f.describe() for f in found)


def test_a_symlink_target_is_scanned_and_never_followed(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(KEY)  # would be found if the link were followed
    os.symlink(outside, root / "src" / "to-outside")
    os.symlink(f"../{TOKEN}", root / "src" / "named")
    found = SCANNER.scan_candidate(root, ["src/named", "src/to-outside"])
    assert [(f.name, f.where) for f in found] == [("GITHUB_TOKEN", "symlink target of src/named")]


def test_a_path_under_a_symlinked_directory_is_not_read(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "leak.py").write_text(KEY)
    os.symlink(elsewhere, root / "linked")
    assert SCANNER.scan_candidate(root, ["linked/leak.py"]) == []


def test_deleted_paths_and_directories_have_nothing_to_scan(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    (root / "vendor" / "sub").mkdir(parents=True)  # a submodule's directory, say
    assert SCANNER.scan_candidate(root, ["gone.py", "src/gone/deeper.py", "vendor/sub"]) == []


def test_a_large_file_is_read_in_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_scan, "CHUNK_BYTES", 16)
    root = checkout(tmp_path)
    (root / "big.bin").write_bytes(b"\0" * 1000 + KEY.encode().hex().encode() + b"\0" * 1000)
    found = SCANNER.scan_candidate(root, ["big.bin"])
    assert [(f.name, f.encoding) for f in found] == [("ANTHROPIC_API_KEY", "hex")]


def test_a_finding_inside_a_secret_named_file_does_not_repeat_its_name(tmp_path: Path) -> None:
    root = checkout(tmp_path)
    named = f"src/{TOKEN}.py"
    (root / named).write_text(KEY)
    os.symlink(KEY, root / "src" / f"link-{TOKEN}")
    found = SCANNER.scan_candidate(root, [named, f"src/link-{TOKEN}"])
    shown = " ".join(f.describe() for f in found)
    assert TOKEN not in shown
    assert KEY not in shown
    assert "ANTHROPIC_API_KEY (raw) in [a path withheld: its name holds GITHUB_TOKEN]" in shown
    assert "symlink target of [a path withheld" in shown


def test_shown_path_keeps_ordinary_paths() -> None:
    assert SCANNER.shown_path("src/app.py") == "src/app.py"
    hidden = SCANNER.shown_path(f"a/{base64.b64encode(KEY.encode()).decode()}")
    assert hidden == "[a path withheld: its name holds ANTHROPIC_API_KEY]"
