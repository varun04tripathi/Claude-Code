"""Tests for pipeline/build.py, using the made-up sample data from pipeline/make_sample.py.

Run from the project root:  python -m unittest discover -s tests -v
"""
import importlib.util
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "pipeline" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load("build")
make_sample = load("make_sample")


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.data = cls.tmp / "data"
        with mock.patch.object(make_sample, "OUT", cls.data), mock.patch("sys.stdout"):
            make_sample.main(["--days", "5"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def embedded(self, html):
        match = re.search(r"window\.SCORECARD = (.*?);</script>", html, re.S)
        return json.loads(match.group(1))

    def test_page_has_engine_and_data_and_no_placeholders(self):
        html = build.build(self.data)
        self.assertNotIn(build.ENGINE_SLOT, html)
        self.assertNotIn(build.DATA_SLOT, html)
        self.assertIn("ScoreEngine", html)
        payload = self.embedded(html)
        self.assertEqual(payload["latest"], json.loads((self.data / "latest.json").read_text()))
        self.assertEqual(len(payload["history"]), 5)
        self.assertEqual(payload["config"]["version"], json.loads(build.CONFIG.read_text())["version"])
        self.assertFalse(payload["sample"])

    def test_sample_flag_reaches_the_page(self):
        self.assertTrue(self.embedded(build.build(self.data, sample=True))["sample"])

    def test_history_is_reduced_to_chart_fields(self):
        day = build.load_history(self.data)[-1]
        for values in day["tickers"].values():
            self.assertEqual(set(values), {"composite", "price"})

    def test_history_is_capped(self):
        with mock.patch.object(build, "HISTORY_DAYS", 3):
            self.assertEqual(len(build.load_history(self.data)), 3)

    def test_text_cannot_close_the_script_tag(self):
        self.assertEqual(build.script_safe('"</script><b>"'), '"<\\/script><b>"')
        self.assertEqual(build.script_safe("</SCRIPT"), "<\\/SCRIPT")

    def test_missing_latest_is_a_clear_error(self):
        with self.assertRaisesRegex(build.BuildError, "run pipeline/compute.py first"):
            build.build(self.tmp / "nowhere")

    def test_wrong_schema_version_is_rejected(self):
        bad = self.tmp / "bad"
        shutil.copytree(self.data, bad)
        latest = json.loads((bad / "latest.json").read_text())
        latest["schema_version"] = 99
        (bad / "latest.json").write_text(json.dumps(latest))
        with self.assertRaisesRegex(build.BuildError, "schema_version"):
            build.build(bad)


if __name__ == "__main__":
    unittest.main()
