# SPDX-License-Identifier: Apache-2.0
"""GitHub adapters: issues as tasks (#6); source control follows in #7."""

from ilmar.adapters.github.client import GitHubClient
from ilmar.adapters.github.tasks import GitHubTaskProvider

__all__ = ["GitHubClient", "GitHubTaskProvider"]
