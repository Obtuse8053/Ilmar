# SPDX-License-Identifier: Apache-2.0
"""Print the licenses of installed Python distributions and flag problems.

Usage: ``python scripts/license_inventory.py [--allow-unknown]``

Exit status is 1 if any installed distribution declares a license on the deny
list, or (unless ``--allow-unknown``) declares no license at all. This is a
first line of defence; it does not replace human review (MANIFEST §7.4).
Standard library only, so it can run before anything else is installed.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from importlib import metadata

# Licenses incompatible with distributing Ilmar under Apache-2.0 when linked or
# bundled. Matched case-insensitively against the declared license text.
DENY = (
    re.compile(r"\bAGPL", re.IGNORECASE),
    re.compile(r"Affero", re.IGNORECASE),
    re.compile(r"\bSSPL\b|Server Side Public License", re.IGNORECASE),
    re.compile(r"(?<!L)GPL", re.IGNORECASE),
    re.compile(r"GNU General Public License", re.IGNORECASE),
    re.compile(r"Business Source License|\bBUSL\b", re.IGNORECASE),
    re.compile(r"Commons Clause", re.IGNORECASE),
)


def _field(meta: metadata.PackageMetadata, key: str) -> str:
    values = meta.get_all(key)
    return str(values[0]).strip() if values else ""


def declared_license(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = _field(meta, "License-Expression")
    if expression:
        return expression
    classifiers = [
        c.removeprefix("License :: ").removeprefix("OSI Approved :: ")
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    if classifiers:
        return "; ".join(classifiers)
    text = _field(meta, "License")
    first_line = text.splitlines()[0] if text else ""
    return first_line if len(first_line) <= 80 else first_line[:77] + "..."


_ALTERNATIVES = re.compile(r"\s+OR\s+|;\s*")


def is_denied(license_text: str) -> bool:
    """True if every license alternative offered is on the deny list.

    ``Apache-2.0 OR GPL-2.0-or-later`` (or several license classifiers, which
    conventionally signal a choice) passes because one alternative is
    acceptable. ``MIT AND GPL-2.0-only`` is denied.
    """
    alternatives = [a for a in _ALTERNATIVES.split(license_text.strip("() ")) if a]
    if not alternatives:
        return False
    return all(any(p.search(a) for p in DENY) for a in alternatives)


def inventory(dists: Iterable[metadata.Distribution]) -> list[tuple[str, str, str]]:
    rows = {(str(d.metadata["Name"]).lower(), str(d.version), declared_license(d)) for d in dists}
    return sorted(rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--allow-unknown", action="store_true")
    args = parser.parse_args(argv)

    problems = 0
    for name, version, license_text in inventory(metadata.distributions()):
        status = "ok"
        if is_denied(license_text):
            status, problems = "DENIED", problems + 1
        elif not license_text:
            status = "unknown"
            if not args.allow_unknown:
                problems += 1
        print(f"{status:8} {name}=={version}  {license_text or '(none declared)'}")
    if problems:
        print(f"\n{problems} distribution(s) need review.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
