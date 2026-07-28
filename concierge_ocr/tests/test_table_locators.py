import ast
import unittest
from pathlib import Path
from typing import Any


MAIN_FILE = Path(__file__).parents[1] / "app" / "main.py"


def load_functions(*names: str) -> dict[str, Any]:
    """Load pure helper functions without importing the service's heavy OCR stack."""
    tree = ast.parse(MAIN_FILE.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    module = ast.Module(body=selected, type_ignores=[])
    namespace: dict[str, Any] = {"Any": Any}
    exec(compile(module, str(MAIN_FILE), "exec"), namespace)
    return namespace


HELPERS = load_functions("_calculate_candidate_priority", "_find_variable_value")


def line(index: int, x: float, y: float, text: str, *, used: bool = False) -> dict[str, Any]:
    return {
        "page": 1,
        "line_index": index,
        "center_x": x,
        "center_y": y,
        "height": 20.0,
        "confidence": 0.99,
        "text": text,
        "used_variable": used,
    }


class TableLocatorTests(unittest.TestCase):
    def test_same_line_right_uses_visual_row_not_ocr_sequence_index(self) -> None:
        anchor = line(30, 100, 400, "Gasto Común")
        percentage = line(31, 350, 402, "0,95110%")
        amount = line(32, 600, 399, "$134.800")

        first = HELPERS["_find_variable_value"](
            anchor, [anchor, percentage, amount], {"strategy": "same_line_right"}, 1
        )
        self.assertIs(first, percentage)

        percentage["used_variable"] = True
        second = HELPERS["_find_variable_value"](
            anchor, [anchor, percentage, amount], {"strategy": "same_line_right"}, 1
        )
        self.assertIs(second, amount)

    def test_same_line_right_rejects_the_next_visual_row(self) -> None:
        anchor = line(30, 100, 400, "Cargo Fijo")
        next_row = line(31, 500, 435, "$12.000")

        result = HELPERS["_find_variable_value"](
            anchor, [anchor, next_row], {"strategy": "same_line_right"}, 1
        )
        self.assertIsNone(result)

    def test_same_line_right_keeps_index_fallback_without_geometry(self) -> None:
        anchor = line(10, 0, 0, "Label")
        value = line(10, 0, 0, "Value")
        anchor["center_x"] = anchor["center_y"] = None
        value["center_x"] = value["center_y"] = None

        priority = HELPERS["_calculate_candidate_priority"](
            anchor, value, "same_line_right", 2
        )
        self.assertEqual(priority, (0, 0.0))


if __name__ == "__main__":
    unittest.main()
