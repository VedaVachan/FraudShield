"""Check the provisioned Grafana dashboards against the project's display rules."""

import json
import unittest
from pathlib import Path


FOLDER = Path(__file__).resolve().parents[1] / "monitoring/grafana/provisioning/dashboards/json"
DASHBOARDS = ("fraudshield", "fraudshield-resources")
# Okabe-Ito colours, a colour-blind-safe palette.
SAFE_COLOURS = {
    "#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9", "#F0E442", "#000000",
}


def load(name):
    return json.loads((FOLDER / f"{name}.json").read_text(encoding="utf-8"))


def configured_colours(panel):
    found = []
    defaults = panel["fieldConfig"]["defaults"]
    found += [s["color"] for s in defaults.get("thresholds", {}).get("steps", [])]
    if "color" in defaults:
        found.append(defaults["color"].get("fixedColor"))
    for override in panel["fieldConfig"]["overrides"]:
        for prop in override["properties"]:
            if prop["id"] == "color":
                found.append(prop["value"].get("fixedColor"))
    for mapping in defaults.get("mappings", []):
        found += [item.get("color") for item in mapping["options"].values()]
    return [colour for colour in found if colour is not None]


def has_several_series(panel):
    targets = panel["targets"]
    return len(targets) > 1 or any("{{" in t.get("legendFormat", "") for t in targets)


class DashboardDisplayTests(unittest.TestCase):
    def test_every_dashboard_has_a_title_and_unique_uid(self):
        uids = set()
        for name in DASHBOARDS:
            data = load(name)
            self.assertTrue(data["title"].strip())
            self.assertNotIn(data["uid"], uids)
            uids.add(data["uid"])
            self.assertTrue(data["panels"])

    def test_every_panel_has_a_title_and_a_short_description(self):
        for name in DASHBOARDS:
            for panel in load(name)["panels"]:
                with self.subTest(dashboard=name, panel=panel["id"]):
                    self.assertTrue(panel["title"].strip())
                    description = panel.get("description", "")
                    self.assertTrue(description.strip())
                    self.assertLessEqual(len(description), 400)

    def test_time_series_panels_name_their_axis_and_unit(self):
        for name in DASHBOARDS:
            for panel in load(name)["panels"]:
                if panel["type"] != "timeseries":
                    continue
                defaults = panel["fieldConfig"]["defaults"]
                with self.subTest(dashboard=name, panel=panel["id"]):
                    self.assertTrue(defaults["custom"]["axisLabel"].strip())
                    self.assertIn("unit", defaults)

    def test_legend_is_shown_whenever_a_panel_has_several_series(self):
        for name in DASHBOARDS:
            for panel in load(name)["panels"]:
                if panel["type"] != "timeseries":
                    continue
                legend = panel["options"]["legend"]
                with self.subTest(dashboard=name, panel=panel["id"]):
                    self.assertIs(legend["showLegend"], has_several_series(panel) or False)

    def test_only_colour_blind_safe_colours_are_configured(self):
        for name in DASHBOARDS:
            for panel in load(name)["panels"]:
                with self.subTest(dashboard=name, panel=panel["id"]):
                    for colour in configured_colours(panel):
                        self.assertIn(colour, SAFE_COLOURS)

    def test_health_panels_also_show_text_not_colour_alone(self):
        for name, ids in (("fraudshield", (1, 7)),):
            data = load(name)
            for pid in ids:
                panel = next(p for p in data["panels"] if p["id"] == pid)
                options = panel["fieldConfig"]["defaults"]["mappings"][0]["options"]
                self.assertEqual(options["0"]["text"], "DOWN")
                self.assertEqual(options["1"]["text"], "UP")


if __name__ == "__main__":
    unittest.main()
