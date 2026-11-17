# SPDX-License-Identifier: Apache-2.0
import unittest

import calc


class AddTest(unittest.TestCase):
    def test_adds(self) -> None:
        assert calc.add(2, 3) == 5  # noqa: S101 - unittest discovers plain asserts too


class DivideTest(unittest.TestCase):
    def test_divides(self) -> None:
        assert calc.divide(6, 3) == 2  # noqa: S101


if __name__ == "__main__":
    unittest.main()
