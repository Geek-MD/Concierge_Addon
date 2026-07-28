import unittest
from pathlib import Path


APP_DIR = Path(__file__).parents[1] / "app"


class TemplateEditorUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (APP_DIR / "web_ui.html").read_text(encoding="utf-8")
        cls.main = (APP_DIR / "main.py").read_text(encoding="utf-8")

    def test_editor_exposes_complete_template_workflow(self) -> None:
        for control_id in (
            "newBlank",
            "newGeneric",
            "openTemplate",
            "saveTemplate",
            "deleteTemplate",
            "importTemplate",
            "exportTemplate",
            "validateTemplate",
        ):
            self.assertIn(f'id="{control_id}"', self.html)

    def test_template_requests_are_authenticated(self) -> None:
        self.assertIn("constheaders=()=>", self.html.replace(" ", ""))
        self.assertIn("Authorization:`Bearer ${apiToken.value}`", self.html)

    def test_backend_protects_built_in_templates(self) -> None:
        self.assertIn("Built-in templates are read-only", self.main)
        self.assertIn("Built-in templates cannot be deleted", self.main)

    def test_editor_highlights_and_explains_invalid_json(self) -> None:
        self.assertIn('id="templateHighlight"', self.html)
        self.assertIn("function highlightJson()", self.html)
        self.assertIn("function jsonErrorHelp(error)", self.html)
        self.assertIn("Línea ${line}, columna ${column}", self.html)
        self.assertIn("Sugerencia:", self.html)


if __name__ == "__main__":
    unittest.main()
