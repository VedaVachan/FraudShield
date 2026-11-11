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


def raw(name):
    return (FRONTEND / name).read_text(encoding="utf-8")


def squash(text):
    return " ".join(text.split())


class OfflineAndAccessibilityTests(unittest.TestCase):
    PAGES = ("index.html", "pipeline.html")

    def test_pages_load_no_external_resources_and_use_system_fonts(self):
        for name in self.PAGES:
            html = raw(name)
            with self.subTest(page=name):
                self.assertNotIn("<link", html)
                self.assertNotIn("@import", html)
                self.assertNotIn("@font-face", html)
                self.assertEqual(
                    re.findall(r'(?:src|href)="(?:https?:)?//[^"]*"', html), []
                )
                self.assertEqual(
                    sorted(set(re.findall(r"url\(([^)]*)\)", html))), ["#arrow"]
                    if name == "pipeline.html" else []
                )
                self.assertIn("system-ui", html)
                for cdn in ("googleapis", "gstatic", "cdn", "unpkg", "jsdelivr", "fontawesome"):
                    self.assertNotIn(cdn, html.lower())

    def test_pages_share_design_tokens_and_accessibility_features(self):
        for name in self.PAGES:
            parser = parse(name)
            styles = "\n".join(parser.styles)
            with self.subTest(page=name):
                for token in ("--primary:", "--text:", "--bg:", "--r-md:", "--shadow:", "--s4:"):
                    self.assertIn(token, styles)
                self.assertIn(":focus-visible", styles)
                self.assertIn("@media(prefers-reduced-motion:reduce)", styles)
                self.assertIn("box-sizing:border-box", styles)
                self.assertIn('href="#main"', raw(name))
                self.assertIn("main", {e["tag"] for e in parser.elements.values()})
                self.assertEqual(parser.elements["main"]["tag"], "main")

    def test_transaction_page_reaches_360_pixels(self):
        styles = "\n".join(parse("index.html").styles)
        self.assertIn("@media(max-width:420px)", styles)
        self.assertIn("grid-template-columns:1fr", styles)

    def test_disclaimers_and_footer_wording_are_unchanged(self):
        visible = squash(" ".join(parse("index.html").text))
        for sentence in (
            "The score is not a calibrated fraud probability.",
            "“Not flagged” does not guarantee that a payment is legitimate.",
            "College project using synthetic PaySim data. Results support a demonstration, "
            "not real payment authorization.",
        ):
            self.assertIn(sentence, visible)


class TransactionPageDesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = parse("index.html")
        cls.script = "\n".join(cls.page.scripts)
        cls.styles = "\n".join(cls.page.styles)

    def test_header_has_a_live_status_chip(self):
        chip = self.page.elements["status-chip"]
        self.assertEqual(chip.get("data-state"), "checking")
        self.assertEqual(self.page.elements["status"].get("role"), "status")
        self.assertIn("function setStatus(text, state)", self.script)
        for state in ("checking", "ready", "warn", "error"):
            self.assertIn(f'"{state}"', self.script)
            self.assertIn(f'data-state="{state}"', self.styles)

    def test_result_panel_has_a_meter_with_a_threshold_marker(self):
        meter = self.page.elements["score-meter"]
        self.assertEqual(meter.get("role"), "meter")
        self.assertEqual(meter.get("aria-valuemin"), "0")
        self.assertEqual(meter.get("aria-valuemax"), "1")
        for name in ("score-fill", "threshold-marker", "score", "threshold"):
            self.assertIn(name, self.page.elements)
        self.assertIn("updateMeter(data.score, data.threshold)", self.script)
        self.assertIn("aria-valuetext", self.script)

    def test_verdict_uses_text_icon_and_colour(self):
        self.assertIn(".decision.flagged", self.styles)
        self.assertIn(".decision.clear", self.styles)
        self.assertIn("⚑", self.styles)
        self.assertIn("✓", self.styles)
        self.assertIn('"Flagged for review" : "Not flagged by the model"', self.script)

    def test_contributions_are_diverging_bars_with_visible_numbers(self):
        for fragment in (
            'bar.className = "bar " + (positive ? "positive" : "negative")',
            'track.setAttribute("aria-hidden", "true")',
            '(positive ? "+" : "") + item.shap_log_odds.toFixed(4)',
            "Math.abs(item.shap_log_odds) / largest",
        ):
            self.assertIn(fragment, self.script)
        self.assertIn(".bar.positive", self.styles)
        self.assertIn(".bar.negative", self.styles)
        # Approved design palette: orange toward fraud, blue away from fraud.
        self.assertIn("--orange-600:#d4640a", self.styles)
        self.assertIn("--blue-600:#2563eb", self.styles)
        self.assertIn(".bar.positive{left:50%;background:var(--orange-600)}", self.styles)
        self.assertIn(".bar.negative{right:50%;background:var(--blue-600)}", self.styles)
        visible = squash(" ".join(self.page.text))
        self.assertIn("The number is always shown.", visible)

    def test_buttons_show_a_loading_state_and_block_double_submits(self):
        for name in ("check", "explain"):
            self.assertEqual(self.page.elements[name]["tag"], "button")
        self.assertIn("setLoading(trigger, true)", self.script)
        self.assertIn("setLoading(trigger, false)", self.script)
        self.assertIn('setAttribute("aria-busy", "true")', self.script)
        self.assertIn("button.loading", self.styles)
        self.assertIn('$("fields").disabled = true', self.script)
        self.assertIn("if (busy) return;", self.script)

    def test_example_buttons_only_fill_the_fields(self):
        for name in ("example-normal", "example-drain"):
            element = self.page.elements[name]
            self.assertEqual(element["tag"], "button")
            self.assertEqual(element.get("type"), "button")
        start = self.script.index("const EXAMPLES")
        end = self.script.index('$("api-origin").value = API;', start)
        handler = self.script[start:end]
        for forbidden in ("run(", "request(", "fetch(", "localStorage", "submit", "sessionStorage"):
            self.assertNotIn(forbidden, handler)
        for value in ('"PAYMENT"', "amount: 50", "balance: 1000", '"TRANSFER"', "100000"):
            self.assertIn(value, handler)

    def test_labels_keep_their_text_and_add_unit_hints(self):
        visible = squash(" ".join(self.page.text))
        for text in (
            "Transaction type", "Transaction amount", "Sender balance before payment",
            "(currency units, 0 or more)", "Try an example",
            "Examples only fill the fields below; nothing is sent or saved.",
        ):
            self.assertIn(text, visible)


class PipelinePageDesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = raw("pipeline.html")
        cls.page = parse("pipeline.html")
        cls.script = "\n".join(cls.page.scripts)
        cls.styles = "\n".join(cls.page.styles)

    def test_the_first_button_is_still_the_refresh_button_the_script_selects(self):
        first = re.search(r"<button[^>]*>(.*?)</button>", self.html, re.DOTALL)
        self.assertIsNotNone(first)
        self.assertEqual(first.group(1).strip(), "Refresh history")
        self.assertIn('document.querySelector("button")', self.script)

    def test_status_is_shown_as_a_pill_with_a_visible_word(self):
        self.assertIn('pill.className = "pill " + run.status', self.script)
        self.assertIn("pill.textContent = row.children[1].textContent", self.script)
        for selector in (".pill.success", ".pill.failed"):
            self.assertIn(selector, self.styles)

    def test_table_is_readable_with_zebra_rows_and_a_sticky_header(self):
        self.assertIn("position:sticky", self.styles)
        self.assertIn("nth-child(even)", self.styles)
        self.assertIn("overflow:auto", self.styles)

    def test_diagram_nodes_are_styled_and_connected_with_arrows(self):
        self.assertEqual(self.page.elements["arrow"]["tag"], "marker")
        for node in ("node-data", "node-step", "node-model", "node-gate", "node-test"):
            self.assertIn(f"class=\"{node}\"", self.html)
            self.assertIn(f".diagram .{node}", self.styles)
        self.assertEqual(self.html.count('marker-end="url(#arrow)"'), 4)
        self.assertIn("Tests (no upstream stage)", " ".join(self.page.text))

    def test_diagram_scrolls_sideways_instead_of_shrinking_text(self):
        wrapper = re.search(r'<div class="diagram-wrap"[^>]*>', self.html).group(0)
        self.assertIn('tabindex="0"', wrapper)
        self.assertIn('role="region"', wrapper)
        self.assertIn("min-width:640px", self.styles)


class ReferenceDesignTests(unittest.TestCase):
    """The pages follow docs/ui_reference.html: top bar, hero, cards, details."""

    @classmethod
    def setUpClass(cls):
        cls.index = raw("index.html")
        cls.pipeline = raw("pipeline.html")
        cls.index_page = parse("index.html")
        cls.index_script = "\n".join(cls.index_page.scripts)
        cls.index_styles = "\n".join(cls.index_page.styles)

    @staticmethod
    def section(html, pattern):
        match = re.search(pattern, html, re.DOTALL)
        assert match, pattern
        return match.group(0)

    def test_both_pages_share_the_white_top_bar_and_hero_band(self):
        for name, html in (("index", self.index), ("pipeline", self.pipeline)):
            with self.subTest(page=name):
                bar = self.section(html, r'<header class="topbar">.*?</header>')
                self.assertIn('class="logo"', bar)
                self.assertIn("<h1>FraudShield</h1>", bar)
                self.assertIn('<nav class="nav" aria-label="Main">', bar)
                hero = self.section(html, r'<section class="hero">.*?</section>')
                self.assertIn("<h2>", hero)
                self.assertIn("linear-gradient", "\n".join(parse(f"{name}.html").styles))

    def test_navigation_keeps_the_existing_link_text(self):
        self.assertIn(">Pipeline and execution history</a>", self.index)
        self.assertIn('href="/pipeline.html"', self.index)
        self.assertIn(">← Transaction review</a>", self.pipeline)
        self.assertIn(">Pipeline and execution history</a>", self.pipeline)

    def test_hero_band_of_the_transaction_page_has_the_approved_text(self):
        hero = self.section(self.index, r'<section class="hero">.*?</section>')
        visible = squash(re.sub(r"<[^>]+>", " ", hero))
        self.assertIn("Check a payment and see why it was scored", visible)
        self.assertIn("Everything runs locally on synthetic PaySim data.", visible)

    def test_status_chip_lives_in_the_top_bar_with_all_four_states(self):
        bar = self.section(self.index, r'<header class="topbar">.*?</header>')
        self.assertIn('id="status-chip"', bar)
        self.assertIn('id="status"', bar)
        self.assertIn('role="status"', bar)
        for state in ("checking", "ready", "warn", "error"):
            self.assertIn(f'setStatus(', self.index_script)
            self.assertIn(f'"{state}")', self.index_script)

    def test_connection_settings_are_in_a_collapsed_details_section(self):
        details = self.section(self.index, r'<details id="connection-details"[^>]*>.*?</details>')
        self.assertNotRegex(details.split(">", 1)[0], r"\bopen\b")
        self.assertIn("<summary>Connection settings</summary>", details)
        for element_id in (
            "api-config", "connection-fields", "api-origin", "api-reset",
            "api-config-message", "api-config-error", "api-config-note",
        ):
            self.assertIn(f'id="{element_id}"', details)
        for label in ("Apply and check connection", "Restore default", "Local API address"):
            self.assertIn(label, details)
        # The transaction form and its buttons are outside the collapsed section.
        for element_id in ("form", "fields", "amount", "balance", "explain", "check"):
            self.assertNotIn(f'id="{element_id}"', details)
        self.assertIn('$("connection-details").open = true', self.index_script)

    def test_two_column_layout_collapses_under_900_pixels(self):
        self.assertIn(".grid{display:grid;grid-template-columns:minmax(340px,420px) 1fr", self.index_styles)
        self.assertIn("@media(max-width:900px){.grid{grid-template-columns:1fr}", self.index_styles)
        form = self.index.index('aria-labelledby="review-title"')
        assessment = self.index.index('aria-labelledby="assessment-title"')
        self.assertLess(form, assessment)

    def test_primary_and_secondary_buttons_and_example_pills(self):
        explain = self.index_page.elements["explain"]
        check = self.index_page.elements["check"]
        self.assertIn("btn primary", explain["class"])
        self.assertIn("btn secondary", check["class"])
        self.assertEqual(check["type"], "submit")
        for name in ("example-normal", "example-drain"):
            self.assertEqual(self.index_page.elements[name]["class"], "pill")
        self.assertIn(".btn.primary", self.index_styles)
        self.assertIn(".btn.secondary", self.index_styles)

    def test_example_handlers_make_no_network_call_and_store_nothing(self):
        start = self.index_script.index("const EXAMPLES")
        end = self.index_script.index('$("api-origin").value = API;', start)
        handler = self.index_script[start:end]
        for forbidden in ("fetch", "request(", "run(", "XMLHttpRequest", "localStorage", "sessionStorage"):
            self.assertNotIn(forbidden, handler)

    def test_stat_cards_meter_and_legend_exist(self):
        for element_id in ("score", "threshold", "score-meter", "threshold-marker", "threshold-label"):
            self.assertIn(f'id="{element_id}"', self.index)
        self.assertIn('class="stats"', self.index)
        self.assertIn('class="legend"', self.index)
        self.assertIn("Pushes toward fraud", self.index)
        self.assertIn("Pushes away from fraud", self.index)

    def test_no_external_resources_in_any_frontend_file(self):
        for path in sorted(FRONTEND.iterdir()):
            if path.suffix not in {".html", ".js"}:
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                for banned in ("<link", "@import", "@font-face", "googleapis", "gstatic",
                               "cdnjs", "unpkg", "jsdelivr", "fontawesome", "<iframe"):
                    self.assertNotIn(banned, text.lower())
                self.assertEqual(re.findall(r'(?:src|href)="(?:https?:)?//', text), [])


if __name__ == "__main__":
    unittest.main()
