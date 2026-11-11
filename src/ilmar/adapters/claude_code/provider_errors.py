# SPDX-License-Identifier: Apache-2.0
"""Classify Claude Code's provider failures (ADR-0029 §5, #90).

Only the final outcome is classified: Claude Code may retry inside its own
run. The exact texts Claude Code shows at a workspace spend limit are not yet
observed; the Gate 1 drill (#93) must establish them. Until then the patterns
fail closed:

* anything that mentions a spend, usage, credit, billing or quota limit is
  ``CAP``, even next to a 429;
* any authentication, authorization or login failure is ``AUTH``;
* only an ordinary rate limit or overload is ``RATE_LIMIT``, which is
  transient.

``CAP`` and ``AUTH`` are never retried. #18 turns them into a durable
suspension; until then the execution fails without a retry.
"""

from __future__ import annotations

import enum
import re


class ProviderFailure(enum.Enum):
    CAP = "spend or usage limit"
    AUTH = "authentication or authorization"
    RATE_LIMIT = "rate limit"


_CAP = re.compile(
    r"spend(?:ing)?[ _-]?limit|usage[ _-]?limit|workspace[^.\n]{0,40}limit|credit[ _-]?balance"
    r"|billing|insufficient[ _-]?(?:credit|funds|quota)|quota[ _-]?exceeded|out of credits",
    re.IGNORECASE,
)
_AUTH = re.compile(
    r"authenticat|invalid[ _-]?(?:x-)?api[ _-]?key|unauthori[sz]ed|forbidden"
    r"|permission[ _-]?(?:error|denied)|\b40[13]\b|oauth|not logged in|/login",
    re.IGNORECASE,
)
_RATE = re.compile(r"rate[ _-]?limit|\b429\b|too many requests|overloaded|\b529\b", re.IGNORECASE)


def classify(text: str) -> ProviderFailure | None:
    """The provider failure ``text`` reports, or ``None`` if it reports none."""
    if _CAP.search(text):
        return ProviderFailure.CAP
    if _AUTH.search(text):
        return ProviderFailure.AUTH
    if _RATE.search(text):
        return ProviderFailure.RATE_LIMIT
    return None
