# SPDX-License-Identifier: Apache-2.0
"""A spending policy for gateway tests: deliberately high prices (so every
estimate is conservative), the reviewed candidate betas, and a small budget."""

from __future__ import annotations

import json

from ilmar.adapters.podman.gateway_admission import CANDIDATE_BETAS

_RATES = {"input": 15_000_000, "output": 75_000_000, "cache_write_5m": 18_750_000,
          "cache_write_1h": 30_000_000, "cache_read": 1_500_000}  # fmt: skip
MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
POLICY = json.dumps(
    {"prices": dict.fromkeys(MODELS, _RATES), "allowed_betas": sorted(CANDIDATE_BETAS)}
)
BUDGET = 5_000_000  # five dollars, in micro-dollars
