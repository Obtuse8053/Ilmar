# SPDX-License-Identifier: Apache-2.0
"""``[secrets] source = "systemd-credentials"``: secrets from the unit's
``$CREDENTIALS_DIRECTORY``, never from the environment, failing closed
(docs/operations/supervisor.md, step 4 of the approved design)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ilmar.adapters.credentials import MAX_BYTES, read_credentials
from ilmar.cli import main
from ilmar.domain.errors import ConfigError

GITHUB_SECRET = "ghp_THIS-MUST-NEVER-BE-PRINTED"
MODEL_SECRET = "sk-ant-THIS-MUST-NEVER-BE-PRINTED"
NAMES = ("ANTHROPIC_API_KEY", "GITHUB_TOKEN")


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    """What systemd makes: a private directory, one 0400 file per credential."""
    path = tmp_path / "credentials"
    path.mkdir(mode=0o700)
    for name, value in (("GITHUB_TOKEN", GITHUB_SECRET), ("ANTHROPIC_API_KEY", MODEL_SECRET)):
        put(path, name, value.encode())
    path.chmod(0o500)
    return path


def put(directory: Path, name: str, content: bytes, mode: int = 0o400) -> Path:
    file = directory / name
    file.write_bytes(content)
    file.chmod(mode)
    return file


def env(directory: Path) -> dict[str, str]:
    return {"CREDENTIALS_DIRECTORY": str(directory)}


@pytest.fixture(autouse=True)
def writable(directory: Path) -> Iterator[None]:
    yield
    directory.chmod(0o700)  # so pytest can clean up


def test_every_named_secret_is_read_by_its_name(directory: Path) -> None:
    assert read_credentials(NAMES, environ=env(directory)) == {
        "ANTHROPIC_API_KEY": MODEL_SECRET, "GITHUB_TOKEN": GITHUB_SECRET,
    }  # fmt: skip


def test_one_trailing_newline_is_allowed(directory: Path) -> None:
    directory.chmod(0o700)
    put(directory, "WITH_NEWLINE", b"value-with-newline\n")
    assert read_credentials(["WITH_NEWLINE"], environ=env(directory)) == {
        "WITH_NEWLINE": "value-with-newline"
    }


def test_the_environment_is_never_a_fallback(directory: Path) -> None:
    with pytest.raises(ConfigError, match="systemd credential NOT_THERE is missing"):
        read_credentials(["NOT_THERE"], environ={**env(directory), "NOT_THERE": "from-env"})


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({}, "CREDENTIALS_DIRECTORY is not set"),
        ({"CREDENTIALS_DIRECTORY": "relative/dir"}, "must be an absolute path"),
        ({"CREDENTIALS_DIRECTORY": "/nonexistent/ilmar-creds"}, "cannot be read"),
    ],
)
def test_without_a_usable_directory_nothing_is_read(environ: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        read_credentials(NAMES, environ=environ)


def test_a_directory_others_can_write_is_refused(directory: Path) -> None:
    directory.chmod(0o777)
    with pytest.raises(ConfigError, match="writable by nobody else"):
        read_credentials(NAMES, environ=env(directory))


def test_a_file_in_place_of_the_directory_is_refused(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.write_text("x")
    with pytest.raises(ConfigError, match="not a directory"):
        read_credentials(NAMES, environ={"CREDENTIALS_DIRECTORY": str(file)})


@pytest.mark.parametrize(
    ("content", "mode", "message"),
    [
        (b"", 0o400, "one non-empty line"),
        (b"\n", 0o400, "one non-empty line"),
        (b"two\nlines", 0o400, "one non-empty line"),
        (b"cr\rhere", 0o400, "one non-empty line"),
        (b"nul\0here", 0o400, "one non-empty line"),
        (b"\xff\xfe", 0o400, "not UTF-8"),
        (b"x" * (MAX_BYTES + 1), 0o400, "larger than"),
        (b"group-readable", 0o440, "readable by nobody else"),
        (b"world-readable", 0o404, "readable by nobody else"),
    ],
)
def test_a_bad_file_is_refused(directory: Path, content: bytes, mode: int, message: str) -> None:
    directory.chmod(0o700)
    put(directory, "BAD", content, mode)
    with pytest.raises(ConfigError, match=message) as caught:
        read_credentials(["BAD"], environ=env(directory))
    assert content.decode("latin-1") not in str(caught.value) or not content.strip()


def test_a_symlink_is_refused(directory: Path, tmp_path: Path) -> None:
    directory.chmod(0o700)
    target = put(tmp_path, "elsewhere", b"secret-elsewhere", 0o400)
    (directory / "LINKED").symlink_to(target)
    with pytest.raises(ConfigError, match="cannot be opened"):
        read_credentials(["LINKED"], environ=env(directory))


def test_a_directory_in_place_of_a_file_is_refused(directory: Path) -> None:
    directory.chmod(0o700)
    (directory / "SUBDIR").mkdir(mode=0o700)
    with pytest.raises(ConfigError, match=r"cannot be opened|not a regular file"):
        read_credentials(["SUBDIR"], environ=env(directory))


@pytest.mark.parametrize("name", ["..", ".", "../escape", "a/b", "", "-leading-dash"])
def test_a_name_that_could_leave_the_directory_is_refused(directory: Path, name: str) -> None:
    with pytest.raises(ConfigError, match="not a valid systemd credential name"):
        read_credentials([name], environ=env(directory))


# -- ilmar config validate --check-environment ----------------------------------------


def test_validate_checks_each_credential_and_prints_no_value(
    directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    path = example_with_credentials(tmp_path)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory))
    assert main(["config", "validate", str(path), "--check-environment"]) == 0
    captured = capsys.readouterr()
    assert "secret GITHUB_TOKEN: set (systemd credential)" in captured.out
    assert GITHUB_SECRET not in captured.out + captured.err
    assert MODEL_SECRET not in captured.out + captured.err


def test_validate_outside_the_unit_reports_the_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = example_with_credentials(tmp_path)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_SECRET)  # never consulted
    assert main(["config", "validate", str(path), "--check-environment"]) == 1
    assert "CREDENTIALS_DIRECTORY is not set" in capsys.readouterr().err


def example_with_credentials(tmp_path: Path) -> Path:
    example = Path(__file__).resolve().parents[2] / "ilmar.example.toml"
    path = tmp_path / "ilmar.toml"
    path.write_text(example.read_text() + '\n[secrets]\nsource = "systemd-credentials"\n')
    return path
