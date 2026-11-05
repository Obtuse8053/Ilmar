# SPDX-License-Identifier: Apache-2.0
"""Strict reading of ``[adapters.<port>] options`` and of secrets (ADR-0007, ADR-0019).

Error messages name the section and the key, and say what was expected.
They never repeat a configured value, so nothing pasted into a log or an
issue can leak what a value was. Secrets are read from the environment only,
through ``*_env`` keys.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ilmar.domain.errors import ConfigError

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_REDACTED = "[redacted]"
_MISSING: Any = object()


class Options:
    """A typed, strict view of one adapter's options.

    Read every key with the typed getters, then call :meth:`finish`, which
    rejects any key that was not read.
    """

    def __init__(self, raw: Mapping[str, Any], where: str) -> None:
        self._raw = raw
        self._where = where
        self._read: set[str] = set()

    @property
    def where(self) -> str:
        return self._where

    def has(self, key: str) -> bool:
        return key in self._raw

    def keys(self) -> frozenset[str]:
        return frozenset(self._raw)

    def error(self, key: str, problem: str) -> ConfigError:
        return ConfigError(f"{self._where} {key}: {problem}")

    def _get(self, key: str, default: Any) -> Any:
        self._read.add(key)
        if key in self._raw:
            return self._raw[key]
        if default is _MISSING:
            raise self.error(key, "is required")
        return default

    def string(
        self, key: str, default: Any = _MISSING, *, pattern: re.Pattern[str] | None = None
    ) -> str:
        value = self._get(key, default)
        if not isinstance(value, str) or not value:
            raise self.error(key, "must be a non-empty string")
        if pattern is not None and not pattern.fullmatch(value):
            raise self.error(key, f"must match {pattern.pattern}")
        return value

    def optional_string(self, key: str, *, pattern: re.Pattern[str] | None = None) -> str | None:
        if key not in self._raw:
            self._read.add(key)
            return None
        return self.string(key, pattern=pattern)

    def strings(self, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        value = self._get(key, list(default))
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise self.error(key, "must be a list of non-empty strings")
        return tuple(value)

    def boolean(self, key: str, default: bool) -> bool:
        value = self._get(key, default)
        if not isinstance(value, bool):
            raise self.error(key, "must be true or false")
        return value

    def positive_int(self, key: str, default: Any = _MISSING) -> int:
        value = self._get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise self.error(key, "must be a positive integer")
        return value

    def optional_positive_int(self, key: str) -> int | None:
        if key not in self._raw:
            self._read.add(key)
            return None
        return self.positive_int(key)

    def optional_positive_number(self, key: str) -> float | None:
        self._read.add(key)
        if key not in self._raw:
            return None
        value = self._raw[key]
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise self.error(key, "must be a positive number")
        return float(value)

    def https_url(self, key: str, default: Any = _MISSING) -> str:
        value = self.string(key, default)
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise self.error(key, "must be an https URL without credentials, query or fragment")
        return value.rstrip("/")

    def optional_https_url(self, key: str) -> str | None:
        if key not in self._raw:
            self._read.add(key)
            return None
        return self.https_url(key)

    def env_name(self, key: str) -> str:
        """The name of the environment variable holding a secret (``*_env`` keys only)."""
        if not key.endswith("_env"):  # pragma: no cover - a programming error
            raise ValueError("secret references must use *_env keys")
        value = self._get(key, _MISSING)
        if not isinstance(value, str) or not _ENV_NAME.fullmatch(value):
            raise self.error(key, "must name an environment variable, like 'GITHUB_TOKEN'")
        return value

    def path(self, key: str, default: Path, *, base_dir: Path) -> Path:
        value = self._get(key, str(default))
        if not isinstance(value, str) or not value or "\0" in value:
            raise self.error(key, "must be a non-empty path")
        path = Path(value)
        return path if path.is_absolute() else base_dir / path

    def finish(self) -> None:
        unknown = sorted(set(self._raw) - self._read)
        if unknown:
            raise ConfigError(f"{self._where}: unknown option(s): {', '.join(unknown)}")


class Secrets:
    """Reads secrets from the environment and remembers them for redaction.

    Only :meth:`get` returns a secret's value. :meth:`redact` removes every
    value read so far from a text, for error messages built from adapter
    errors.
    """

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ
        self._values: set[str] = set()

    def get(self, name: str, *, where: str) -> str:
        value = self._environ.get(name, "")
        if not value:
            raise ConfigError(f"{where}: environment variable {name} is not set or empty")
        if any(c in value for c in "\r\n\0"):
            raise ConfigError(f"{where}: environment variable {name} must hold a single line")
        self._values.add(value)
        return value

    def redact(self, text: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, _REDACTED)
        return text
