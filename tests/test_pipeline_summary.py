"""Check historical summary availability and error responses."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import pipeline


class PipelineSummaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "summary.json"
        patcher = patch.object(pipeline, "SUMMARY", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(pipeline.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_returns_saved_snapshot(self):
        expected = {
            "schema_version": 1,
            "scope": "historical snapshot",
            "spark": {"rows": 100, "total_seconds": 2},
        }
        self.path.write_text(json.dumps(expected))
        response = self.client.get("/pipeline/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)

    def test_missing_snapshot_returns_503(self):
        response = self.client.get("/pipeline/summary")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(str(self.path), response.text)

    def test_invalid_snapshot_returns_503(self):
        for content in ("not-json", "[]", '{"schema_version": 99}'):
            with self.subTest(content=content):
                self.path.write_text(content)
                response = self.client.get("/pipeline/summary")
                self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
