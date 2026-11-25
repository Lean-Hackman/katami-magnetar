import json
import tempfile
import unittest
from pathlib import Path

from job_agent.campaign import campaign_with_skills
from job_agent.skills import SkillStore, WorkflowSkill, record_observation


class WorkflowSkillTests(unittest.TestCase):
    def test_host_and_mode_matching(self):
        skill = WorkflowSkill(
            name="example",
            mode="jobs",
            hosts=("example.com",),
            instructions="Complete the form.",
        )
        self.assertTrue(skill.matches("https://careers.example.com/apply", "jobs"))
        self.assertFalse(skill.matches("https://example.com/apply", "research"))
        self.assertFalse(skill.matches("https://notexample.com/apply", "jobs"))

    def test_store_saves_and_loads_data_only_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SkillStore([directory])
            path = store.save(
                WorkflowSkill(
                    name="Example Flow",
                    mode="research",
                    hosts=("example.com",),
                    instructions="Collect public facts.",
                ),
                directory,
            )
            self.assertTrue(path.is_file())
            matches = store.match("https://example.com", "research")
            self.assertEqual(matches[0].name, "Example Flow")

    def test_job_mode_receives_builtin_skill(self):
        campaign = campaign_with_skills(
            name="apply",
            url="https://jobs.example.com/apply",
            instructions="Apply.",
            mode="jobs",
        )
        self.assertIn("generic-application", [skill.name for skill in campaign.skills])
        generic = next(skill for skill in campaign.skills if skill.name == "generic-application")
        self.assertIn("attempt that upload first", generic.instructions)

    def test_campaign_rejects_unsafe_or_empty_configuration(self):
        with self.assertRaisesRegex(ValueError, "HTTP or HTTPS"):
            campaign_with_skills(name="bad", url="file:///tmp/x", instructions="Run")
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            campaign_with_skills(name="bad", url="https://example.com", instructions=" ")

    def test_local_skill_overrides_builtin_by_mode_and_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            SkillStore().save(
                WorkflowSkill(
                    name="generic-application",
                    mode="jobs",
                    instructions="Local behavior",
                ),
                root,
            )
            matches = SkillStore([root]).match("https://example.com", "jobs")
            skill = next(item for item in matches if item.name == "generic-application")
            self.assertEqual(skill.instructions, "Local behavior")

    def test_observation_excludes_url_path_and_private_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = record_observation(
                root=directory,
                campaign="example",
                mode="general",
                url="https://example.com/private/path?token=secret",
                success=True,
                matched_skills=[],
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["host"], "example.com")
            self.assertNotIn("url", data)
            self.assertNotIn("result", data)
            self.assertNotIn("secret", path.read_text(encoding="utf-8"))
