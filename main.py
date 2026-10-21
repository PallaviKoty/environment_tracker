"""Minimal monitor for Singapore's Central region 1-hour PM2.5 reading.

Fetches the official NEA / data.gov.sg API, classifies the Central region
1-hour PM2.5 reading, and sends an hourly Gmail alert. Designed to run either
as a single invocation (cron / GitHub Actions / Task Scheduler) or as a
simple built-in loop for local testing.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

API_URL = "https://api-open.data.gov.sg/v2/real-time/api/pm25"
HAZE_URL = "https://www.haze.gov.sg/"
SG_TZ = timezone(timedelta(hours=8), "Asia/Singapore")
REGION = "central"
REGION_LABEL = "Central"

STATE_FILE = Path(os.environ.get("PM25_STATE_FILE", "state.json"))
MAX_READING_AGE_MINUTES = int(os.environ.get("MAX_READING_AGE_MINUTES", "150"))
NOTIFICATION_INTERVAL_SECONDS = int(os.environ.get("NOTIFICATION_INTERVAL_SECONDS", "3600"))

# (low, high inclusive, band name, suggested action)
BANDS = [
    (0, 55, "Normal", "Consider ventilation when outdoor conditions are suitable."),
    (56, 150, "Elevated", "Close doors/windows where practical and use a suitable air purifier if available."),
    (151, 250, "High", "Reduce outdoor particle entry and exposure; follow NEA guidance."),
    (251, float("inf"), "Very High", "Minimise outdoor particle entry and follow current NEA health advisories."),
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pm25monitor")


class DataError(Exception):
    """Raised when the PM2.5 reading cannot be safely obtained."""


@dataclass
class Reading:
    value: float
    observation_time: datetime  # tz-aware, Asia/Singapore


def classify(value: float) -> tuple[str, str]:
    """Return (band_name, suggested_action) for a PM2.5 value. Raises ValueError if invalid."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid PM2.5 value: {value!r}")
    if value < 0:
        raise ValueError(f"PM2.5 value cannot be negative: {value!r}")
    for lo, hi, name, action in BANDS:
        if lo <= value <= hi:
            return name, action
    raise ValueError(f"PM2.5 value out of range: {value!r}")


def fetch_reading(timeout: int = 15) -> Reading:
    """Fetch and validate the latest Central region 1-hour PM2.5 reading."""
    try:
        req = urlrequest.Request(API_URL, headers={"User-Agent": "pm25-monitor/1.0"})
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urlerror.URLError, TimeoutError) as exc:
        raise DataError(f"API request failed: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataError(f"API returned invalid JSON: {exc}") from exc

    return parse_api_response(payload)


def parse_api_response(payload: dict) -> Reading:
    """Extract and validate the Central reading from a parsed API response."""
    if not isinstance(payload, dict):
        raise DataError("API response was not a JSON object")
    if payload.get("code") != 0:
        raise DataError(f"API returned an error: {payload.get('errorMsg') or payload.get('code')}")

    data = payload.get("data") or {}
    items = data.get("items") or []
    if not items:
        raise DataError("API response contained no observation items")

    # Items are expected ordered; pick the one with the latest timestamp defensively.
    def _ts(item):
        try:
            return datetime.fromisoformat(item["timestamp"])
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    item = max(items, key=_ts)

    timestamp_raw = item.get("timestamp")
    if not timestamp_raw:
        raise DataError("Observation missing timestamp")
    try:
        observation_time = datetime.fromisoformat(timestamp_raw)
    except ValueError as exc:
        raise DataError(f"Observation timestamp unparsable: {timestamp_raw!r}") from exc
    if observation_time.tzinfo is None:
        observation_time = observation_time.replace(tzinfo=SG_TZ)
    observation_time = observation_time.astimezone(SG_TZ)

    readings = (item.get("readings") or {}).get("pm25_one_hourly") or {}
    value = readings.get(REGION)
    if value is None:
        raise DataError(f"'{REGION}' reading missing from pm25_one_hourly")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError(f"'{REGION}' reading is not numeric: {value!r}")
    if value < 0:
        raise DataError(f"'{REGION}' reading is negative: {value!r}")

    return Reading(value=float(value), observation_time=observation_time)


def is_stale(observation_time: datetime, now: Optional[datetime] = None,
             max_age_minutes: int = MAX_READING_AGE_MINUTES) -> bool:
    """Return True if the observation is older than max_age_minutes relative to now."""
    now = now or datetime.now(SG_TZ)
    age = now - observation_time
    return age > timedelta(minutes=max_age_minutes)


def build_subject(value: float, band: str) -> str:
    return f"[PM2.5 Alert] {REGION_LABEL} region: {value:.0f} ug/m3 ({band})"


def build_body(value: float, band: str, action: str, observation_time: datetime,
               previous_band: Optional[str] = None) -> str:
    lines = []
    if previous_band and previous_band != band:
        lines.append(f"** AIR QUALITY BAND CHANGED: {previous_band} -> {band} **")
        lines.append("")
    lines += [
        f"Region: {REGION_LABEL}",
        f"1-hour PM2.5 reading: {value:.0f} ug/m3",
        f"Classification: {band}",
        f"Observation time (Asia/Singapore): {observation_time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
        f"Suggested action: {action}",
        "",
        "Note: this reading is an outdoor measurement and does not guarantee indoor air quality.",
        "Indoor levels depend on ventilation, filtration, and building conditions.",
        "",
        f"More information: {HAZE_URL}",
    ]
    return "\n".join(lines)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read state file %s; starting fresh", STATE_FILE)
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state))
    except OSError as exc:
        logger.warning("Could not write state file %s: %s", STATE_FILE, exc)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def live_sending_enabled(dry_run: bool) -> bool:
    """Real emails only go out when not forced dry-run AND explicitly enabled via env."""
    return not dry_run and _env_truthy("ENABLE_LIVE_EMAIL")


def get_gmail_credentials() -> tuple[str, str, str]:
    address = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("ALERT_RECIPIENT_EMAIL")
    missing = [n for n, v in (
        ("GMAIL_ADDRESS", address),
        ("GMAIL_APP_PASSWORD", app_password),
        ("ALERT_RECIPIENT_EMAIL", recipient),
    ) if not v]
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")
    return address, app_password, recipient


def send_email(subject: str, body: str, dry_run: bool) -> None:
    """Send via Gmail SMTP, or simulate when dry_run is True / live sending disabled."""
    if not live_sending_enabled(dry_run):
        logger.info("[DRY RUN] Would send email.\nSubject: %s\n%s", subject, body)
        return

    address, app_password, recipient = get_gmail_credentials()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = recipient
    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(address, app_password)
        smtp.send_message(msg)
    logger.info("Alert email sent to %s", recipient)


def run_once(dry_run: bool) -> int:
    """Perform one fetch-classify-notify cycle. Returns process exit code (0 = ok)."""
    try:
        reading = fetch_reading()
    except DataError as exc:
        logger.error("Failed to obtain PM2.5 reading: %s", exc)
        return 1

    now = datetime.now(SG_TZ)
    if is_stale(reading.observation_time, now):
        logger.error(
            "Observation is stale (observed %s, now %s); not sending an alert.",
            reading.observation_time.isoformat(), now.isoformat(),
        )
        return 1

    try:
        band, action = classify(reading.value)
    except ValueError as exc:
        logger.error("Invalid reading, cannot classify: %s", exc)
        return 1

    state = load_state()
    last_obs = state.get("last_sent_observation")
    if last_obs == reading.observation_time.isoformat():
        logger.info("Observation %s already notified; skipping duplicate.", last_obs)
        return 0

    previous_band = state.get("last_sent_band")
    subject = build_subject(reading.value, band)
    body = build_body(reading.value, band, action, reading.observation_time, previous_band)

    try:
        send_email(subject, body, dry_run)
    except Exception as exc:  # smtplib / credential errors
        logger.error("Failed to send alert email: %s", exc)
        return 1

    if live_sending_enabled(dry_run):
        state["last_sent_observation"] = reading.observation_time.isoformat()
        state["last_sent_band"] = band
        save_state(state)

    return 0


def run_test_email(dry_run: bool) -> int:
    subject = f"[PM2.5 Monitor] Test email ({REGION_LABEL})"
    body = (
        "This is a test email from the PM2.5 monitor.\n"
        f"Sent at (Asia/Singapore): {datetime.now(SG_TZ).strftime('%Y-%m-%d %H:%M:%S %z')}\n"
        f"More information: {HAZE_URL}\n"
    )
    if not dry_run and not _env_truthy("ENABLE_LIVE_EMAIL"):
        logger.error(
            "Refusing to send a live test email: set ENABLE_LIVE_EMAIL=true to allow real delivery, "
            "or pass --dry-run to simulate."
        )
        return 1
    try:
        send_email(subject, body, dry_run)
    except Exception as exc:
        logger.error("Test email failed: %s", exc)
        return 1
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Singapore Central region PM2.5 and email alerts.")
    parser.add_argument("--dry-run", action="store_true", help="Simulate only; never sends a real email.")
    parser.add_argument("--once", action="store_true", help="Run a single check-and-notify cycle then exit.")
    parser.add_argument("--test-email", action="store_true", help="Send/simulate a test email and exit.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if not args.dry_run and not _env_truthy("ENABLE_LIVE_EMAIL"):
        logger.info("ENABLE_LIVE_EMAIL is not set to true; running in dry-run (simulate) mode.")

    if args.test_email:
        return run_test_email(args.dry_run)

    if args.once:
        return run_once(args.dry_run)

    logger.info("Starting hourly loop (interval=%ss). Press Ctrl+C to stop.", NOTIFICATION_INTERVAL_SECONDS)
    while True:
        try:
            run_once(args.dry_run)
        except Exception as exc:  # keep the loop alive across unexpected errors
            logger.error("Unexpected error in monitoring cycle: %s", exc)
        time.sleep(NOTIFICATION_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
