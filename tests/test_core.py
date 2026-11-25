import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from job_agent.browser import (
    RESTRICTED_ACTIONS,
    _click_needs_approval,
    build_controller,
)
from job_agent.documents import draft_cover_letter
from job_agent.profile import field_values, load_profile, profile_value
from job_agent.llm_config import resolve_model, resolve_base_url
from job_agent.scraper import merge_queue
from job_agent.tracker import ApplicationTracker


class ProfileTests(unittest.TestCase):
    def test_profile_field_resolution_and_form_values(self) -> None:
        profile = {
            "personal": {"first_name": "Ari", "last_name": "Lee", "email": "a@b.test"},
            "links": {"github": "https://github.com/ari"},
        }
        self.assertEqual(profile_value(profile, "personal.email"), "a@b.test")
        self.assertIsNone(profile_value(profile, "personal.phone"))
        values = field_values(profile)
        self.assertEqual(values["full name"], "Ari Lee")
        self.assertEqual(values["github"], "https://github.com/ari")

    def test_invalid_json_has_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid JSON"):
                load_profile(path)


class ModelConfigTests(unittest.TestCase):
    def test_accepts_arbitrary_model_ids_without_a_fixed_allowlist(self) -> None:
        self.assertEqual(resolve_model("provider/custom-model-v9"), "provider/custom-model-v9")

    def test_reads_model_from_environment_and_errors_when_unset(self) -> None:
        with patch.dict("os.environ", {"OPENAI_MODEL": "account-model"}):
            self.assertEqual(resolve_model(), "account-model")
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Choose a model"):
                resolve_model()


class QueueTests(unittest.TestCase):
    def test_merge_skips_queued_and_processed_urls(self) -> None:
        existing = [{"job_url": "https://jobs.test/1"}]
        discovered = [
            {"job_url": "https://jobs.test/1"},
            {"job_url": "https://jobs.test/2"},
            {"job_url": "https://jobs.test/3"},
        ]
        merged = merge_queue(existing, discovered, {"https://jobs.test/3"})
        self.assertEqual([job["job_url"] for job in merged], [
            "https://jobs.test/1",
            "https://jobs.test/2",
        ])


class TrackerTests(unittest.TestCase):
    def test_status_upsert_and_processed_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tracker = ApplicationTracker(Path(directory) / "apps.sqlite3")
            tracker.record(
                job_url="https://jobs.test/1",
                company_name="Example",
                role_title="Engineer",
                status="IN_PROGRESS",
            )
            self.assertNotIn("https://jobs.test/1", tracker.processed_urls())
            tracker.record(
                job_url="https://jobs.test/1",
                company_name="Example",
                role_title="Engineer",
                status="SUBMITTED",
            )
            self.assertIn("https://jobs.test/1", tracker.processed_urls())
            self.assertEqual(len(tracker.list_applications()), 1)


class DocumentTests(unittest.TestCase):
    def test_cover_letter_uses_profile_facts_without_inventing_experience(self) -> None:
        profile = {
            "personal": {"first_name": "Ari", "last_name": "Lee"},
            "experience_summary": "Python engineer with API experience.",
            "work_history": [{"highlights": ["Built API services."]}],
        }
        letter = draft_cover_letter(
            profile, "Example Co", "Python Engineer", "Build Python API services."
        )
        self.assertIn("Ari Lee", letter)
        self.assertIn("Built API services.", letter)
        self.assertNotIn("5+ years", letter)


class AgentGuardrailTests(unittest.TestCase):
    def test_browser_actions_are_usable_and_submit_is_gated(self) -> None:
        controller = build_controller(
            {
                "personal": {"first_name": "Ari"},
                "experience_summary": "Python engineer.",
            }
        )
        available = set(controller.registry.registry.actions)
        self.assertTrue(set(RESTRICTED_ACTIONS).isdisjoint(available))
        self.assertIn("safe_click", available)
        self.assertIn("safe_navigate", available)
        self.assertNotIn("click", available)
        self.assertNotIn("navigate", available)
        self.assertIn("input", available)
        self.assertIn("get_candidate_profile", available)
        self.assertIn("answer_open_question", available)

    def test_submit_controls_require_approval_but_dropdowns_do_not(self) -> None:
        class Node:
            def __init__(self, node_name: str, attributes: dict[str, str], node_value: str = ""):
                self.node_name = node_name
                self.attributes = attributes
                self.node_value = node_value

        self.assertTrue(
            _click_needs_approval(Node("button", {"type": "submit"}, "Submit"))
        )
        self.assertTrue(
            _click_needs_approval(Node("button", {"type": "button"}, "Submit Application"))
        )
        self.assertFalse(
            _click_needs_approval(Node("button", {"type": "button"}, "Country"))
        )


class ClickApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_click_requires_approval_for_the_same_target(self) -> None:
        from browser_use.dom.views import EnhancedDOMTreeNode, NodeType

        def submit_node() -> EnhancedDOMTreeNode:
            return EnhancedDOMTreeNode(
                node_id=4,
                backend_node_id=4,
                node_type=NodeType.ELEMENT_NODE,
                node_name="button",
                node_value="Submit application",
                attributes={"type": "submit"},
                is_scrollable=False,
                is_visible=True,
                absolute_position=None,
                target_id="test-target",
                frame_id=None,
                session_id=None,
                content_document=None,
                shadow_root_type=None,
                shadow_roots=[],
                parent_node=None,
                children_nodes=[],
                ax_node=None,
                snapshot_node=None,
            )

        class Event:
            async def event_result(self, **kwargs):
                return {}

            def __await__(self):
                async def complete():
                    return self

                return complete().__await__()

        class EventBus:
            def __init__(self):
                self.dispatched = 0

            def dispatch(self, event):
                self.dispatched += 1
                return Event()

        class BrowserSession:
            def __init__(self):
                self.event_bus = EventBus()

            async def get_element_by_index(self, index):
                return submit_node()

            async def highlight_interaction_element(self, node):
                return None

        class Browser:
            async def take_screenshot(self, path, full_page):
                Path(path).write_bytes(b"review")

        with tempfile.TemporaryDirectory() as directory:
            controller = build_controller(
                {"personal": {}},
                Browser(),
                company="Example",
                role="Engineer",
                screenshot_path=Path(directory) / "review.png",
            )
            click = controller.registry.registry.actions["safe_click"]
            approve = controller.registry.registry.actions["request_human_approval"]
            browser_session = BrowserSession()

            first = await click.function(
                params=click.param_model(index=4),
                browser_session=browser_session,
            )
            self.assertIn("request_human_approval", first.error)
            self.assertEqual(browser_session.event_bus.dispatched, 0)

            with (
                patch(
                    "job_agent.browser.request_telegram_review",
                    new=AsyncMock(return_value=None),
                ),
                patch("builtins.input", return_value="approve"),
            ):
                decision = await approve.function(
                    params=approve.param_model(
                        filled_summary="Required fields reviewed."
                    )
                )
            self.assertIn("approved one click", decision.extracted_content)

            final = await click.function(
                params=click.param_model(index=4),
                browser_session=browser_session,
            )
            self.assertIn("Clicked", final.extracted_content)
            self.assertEqual(browser_session.event_bus.dispatched, 1)


if __name__ == "__main__":
    unittest.main()
