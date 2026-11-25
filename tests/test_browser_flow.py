import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from job_agent.browser import _agent_profile, _click_needs_approval, build_controller
from job_agent.form_fill import _FILL_SCRIPT, fill_known_fields
from job_agent.tracker import ApplicationTracker


class BrowserFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from browser_use.dom.views import EnhancedDOMTreeNode, NodeType

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.node = EnhancedDOMTreeNode(
            node_id=4, backend_node_id=4, node_type=NodeType.ELEMENT_NODE,
            node_name="button", node_value="Continue", attributes={"type": "submit"},
            is_scrollable=False, is_visible=True, absolute_position=None,
            target_id="target", frame_id=None, session_id=None, content_document=None,
            shadow_root_type=None, shadow_roots=[], parent_node=None, children_nodes=[],
            ax_node=None, snapshot_node=None,
        )
        self.dispatched = []

        class Event:
            def __await__(self):
                async def complete():
                    return self
                return complete().__await__()

            async def event_result(self, **kwargs):
                return {}

        def dispatch(event):
            self.dispatched.append(event)
            return Event()

        self.session = SimpleNamespace(
            get_element_by_index=AsyncMock(side_effect=lambda index: self.node),
            event_bus=SimpleNamespace(dispatch=dispatch),
        )
        self.state = {}
        self.controller = build_controller(
            {"personal": {"first_name": "Ari"}},
            SimpleNamespace(take_screenshot=AsyncMock()),
            screenshot_path=Path(self.directory.name) / "review.png",
            state=self.state,
        )
        self.actions = self.controller.registry.registry.actions

    async def action(self, name, **params):
        action = self.actions[name]
        kwargs = {"params": action.param_model(**params)}
        if name in {"safe_click", "safe_navigate", "input"}:
            kwargs["browser_session"] = self.session
        return await action.function(**kwargs)

    async def approve(self, response="approve"):
        with patch("job_agent.browser.request_telegram_review", new=AsyncMock(return_value=None)), patch("builtins.input", return_value=response):
            return await self.action("request_human_approval", filled_summary="Ready to submit.")

    async def test_continue_is_automatic_even_with_submit_type(self):
        result = await self.action("safe_click", index=4)
        self.assertIsNone(result.error)
        self.assertEqual(len(self.dispatched), 1)
        self.assertFalse(self.state.get("submission_attempted"))

    async def test_contextual_final_click_requires_approval_and_cannot_repeat(self):
        first = await self.action("safe_click", index=4, final_submission=True)
        self.assertIn("request_human_approval", first.error)
        self.assertEqual(len(self.dispatched), 0)
        await self.approve()
        final = await self.action("safe_click", index=4)
        self.assertIsNone(final.error)
        repeat = await self.action("safe_click", index=4, final_submission=True)
        self.assertIn("finished", repeat.error)
        self.assertEqual(len(self.dispatched), 1)

    async def test_changed_target_requires_new_approval(self):
        await self.action("safe_click", index=4, final_submission=True)
        await self.approve()
        self.node.backend_node_id = 5
        result = await self.action("safe_click", index=4, final_submission=True)
        self.assertIn("request_human_approval", result.error)
        self.assertEqual(len(self.dispatched), 0)

    async def test_rejection_stops_future_clicks(self):
        await self.action("safe_click", index=4, final_submission=True)
        result = await self.approve("reject")
        self.assertTrue(result.is_done)
        blocked = await self.action("safe_click", index=4)
        self.assertIsNotNone(blocked.error)
        self.assertEqual(len(self.dispatched), 0)

    async def test_navigation_invalidates_approval(self):
        await self.action("safe_click", index=4, final_submission=True)
        await self.approve()
        await self.action("safe_navigate", url="https://jobs.test/next")
        blocked = await self.action("safe_click", index=4, final_submission=True)
        self.assertIsNotNone(blocked.error)
        self.assertEqual(len(self.dispatched), 1)  # navigation only

    async def test_input_invalidates_approval_even_if_input_fails(self):
        await self.action("safe_click", index=4, final_submission=True)
        await self.approve()
        self.session.get_element_by_index.side_effect = None
        self.session.get_element_by_index.return_value = None
        await self.action("input", index=4, text="new value")
        self.session.get_element_by_index.return_value = self.node
        blocked = await self.action("safe_click", index=4, final_submission=True)
        self.assertIsNotNone(blocked.error)
        self.assertEqual(len(self.dispatched), 0)

    async def test_unsafe_navigation_is_rejected(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "https://user:pass@jobs.test"):
            result = await self.action("safe_navigate", url=url)
            self.assertIsNotNone(result.error)
        self.assertEqual(len(self.dispatched), 0)

    async def test_cdp_fill_passes_profile_as_data_and_checks_errors(self):
        evaluate = AsyncMock(return_value={"result": {"value": {"filled": 2}}})
        session = SimpleNamespace(
            session_id="session", cdp_client=SimpleNamespace(
                send=SimpleNamespace(Runtime=SimpleNamespace(evaluate=evaluate))
            ),
        )
        browser = SimpleNamespace(get_or_create_cdp_session=AsyncMock(return_value=session))
        self.assertEqual(await fill_known_fields(browser, {"personal": {"first_name": "Ari"}}), 2)
        self.assertEqual(evaluate.call_args.kwargs["session_id"], "session")
        evaluate.return_value = {"exceptionDetails": {"text": "Error"}}
        with self.assertRaises(RuntimeError):
            await fill_known_fields(browser, {})


class LocalDataTests(unittest.TestCase):
    def test_profile_excludes_local_paths_and_demographics(self):
        self.assertEqual(_agent_profile({
            "personal": {"first_name": "Ari"}, "files": {"resume": "private"},
            "demographics_optional": {"gender": "private"},
        }), {"personal": {"first_name": "Ari"}})

    def test_nested_submit_text_is_detected(self):
        child = SimpleNamespace(node_value="Submit application", attributes={}, children_nodes=[])
        self.assertTrue(_click_needs_approval(SimpleNamespace(node_value="", attributes={}, children_nodes=[child])))

    def test_reviewed_jobs_are_not_automatically_requeued(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = ApplicationTracker(Path(directory) / "apps.db")
            tracker.record(job_url="https://jobs.test/1", company_name="Example", role_title="Engineer", status="REVIEWED")
            self.assertIn("https://jobs.test/1", tracker.processed_urls())

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the local DOM fixture")
    def test_fill_script_preserves_values_and_skips_ambiguous_or_hidden_fields(self):
        fixture = r'''
class HTMLInputElement {
    constructor(label, value = '', opts = {}) {
        this.labels = [{textContent: label}]; this._value = value;
        this.type = 'text'; this.autocomplete = ''; this.events = [];
        Object.assign(this, opts);
    }
    get value() {return this._value;}
    set value(value) {this._value = value;}
    getAttribute(name) {return this[name] || null;}
    getClientRects() {return this.hidden ? [] : [{}];}
    dispatchEvent(event) {this.events.push(event.type);}
}
const inputs = [
    new HTMLInputElement('First name'),
    new HTMLInputElement('Email', 'existing@test'),
    new HTMLInputElement('Email', '', {hidden: true}),
    new HTMLInputElement('Last name', '', {readOnly: true}),
    new HTMLInputElement('Email', '', {autocomplete: 'given-name'}),
    new HTMLInputElement('Why do you want this job?'),
    new HTMLInputElement('Email', '', {type: 'checkbox'}),
    new HTMLInputElement('Email', '', {disabled: true}),
    new HTMLInputElement('First name', '', {labels: [], autocomplete: 'given-name'})
];
const document = {querySelectorAll: () => inputs};
const getComputedStyle = () => ({visibility: 'visible'});
'''
        script = fixture + '\nconst result = ' + _FILL_SCRIPT + '(' + json.dumps({"first name": "Ari", "email": "new@test", "last name": "Lee"}) + ');\n'
        script += 'console.log(JSON.stringify({result, values: inputs.map(i => i.value), events: inputs.map(i => i.events)}));'
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        self.assertEqual(data["result"], {"filled": 2})
        self.assertEqual(data["values"], ["Ari", "existing@test", "", "", "", "", "", "", "Ari"])
        self.assertEqual(data["events"][0], ["input", "change"])
