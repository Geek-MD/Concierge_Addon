import ast
import json
import unittest
from pathlib import Path
from typing import Any


MAIN_FILE = Path(__file__).parents[1] / "app" / "main.py"
TEMPLATE_FILE = Path(__file__).parents[1] / "app" / "templates" / "coe_administraciones.json"


def load_functions(*names: str) -> dict[str, Any]:
    """Load pure helper functions without importing the service's heavy OCR stack."""
    tree = ast.parse(MAIN_FILE.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    module = ast.Module(body=selected, type_ignores=[])
    namespace: dict[str, Any] = {"Any": Any, "re": __import__("re")}
    exec(compile(module, str(MAIN_FILE), "exec"), namespace)
    return namespace


HELPERS = load_functions(
    "_calculate_candidate_priority",
    "_find_variable_value",
    "_find_variable_values_on_row",
    "_variable_value_matches",
)


APPLY_TEMPLATE = load_functions("_apply_template")["_apply_template"]


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
    def test_same_row_join_without_anchor_returns_none(self) -> None:
        APPLY_TEMPLATE.__globals__.update(
            {
                "_flatten_ocr_lines": lambda payload, config: [],
                "_section_anchor_score": lambda section, lines, config: (1.0, 1),
                "_find_best_fixed_match": lambda *args, **kwargs: None,
                "_find_variable_values_on_row": lambda anchor, lines: [],
                "_variable_value_matches": lambda value, value_type: True,
            }
        )
        template = {
            "sections": [
                {
                    "id": "header",
                    "lines": [
                        {
                            "boxes": [
                                {
                                    "role": "variable",
                                    "key": "description",
                                    "locator": {"strategy": "same_row_right_join"},
                                }
                            ]
                        }
                    ],
                }
            ]
        }

        result = APPLY_TEMPLATE({"page_count": 1}, template)

        self.assertIsNone(result["sections"]["header"]["description"])

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

    def test_currency_locator_skips_a_nearby_text_label(self) -> None:
        percentage = line(37, 350, 400, "5,00%", used=True)
        wrong_label = line(38, 100, 402, "Subtotal Departamento")
        amount = line(39, 600, 400, "$6.734")

        result = HELPERS["_find_variable_value"](
            percentage,
            [percentage, wrong_label, amount],
            {"strategy": "same_line_right"},
            1,
            "currency",
        )

        self.assertIs(result, amount)

    def test_same_row_join_collects_note_month_year_and_department(self) -> None:
        anchor = line(10, 100, 200, "Nota de Cobro")
        period = line(11, 300, 200, "Julio 2026")
        department = line(12, 500, 201, "Depto. 404")
        next_row = line(13, 300, 240, "Copropietario")

        result = HELPERS["_find_variable_values_on_row"](
            anchor, [anchor, period, department, next_row]
        )

        self.assertEqual(result, [period, department])

    def test_value_type_shapes(self) -> None:
        matches = HELPERS["_variable_value_matches"]
        self.assertTrue(matches("$ 6.734", "currency"))
        self.assertTrue(matches("123,456", "numeric"))
        self.assertFalse(matches("Subtotal Departamento", "currency"))

    def test_provision_amount_is_anchored_to_percentage_cell(self) -> None:
        template = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
        breakdown = next(
            section
            for section in template["sections"]
            if section["id"] == "tabla_desglose_departamento"
        )
        provision = next(
            row for row in breakdown["lines"] if row["id"] == "linea_provision_fondos"
        )
        amount = next(
            box for box in provision["boxes"] if box.get("key") == "provision_fondos_monto"
        )

        self.assertEqual(amount["locator"]["anchor_key"], "provision_fondos_porcentaje")


if __name__ == "__main__":
    unittest.main()
