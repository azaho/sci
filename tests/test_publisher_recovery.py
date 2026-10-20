"""The recovery operation must not untaint a mismatched or inactive app."""

import copy
import io
import json
import unittest
from unittest.mock import patch

from deploy import recover_publisher as recovery


class PublisherRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.app = {
            "id": "test-app",
            "spec": {"name": "sci-publisher"},
            "active_deployment": {"phase": "ACTIVE"},
            "live_url": "https://example.ondigitalocean.app",
        }
        self.health = {"status": "ok", "database": "ok", "storage": {"temp": "ok", "public": "ok"}}

    def run_recovery(self, app, address, health):
        replies = [
            io.BytesIO(json.dumps({"apps": [app]}).encode()),
            io.BytesIO(json.dumps(health).encode()),
        ]
        with patch.dict("os.environ", {"DIGITALOCEAN_TOKEN": "test", "APP_NAME": "sci-publisher"}), \
                patch.object(recovery.urllib.request, "urlopen", side_effect=replies), \
                patch.object(recovery.subprocess, "check_output", return_value=address), \
                patch.object(recovery.subprocess, "run") as mutate:
            try:
                recovery.main()
            except ValueError:
                mutate.assert_not_called()
                raise
            mutate.assert_called_once_with(
                ["terraform", "-chdir=deploy", "untaint", "-lock-timeout=10m",
                 "-allow-missing", recovery.ADDRESS], check=True,
            )

    def test_active_matching_app_can_be_recovered(self):
        self.run_recovery(self.app, recovery.ADDRESS + "\n", self.health)

    def test_mismatched_state_is_not_modified(self):
        with self.assertRaises(ValueError):
            self.run_recovery(self.app, "digitalocean_app.other\n", self.health)

    def test_database_failure_does_not_modify_state(self):
        with self.assertRaises(ValueError):
            self.run_recovery(self.app, recovery.ADDRESS + "\n", {"database": "error"})

    def test_unhealthy_or_ambiguous_apps_are_rejected(self):
        for field, value in [
            ("active_deployment", {"phase": "ERROR"}),
            ("pending_deployment", {"phase": "PENDING_BUILD"}),
            ("live_url", "https://attacker.example/"),
            ("live_url", "https://secret@example.ondigitalocean.app/"),
        ]:
            app = copy.deepcopy(self.app)
            app[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.run_recovery(app, recovery.ADDRESS + "\n", self.health)
        with self.assertRaises(ValueError):
            recovery.active_publisher([self.app, self.app], "sci-publisher")

    def test_degraded_storage_does_not_require_recreating_app(self):
        health = {"status": "degraded", "database": "ok", "storage": {"public": "error"}}
        self.run_recovery(self.app, recovery.ADDRESS + "\n", health)
