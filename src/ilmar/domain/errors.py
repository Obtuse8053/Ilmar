# SPDX-License-Identifier: Apache-2.0
"""Error hierarchy shared by the core and all adapters."""

from __future__ import annotations


class IlmarError(Exception):
    """Base class for all errors raised deliberately by Ilmar."""


class ConfigError(IlmarError):
    """The configuration is missing, malformed or unsafe."""


class InvalidTransitionError(IlmarError):
    """A requested execution state change is not allowed by the state machine."""


class RetryLimitExceededError(InvalidTransitionError):
    """A retry was requested but the execution has used all permitted attempts."""


class ExecutionNotFoundError(IlmarError):
    """No execution exists with the requested identifier."""


class DuplicateExecutionError(IlmarError):
    """The task already has an active (non-terminal) execution, or the id is taken."""


class ConcurrencyConflictError(IlmarError):
    """The stored execution changed since it was read; the write was rejected."""


class ProviderError(IlmarError):
    """An external provider failed. Adapters wrap provider-specific errors in this."""
