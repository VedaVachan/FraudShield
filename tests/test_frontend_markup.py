"""Static checks of the two frontend pages (no browser is started)."""

from html.parser import HTMLParser
from pathlib import Path
import re
import unittest


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


class PageParser(HTMLParser):
    """Collect ids (with attributes), meta tags, visible text and scripts."""

    def __init__(self):
        super().__init__()
        self.elements = {}
        self.duplicate_ids = []
        self.meta = []
        self.text = []
        self.scripts = []
        self.styles = []
        self._capture = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            if values["id"] in self.elements:
                self.duplicate_ids.append(values["id"])
            self.elements[values["id"]] = {"tag": tag, **values}
        if tag == "meta":
            self.meta.append(values)
        if tag in ("script", "style"):
            self._capture = tag
            (self.scripts if tag == "script" else self.styles).append("")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._capture = None

    def handle_data(self, data):
        if self._capture == "script":
            self.scripts[-1] += data
        elif self._capture == "style":
            self.styles[-1] += data
        else:
            self.text.append(data)


def parse(name):
    parser = PageParser()
    parser.feed((FRONTEND / name).read_text(encoding="utf-8"))
    return parser


def function_source(script, name):
    match = re.search(rf"function {name}\(.*?\n}}\n", script, re.DOTALL)
    return match.group(0) if match else None


class TransactionPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = parse("index.html")
        cls.script = "\n".join(cls.page.scripts)
        cls.visible = " ".join(cls.page.text)

    def test_ids_are_unique_and_existing_ids_are_kept(self):
        self.assertEqual(self.page.duplicate_ids, [])
        for name in (
            "api-origin", "api-config", "api-reset", "api-config-message",
            "api-config-error", "form", "fields", "type", "amount", "balance",
            "explain", "error", "empty", "result", "decision", "score",
            "threshold", "model", "shap", "contributions", "reconstruction",
            "status", "connection-fields",
        ):
            self.assertIn(name, self.page.elements, name)

    def test_every_script_lookup_points_to_an_existing_id(self):
        referenced = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', self.script))
        referenced |= set(re.findall(r'(?:error|input): "([a-z-]+)"', self.script))
        self.assertTrue(referenced)
        self.assertEqual(
            sorted(referenced - set(self.page.elements)), []
        )

    def test_viewport_and_small_screen_rules_exist(self):
        self.assertTrue(any(
            m.get("name") == "viewport" and "width=device-width" in m.get("content", "")
            for m in self.page.meta
        ))
        self.assertIn("@media(max-width:760px)", "\n".join(self.page.styles))

    def test_inline_error_elements_exist_for_both_fields(self):
        for field in ("amount", "balance"):
            error = self.page.elements[f"{field}-error"]
            self.assertEqual(error.get("role"), "alert")
            self.assertIn("hidden", error)
            self.assertEqual(
                self.page.elements[field].get("aria-describedby"), f"{field}-error"
            )
        self.assertIn("function validateFields", self.script)
        self.assertIn("cannot be negative", self.script)
        self.assertIn("validity.badInput", self.script)

    def test_plain_language_labels_keep_the_technical_terms(self):
        for text in (
            "Fraud score", "(model score)", "Decision threshold",
            "Why this score?", "(feature contributions)",
            "(engineered feature)", "(SHAP log-odds)",
        ):
            self.assertIn(text, self.visible, text)

    def test_existing_messages_and_api_calls_are_unchanged(self):
        for message in (
            "Enter finite numerical amounts.",
            "Invalid input. Check the type, amount, and balance.",
            "Request timed out. Check the backend and try again.",
            "Cannot reach the API. Check the backend and browser connection.",
            "check the address and backend",
            "Enter a complete local API address",
        ):
            self.assertIn(message, self.script, message)
        self.assertIn('request(explain ? "/explain" : "/predict", payload)', self.script)
        self.assertIn('request("/health")', self.script)


class PipelinePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = parse("pipeline.html")
        cls.script = "\n".join(cls.page.scripts)
        cls.summary = (FRONTEND / "pipeline_summary.js").read_text(encoding="utf-8")
        cls.index_script = "\n".join(parse("index.html").scripts)

    def test_ids_are_unique_and_script_lookups_resolve(self):
        self.assertEqual(self.page.duplicate_ids, [])
        referenced = set(re.findall(r'byId\("([A-Za-z0-9_-]+)"\)', self.script))
        referenced |= set(re.findall(
            r'getElementById\("([A-Za-z0-9_-]+)"\)', self.summary
        ))
        self.assertTrue(referenced)
        self.assertEqual(sorted(referenced - set(self.page.elements)), [])

    def test_viewport_and_small_screen_rules_exist(self):
        self.assertTrue(any(
            m.get("name") == "viewport" for m in self.page.meta
        ))
        self.assertIn("@media(max-width:640px)", "\n".join(self.page.styles))

    def test_address_is_remembered_with_the_same_validation_as_the_transaction_page(self):
        self.assertIn('"fraudshield.apiOrigin"', self.script)
        self.assertIn('"fraudshield.apiOrigin"', self.index_script)
        mine = function_source(self.script, "normalizeAPI")
        theirs = function_source(self.index_script, "normalizeAPI")
        self.assertIsNotNone(mine)
        self.assertEqual(mine, theirs)
        self.assertIn("localStorage.setItem(API_STORAGE_KEY, origin)", self.script)
        error = self.page.elements["api-error"]
        self.assertEqual(error.get("role"), "alert")
        self.assertIn("hidden", error)
        self.assertIn("stopImmediatePropagation", self.script)

    def test_failed_runs_are_marked_and_explained(self):
        self.assertIn('failed ? "FAILED"', self.script)
        self.assertIn('row.className = "row-failed"', self.script)
        self.assertIn("No error text is stored for this failed run", self.script)
        self.assertIn("tr.row-failed", "\n".join(self.page.styles))

    def test_diagram_lists_the_unit_tests_stage_with_the_others(self):
        diagram = " ".join(self.page.text)
        for stage in (
            "paysim.dvc", "spark_prepare", "logreg_train", "xgboost_train",
            "xgboost_validation_gate", "unit_tests",
        ):
            self.assertIn(stage, diagram, stage)

    def test_dvc_yaml_defines_every_stage_the_diagram_shows(self):
        stages = (FRONTEND.parent / "dvc.yaml").read_text(encoding="utf-8")
        for stage in (
            "spark_prepare", "logreg_train", "xgboost_train",
            "xgboost_validation_gate", "unit_tests",
        ):
            self.assertRegex(stages, rf"(?m)^  {stage}:$")


if __name__ == "__main__":
    unittest.main()
