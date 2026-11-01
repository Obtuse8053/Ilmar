# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from license_inventory import is_denied


@pytest.mark.parametrize(
    "text",
    [
        "GPL-3.0-only",
        "GNU General Public License v3 (GPLv3)",
        "AGPL-3.0-or-later",
        "GNU Affero General Public License v3",
        "SSPL-1.0",
        "Business Source License 1.1",
        "MIT AND GPL-2.0-or-later",
        "GPL-2.0-only OR AGPL-3.0-only",
    ],
)
def test_incompatible_licenses_are_denied(text: str) -> None:
    assert is_denied(text)


@pytest.mark.parametrize(
    "text",
    [
        "MIT",
        "Apache-2.0",
        "BSD-3-Clause",
        "Apache Software License; MIT License",
        "LGPL-3.0-or-later",
        "GNU Lesser General Public License v3 (LGPLv3)",
        "PSF-2.0",
        "MPL-2.0",
        "Apache-2.0 OR GPL-2.0-or-later",
        "Apache Software License; GNU General Public License (GPL)",
    ],
)
def test_permissive_and_weak_copyleft_licenses_pass(text: str) -> None:
    assert not is_denied(text)
