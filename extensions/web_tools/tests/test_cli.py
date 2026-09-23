"""Exercise onboarding in a fresh temporary checkout with no real Docker daemon."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kkb-web-cli-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ignore = shutil.ignore_patterns("__pycache__", ".env", ".venv", ".cache")
        shutil.copytree(ROOT / "backend", self.root / "backend", ignore=ignore)
        self.extension = self.root / "extensions" / "web_tools"
        shutil.copytree(ROOT / "extensions" / "web_tools", self.extension, ignore=ignore)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("WEB_") and key != "SEARXNG_SECRET"}
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env.pop("PYTHONPATH", None)
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        # Test the public command's lifecycle arguments, with no Docker state.
        fake = self.bin / "docker"
        fake.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "with open(os.environ['WEB_TEST_DOCKER_CALLS'], 'a') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:3] == ['compose', 'version']:\n"
            "    print('Docker Compose version v2.39.4')\n"
            "elif sys.argv[1] == 'info':\n"
            "    print('28.3.3')\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        self.calls = self.root / "docker-calls.jsonl"
        self.env["WEB_TEST_DOCKER_CALLS"] = str(self.calls)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "")

    def run_cli(self, *args, overrides=None):
        env = self.env | (overrides or {})
        return subprocess.run(
            [sys.executable, "-S", str(self.extension / "web-tools"), *args],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=10,
        )

    def setup_config(self):
        result = self.run_cli("setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.extension / ".env"

    def test_setup_is_repeatable_and_preserves_existing_values(self):
        config = self.setup_config()
        custom = config.read_text().replace("WEB_SEARCH_PORT=8888", "WEB_SEARCH_PORT=18888")
        custom += "TEAM_NOTE=do-not-change\n"
        config.write_text(custom)
        before = config.read_bytes()
        timestamp = config.stat().st_mtime_ns
        result = self.run_cli("setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(config.stat().st_mtime_ns, timestamp)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertNotIn("TEAM_NOTE", result.stdout)

    def test_config_redacts_secret_and_respects_process_overrides(self):
        self.setup_config()
        secret = "private-test-value-never-printed"
        result = self.run_cli("config", overrides={
            "SEARXNG_SECRET": secret, "WEB_SEARCH_PORT": "18889",
            "WEB_TOOLS_ENABLED": "true", "MODEL_API_KEY": "unrelated-private-value",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn("unrelated-private-value", result.stdout + result.stderr)
        config = json.loads(result.stdout)
        self.assertTrue(config["enabled"])
        self.assertEqual(config["SEARXNG_SECRET"], "[set]")
        self.assertEqual(config["WEB_SEARXNG_URL"], "http://127.0.0.1:18889")

    def test_fresh_checkout_registers_nothing_without_dependencies(self):
        # -S hides site packages. No setup, browser, daemon or credentials needed.
        result = self.run_cli("search", "example")
        self.assertEqual(result.returncode, 2)
        self.assertIn("feature_disabled", result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.extension / ".env").exists())

    def test_lifecycle_commands_target_only_generated_extension_project(self):
        config = self.setup_config()
        project = next(line.split("=", 1)[1] for line in config.read_text().splitlines()
                       if line.startswith("WEB_COMPOSE_PROJECT="))
        for command in ("start", "browser-test", "logs", "stop"):
            result = self.run_cli(command)
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        lifecycle = [call for call in calls if "--project-name" in call]
        self.assertEqual(len(lifecycle), 4)
        for call in lifecycle:
            self.assertEqual(call[call.index("--project-name") + 1], project)
            self.assertTrue(project.startswith("kkb-web-"))
            self.assertEqual(call[call.index("--env-file") + 1], str(config))
            self.assertEqual(call[call.index("--file") + 1], str(self.extension / "compose.yaml"))
            self.assertFalse({"down", "prune", "rm", "--volumes", "kill"}.intersection(call))
        self.assertIn("up", lifecycle[0])
        self.assertIn("--wait", lifecycle[0])
        self.assertIn("WEB_TOOLS_TEST_BROWSER=1", lifecycle[1])
        self.assertEqual(lifecycle[-1][-1], "stop")

    def test_unrelated_compose_project_is_rejected(self):
        self.setup_config()
        result = self.run_cli("stop", overrides={"WEB_COMPOSE_PROJECT": "team-baseline"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("WEB_COMPOSE_PROJECT", result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertFalse(any("stop" in call for call in calls))

    def test_bootstrap_prepares_configuration_without_docker(self):
        # Python uses an absolute executable; an empty directory guarantees no Docker.
        empty = self.root / "empty-bin"
        empty.mkdir()
        result = self.run_cli("setup", overrides={"PATH": str(empty)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Prerequisite missing", result.stdout)
        self.assertTrue((self.extension / ".env").exists())
        result = self.run_cli("start", overrides={"PATH": str(empty)})
        self.assertEqual(result.returncode, 2)
        self.assertIn("Docker", result.stderr)

    def test_setup_does_not_evaluate_environment_content(self):
        marker = self.root / "must-not-exist"
        config = self.setup_config()
        with config.open("a") as stream:
            stream.write("TEAM_NOTE=$(touch " + str(marker) + ")\n")
        result = self.run_cli("setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_optional_overlay_and_mia_secret_redaction(self):
        self.setup_config()
        overrides = {"WEB_DOCUMENTS_ENABLED": "true", "WEB_IMAGES_ENABLED": "true", "WEB_LINKS_ENABLED": "true",
                     "WEB_KLOUDEKS_API_KEY": "private-mia-key", "WEB_TOOLS_ENABLED": "true"}
        config = self.run_cli("config", overrides=overrides)
        self.assertEqual(config.returncode, 0, config.stderr)
        self.assertNotIn("private-mia-key", config.stdout + config.stderr)
        self.assertEqual(json.loads(config.stdout)["WEB_KLOUDEKS_API_KEY"], "[set]")
        for command in ("start", "asset-test"):
            result = self.run_cli(command, overrides=overrides)
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        scoped = [call for call in calls if "--project-name" in call]
        self.assertEqual(len(scoped), 2)
        for call in scoped:
            self.assertIn(str(self.extension / "compose.assets.yaml"), call)
        self.assertIn("WEB_TOOLS_TEST_ASSETS=1", scoped[-1])

    def test_asset_commands_are_off_by_default(self):
        for command in ("document", "image", "assets"):
            result = self.run_cli(command, "https://example.com/file", overrides={"WEB_TOOLS_ENABLED": "true"})
            self.assertEqual(result.returncode, 2)
            self.assertIn("feature_disabled", result.stderr)


if __name__ == "__main__":
    unittest.main()
