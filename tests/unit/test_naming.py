# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import itertools
import re

import pytest

from ilmar.core.naming import (
    MAX_SLUG_LENGTH,
    branch_name,
    container_name,
    safe_identifier,
    task_slug,
)
from ilmar.domain.tasks import TaskRef
from ilmar.domain.workspace import ContainerSpec

SAFE = re.compile(r"^[A-Za-z0-9._-]+$")

HOSTILE_KEYS = [
    "Obtuse8053/Ilmar#7",
    "../../etc/passwd",
    "/absolute/path",
    "..",
    ".",
    ".hidden",
    "-rf",
    "a..b",
    "name.lock",
    "trailing/",
    "with space and\ttab\nnewline",
    "nul\x00byte",
    "Ünïcödé-ключ-键",
    "ｆｕｌｌｗｉｄｔｈ",  # noqa: RUF001 - deliberately confusable fullwidth characters
    "emoji-🚀",
    "x" * 500,
    "@{upstream}",
    "~^:?*[\\",
]


@pytest.mark.parametrize("key", HOSTILE_KEYS)
def test_task_slug_is_safe_for_paths_and_git(key: str) -> None:
    slug = task_slug(TaskRef("github", key))
    assert SAFE.match(slug)
    assert len(slug) <= MAX_SLUG_LENGTH
    assert slug not in (".", "..")
    assert not slug.startswith((".", "-"))
    assert ".." not in slug
    assert not slug.endswith((".lock", ".", "/"))
    assert "/" not in slug


def test_task_slug_is_deterministic_and_readable() -> None:
    task = TaskRef("github", "Obtuse8053/Ilmar#7")
    assert task_slug(task) == task_slug(TaskRef("github", "Obtuse8053/Ilmar#7"))
    assert task_slug(task).startswith("Obtuse8053_Ilmar_7-")


def test_keys_that_sanitise_alike_still_get_distinct_slugs() -> None:
    lookalikes = [
        TaskRef("github", "a/b"),
        TaskRef("github", "a_b"),
        TaskRef("github", "a b"),
        TaskRef("github", "a\\b"),
        TaskRef("gitlab", "a/b"),
        TaskRef("github", "ｆｕｌｌ"),  # noqa: RUF001 - NFKC-equal to "full"
        TaskRef("github", "full"),
        TaskRef("github", "x" * 200 + "1"),
        TaskRef("github", "x" * 200 + "2"),
    ]
    slugs = [task_slug(t) for t in lookalikes]
    assert len(set(slugs)) == len(slugs)
    for a, b in itertools.combinations(lookalikes, 2):
        assert task_slug(a) != task_slug(b)


def test_slug_length_limit_is_enforced() -> None:
    with pytest.raises(ValueError, match="max_length"):
        task_slug(TaskRef("github", "x"), max_length=5)
    assert len(task_slug(TaskRef("github", "y" * 1000), max_length=20)) <= 20


def test_branch_name() -> None:
    name = branch_name(TaskRef("github", "../../main"))
    assert name.startswith("ilmar/")
    assert SAFE.match(name.removeprefix("ilmar/"))
    with pytest.raises(ValueError, match="prefix"):
        branch_name(TaskRef("github", "x"), prefix="../evil")


def test_container_name_is_valid_for_container_specs() -> None:
    name = container_name("inst", "0123456789abcdef", 2)
    assert name == "ilmar-inst-0123456789abcdef-2"
    assert re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", name)
    assert ContainerSpec.__name__  # the spec regex is exercised in test_domain


@pytest.mark.parametrize(
    ("instance", "execution", "attempt"),
    [("inst", "../x", 1), ("in/st", "e", 1), ("inst", "e", 0), ("i" * 64, "e" * 64, 1)],
)
def test_container_name_rejects_unsafe_or_overlong_parts(
    instance: str, execution: str, attempt: int
) -> None:
    with pytest.raises(ValueError, match=r"id|attempt|longer"):
        container_name(instance, execution, attempt)


@pytest.mark.parametrize("value", ["", ".", "..", "-x", ".x", "a/b", "a.b", "a b", "x" * 65, "é"])
def test_safe_identifier_rejects(value: str) -> None:
    with pytest.raises(ValueError, match="execution id"):
        safe_identifier(value, "execution id")


def test_safe_identifier_accepts_generated_ids() -> None:
    assert safe_identifier("3f2b9c0e4a7d4e0f8b1a2c3d4e5f6a7b", "execution id")
    assert safe_identifier("inst_1-a", "instance id")
