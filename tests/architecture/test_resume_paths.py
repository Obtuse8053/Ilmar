# SPDX-License-Identifier: Apache-2.0
"""Only ``ilmar resume`` clears a suspension (ADR-0029 §3, ADR-0039, #131 review).

Loop mode relies on a suspension to stop over unreviewed uncertain gateway
spend, and ``ilmar resume`` refuses to clear any suspension while such spend
is unreviewed. That holds only if no other code path clears one, so: no module
but ``cli_suspend.py`` builds a ``Commit`` with ``resume=``, and the stores
clear a suspension only for such a commit.

Read statically with ``ast``, like the dependency rules.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "ilmar"
RESUMERS = {SRC / "cli_suspend.py"}


def _resumes(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "resume"
    ]


def test_only_ilmar_resume_clears_a_suspension() -> None:
    found = {
        str(path.relative_to(SRC)): lines
        for path in sorted(SRC.rglob("*.py"))
        if path not in RESUMERS and (lines := _resumes(path))
    }
    assert found == {}


def test_ilmar_resume_does_clear_one() -> None:
    assert _resumes(SRC / "cli_suspend.py")


def test_the_stores_delete_a_suspension_only_on_resume() -> None:
    sqlite = (SRC / "adapters" / "sqlite.py").read_text()
    assert sqlite.count("DELETE FROM suspension") == 1
    deleting = sqlite.index("DELETE FROM suspension")
    assert "if changes.resume is not None:" in sqlite[deleting - 120 : deleting]
    memory = (SRC / "adapters" / "memory.py").read_text()
    assert memory.count("self._suspension = None") == 1
    clearing = memory.index("self._suspension = None")
    assert "if changes.resume is not None:" in memory[clearing - 80 : clearing]
