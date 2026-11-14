# SPDX-License-Identifier: Apache-2.0
"""``deploy/gate1/ilmar.gate1.toml``: the Gate 1 configuration (#93) is valid
once its placeholder is replaced, and refused until then."""

from __future__ import annotations

from pathlib import Path

import pytest

from ilmar.cli import main
from ilmar.config import load_config
from ilmar.domain.errors import ConfigError

SHIPPED = Path(__file__).resolve().parents[2] / "deploy" / "gate1" / "ilmar.gate1.toml"


def test_the_shipped_placeholder_is_refused() -> None:
    with pytest.raises(ConfigError, match="repository_id"):
        load_config(SHIPPED)


def test_with_the_repository_id_it_is_a_valid_mode_1_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "ilmar.gate1.toml"
    path.write_text(SHIPPED.read_text().replace("repository_id = 0", "repository_id = 123456"))
    config = load_config(path)
    assert config.autonomy.mode == "supervised"
    assert config.autonomy.repository_id == 123456
    assert config.limits.max_concurrent_executions == 1
    assert config.budget is not None
    assert config.budget.kill_switch_file is not None
    for port in ("task_provider", "source_control"):
        assert config.adapters[port].options["repository"] == "Obtuse8053/ilmar-disposable"
    assert main(["config", "validate", str(path)]) == 0, capsys.readouterr().err
