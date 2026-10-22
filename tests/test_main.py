"""Basic automated tests for main.py."""
import copy
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402

SAMPLE_PAYLOAD = {
    "code": 0,
    "data": {
        "items": [
            {
                "date": "2026-10-21",
                "updatedTimestamp": "2026-10-21T17:30:43+08:00",
                "timestamp": "2026-10-21T17:00:00+08:00",
                "readings": {
                    "pm25_one_hourly": {
                        "north": 29, "south": 56, "west": 74, "east": 47, "central": 57
                    }
                },
            }
        ]
    },
    "errorMsg": "",
}


class ClassifyTests(unittest.TestCase):
    def test_normal_boundaries(self):
        self.assertEqual(main.classify(0)[0], "Normal")
        self.assertEqual(main.classify(55)[0], "Normal")

    def test_elevated_boundaries(self):
        self.assertEqual(main.classify(56)[0], "Elevated")
        self.assertEqual(main.classify(150)[0], "Elevated")

    def test_high_boundaries(self):
        self.assertEqual(main.classify(151)[0], "High")
        self.assertEqual(main.classify(250)[0], "High")

    def test_very_high(self):
        self.assertEqual(main.classify(251)[0], "Very High")
        self.assertEqual(main.classify(1000)[0], "Very High")

    def test_invalid_values(self):
        with self.assertRaises(ValueError):
            main.classify(-1)
        with self.assertRaises(ValueError):
            main.classify(None)
        with self.assertRaises(ValueError):
            main.classify("57")


class ParseApiResponseTests(unittest.TestCase):
    def test_valid_payload(self):
        reading = main.parse_api_response(SAMPLE_PAYLOAD)
        self.assertEqual(reading.value, 57.0)
        self.assertEqual(reading.observation_time.utcoffset(), timedelta(hours=8))

    def test_missing_items(self):
        payload = copy.deepcopy(SAMPLE_PAYLOAD)
        payload["data"]["items"] = []
        with self.assertRaises(main.DataError):
            main.parse_api_response(payload)

    def test_error_code(self):
        payload = copy.deepcopy(SAMPLE_PAYLOAD)
        payload["code"] = 1
        payload["errorMsg"] = "boom"
        with self.assertRaises(main.DataError):
            main.parse_api_response(payload)

    def test_missing_central_field(self):
        payload = copy.deepcopy(SAMPLE_PAYLOAD)
        del payload["data"]["items"][0]["readings"]["pm25_one_hourly"]["central"]
        with self.assertRaises(main.DataError):
            main.parse_api_response(payload)

    def test_negative_reading(self):
        payload = copy.deepcopy(SAMPLE_PAYLOAD)
        payload["data"]["items"][0]["readings"]["pm25_one_hourly"]["central"] = -5
        with self.assertRaises(main.DataError):
            main.parse_api_response(payload)

    def test_non_numeric_reading(self):
        payload = copy.deepcopy(SAMPLE_PAYLOAD)
        payload["data"]["items"][0]["readings"]["pm25_one_hourly"]["central"] = "high"
        with self.assertRaises(main.DataError):
            main.parse_api_response(payload)


class StalenessTests(unittest.TestCase):
    def test_fresh_is_not_stale(self):
        now = datetime(2026, 10, 21, 17, 30, tzinfo=main.SG_TZ)
        obs = datetime(2026, 10, 21, 17, 0, tzinfo=main.SG_TZ)
        self.assertFalse(main.is_stale(obs, now, max_age_minutes=150))

    def test_old_is_stale(self):
        now = datetime(2026, 10, 21, 20, 0, tzinfo=main.SG_TZ)
        obs = datetime(2026, 10, 21, 17, 0, tzinfo=main.SG_TZ)
        self.assertTrue(main.is_stale(obs, now, max_age_minutes=150))


class MessageGenerationTests(unittest.TestCase):
    def test_subject_contains_region_value_band(self):
        subject = main.build_subject(57, "Elevated")
        self.assertIn("Central", subject)
        self.assertIn("57", subject)
        self.assertIn("Elevated", subject)

    def test_body_contains_required_elements(self):
        obs = datetime(2026, 10, 21, 17, 0, tzinfo=main.SG_TZ)
        body = main.build_body(57, "Elevated", "Close doors/windows.", obs)
        self.assertIn("57", body)
        self.assertIn("Elevated", body)
        self.assertIn("17:00", body)
        self.assertIn("Close doors/windows.", body)

    def test_band_change_notice(self):
        obs = datetime(2026, 10, 21, 17, 0, tzinfo=main.SG_TZ)
        body = main.build_body(57, "Elevated", "action", obs, previous_band="Normal")
        self.assertIn("BAND CHANGED", body)
        self.assertIn("Normal -> Elevated", body)

    def test_no_band_change_notice_when_same(self):
        obs = datetime(2026, 10, 21, 17, 0, tzinfo=main.SG_TZ)
        body = main.build_body(57, "Elevated", "action", obs, previous_band="Elevated")
        self.assertNotIn("BAND CHANGED", body)


class DryRunSafetyTests(unittest.TestCase):
    def test_dry_run_never_opens_smtp(self):
        with mock.patch.object(main.smtplib, "SMTP") as smtp_mock:
            main.send_email("subject", "body", dry_run=True)
            smtp_mock.assert_not_called()

    def test_live_disabled_by_default_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_LIVE_EMAIL", None)
            with mock.patch.object(main.smtplib, "SMTP") as smtp_mock:
                main.send_email("subject", "body", dry_run=False)
                smtp_mock.assert_not_called()

    def test_live_requires_explicit_env_true(self):
        with mock.patch.dict(os.environ, {"ENABLE_LIVE_EMAIL": "true",
                                           "GMAIL_ADDRESS": "a@gmail.com",
                                           "GMAIL_APP_PASSWORD": "pw",
                                           "ALERT_RECIPIENT_EMAIL": "b@example.com"}):
            with mock.patch.object(main.smtplib, "SMTP") as smtp_mock:
                smtp_instance = smtp_mock.return_value.__enter__.return_value
                main.send_email("subject", "body", dry_run=False)
                smtp_mock.assert_called_once()
                smtp_instance.starttls.assert_called_once()
                smtp_instance.login.assert_called_once_with("a@gmail.com", "pw")

    def test_dry_run_flag_overrides_env(self):
        with mock.patch.dict(os.environ, {"ENABLE_LIVE_EMAIL": "true"}):
            with mock.patch.object(main.smtplib, "SMTP") as smtp_mock:
                main.send_email("subject", "body", dry_run=True)
                smtp_mock.assert_not_called()

    def test_multiple_comma_separated_recipients(self):
        with mock.patch.dict(os.environ, {"ENABLE_LIVE_EMAIL": "true",
                                           "GMAIL_ADDRESS": "a@gmail.com",
                                           "GMAIL_APP_PASSWORD": "pw",
                                           "ALERT_RECIPIENT_EMAIL": "b@example.com, c@example.com"}):
            with mock.patch.object(main.smtplib, "SMTP") as smtp_mock:
                main.send_email("subject", "body", dry_run=False)
                sent_msg = smtp_mock.return_value.__enter__.return_value.send_message.call_args[0][0]
                self.assertEqual(sent_msg["To"], "b@example.com, c@example.com")


class RunOnceDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.state_file = Path("test_state.json")
        if self.state_file.exists():
            self.state_file.unlink()
        self._orig_state_file = main.STATE_FILE
        main.STATE_FILE = self.state_file

    def tearDown(self):
        main.STATE_FILE = self._orig_state_file
        if self.state_file.exists():
            self.state_file.unlink()

    def test_duplicate_observation_skipped(self):
        reading = main.Reading(value=57.0, observation_time=datetime(2026, 10, 21, 17, 0, tzinfo=main.SG_TZ))
        main.STATE_FILE.write_text(
            '{"last_sent_observation": "2026-10-21T17:00:00+08:00", "last_sent_band": "Elevated"}'
        )
        with mock.patch.object(main, "fetch_reading", return_value=reading), \
             mock.patch.object(main, "is_stale", return_value=False), \
             mock.patch.object(main, "send_email") as send_mock:
            result = main.run_once(dry_run=True)
        self.assertEqual(result, 0)
        send_mock.assert_not_called()

    def test_normal_band_does_not_send(self):
        reading = main.Reading(value=38.0, observation_time=datetime(2026, 10, 21, 17, 0, tzinfo=main.SG_TZ))
        with mock.patch.object(main, "fetch_reading", return_value=reading), \
             mock.patch.object(main, "is_stale", return_value=False), \
             mock.patch.object(main, "send_email") as send_mock:
            result = main.run_once(dry_run=True)
        self.assertEqual(result, 0)
        send_mock.assert_not_called()

    def test_elevated_band_sends(self):
        reading = main.Reading(value=57.0, observation_time=datetime(2026, 10, 21, 17, 0, tzinfo=main.SG_TZ))
        with mock.patch.object(main, "fetch_reading", return_value=reading), \
             mock.patch.object(main, "is_stale", return_value=False), \
             mock.patch.object(main, "send_email") as send_mock:
            result = main.run_once(dry_run=True)
        self.assertEqual(result, 0)
        send_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
