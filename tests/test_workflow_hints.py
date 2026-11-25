import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_agent.workflow_hints import WorkflowHintObserver, path_pattern, semantic_actions


class WorkflowHintObserverTests(unittest.TestCase):
    def test_normalizes_without_values_or_volatile_ids(self):
        self.assertEqual(path_pattern("https://Example.com/orders/12345?token=secret"), "/orders/*")
        self.assertEqual(
            semantic_actions(["screenshot", "input", "input", "safe_click", "done"]),
            ("input", "safe_click"),
        )

    def test_repeated_success_increments_one_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "hints.sqlite3"
            observer = WorkflowHintObserver(database)
            for _ in range(2):
                self.assertTrue(observer.observe_success(
                    url="https://portal.example.com/orders/987654?session=private",
                    mode="general",
                    actions=["input", "safe_click", "done"],
                ))
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT host, path_pattern, actions_json, successes, promoted "
                    "FROM workflow_hint_candidates"
                ).fetchone()
            self.assertEqual(row[:2], ("portal.example.com", "/orders/*"))
            self.assertEqual(json.loads(row[2]), ["input", "safe_click"])
            self.assertEqual(row[3:], (2, 0))

    def test_empty_action_path_is_not_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "hints.sqlite3"
            self.assertFalse(WorkflowHintObserver(database).observe_success(
                url="https://example.com", mode="general", actions=["done"]
            ))
            self.assertFalse(database.exists())
