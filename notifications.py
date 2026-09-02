"""Notification helpers shared by the forecast checkers."""

from csv import Error
import logging
import os
import subprocess
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)


def _signal_text(alert, source):
    return (
        f"🌬️ Wingfoil window — {alert['spot']}\n\n"
        f"{alert['start']:%a %d %b %H:%M}"
        f"–{alert['end']:%H:%M}\n"
        f"Wind: {alert['wind']} kt\n"
        f"Direction: {alert['direction']}\n\n"
        f"Source: {source}"
    )


def send_signal(alert, source, dry_run=False):
    """Send one alert to the configured Signal group using signal-cli."""

    text = _signal_text(alert, source)
    if dry_run:
        log.info("[DRY RUN] Signal group message\n%s", text)
        return

    account = os.getenv("SIGNAL_ACCOUNT")
    group_id = os.getenv("SIGNAL_GROUP_ID")
    config_dir = os.getenv("SIGNAL_CONFIG_DIR", "/var/lib/signal-cli")
    cli = os.getenv("SIGNAL_CLI", "signal-cli")

    command = [
        cli,
        "--config",
        config_dir,
        "-a",
        account,
        "send",
        "-g",
        group_id,
        "-m",
        text,
    ]

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"signal-cli executable not found: {cli}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"signal-cli failed with exit code {exc.returncode}{suffix}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("signal-cli timed out while sending the alert") from exc

    log.info("Signal alert sent to group %s", group_id)
    if result.stdout:
        log.debug("signal-cli: %s", result.stdout.strip())


def main():
    """Test sending a Signal message."""
    logging.basicConfig(level=logging.INFO)

    alert = {
        "spot": "Préverenges",
        "start": datetime(2026, 8, 30, 14, 0, tzinfo=timezone(timedelta(hours=2))),
        "end": datetime(2026, 8, 30, 17, 0, tzinfo=timezone(timedelta(hours=2))),
        "wind": "12-18",
        "direction": "SW (225°)",
    }
    try:
        send_signal(alert, "Test forecast", dry_run=False)
    except Error as exc:
        log.error("Failed to send Signal alert: %s", exc)


if __name__ == "__main__":
    main()
