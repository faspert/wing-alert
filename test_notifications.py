import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import notifications


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.alert = {
            "spot": "Préverenges",
            "start": datetime(2026, 8, 30, 14, tzinfo=ZoneInfo("Europe/Zurich")),
            "end": datetime(2026, 8, 30, 17, tzinfo=ZoneInfo("Europe/Zurich")),
            "wind": "12-18",
            "direction": "SW (225°)",
        }

    @patch("notifications.subprocess.run")
    def test_sends_signal_group_message(self, run):
        environment = {
            "SIGNAL_CLI": "/usr/local/bin/signal-cli",
            "SIGNAL_CONFIG_DIR": "/var/lib/signal-cli",
            "SIGNAL_ACCOUNT": "+41770000000",
            "SIGNAL_GROUP_ID": "group-id",
        }

        with patch.dict(os.environ, environment, clear=True):
            notifications.send_signal(self.alert, "Test forecast")

        run.assert_called_once_with(
            [
                "/usr/local/bin/signal-cli",
                "--config",
                "/var/lib/signal-cli",
                "-a",
                "+41770000000",
                "send",
                "-g",
                "group-id",
                "-m",
                notifications._signal_text(self.alert, "Test forecast"),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    @patch("notifications.subprocess.run")
    def test_sends_signal_group_message_with_plot(self, run):
        environment = {
            "SIGNAL_CLI": "/usr/local/bin/signal-cli",
            "SIGNAL_CONFIG_DIR": "/var/lib/signal-cli",
            "SIGNAL_ACCOUNT": "+41770000000",
            "SIGNAL_GROUP_ID": "group-id",
        }

        with tempfile.NamedTemporaryFile(suffix=".png") as plot:
            with patch.dict(os.environ, environment, clear=True):
                notifications.send_signal(
                    self.alert,
                    "Test forecast",
                    attachment=plot.name,
                )

        run.assert_called_once_with(
            [
                "/usr/local/bin/signal-cli",
                "--config",
                "/var/lib/signal-cli",
                "-a",
                "+41770000000",
                "send",
                "-g",
                "group-id",
                "-m",
                notifications._signal_text(self.alert, "Test forecast"),
                "--attachment",
                plot.name,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )


if __name__ == "__main__":
    unittest.main()
