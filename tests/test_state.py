"""
Unit tests for AlertStateManager and AlertDecision state machine.
Verifies edge detection (UNAVAILABLE -> AVAILABLE), cooldown suppression,
reminder alerts, state reset on UNAVAILABLE, transient error immunity,
monotonic time tracking, and state file persistence/reloading.

Zero third-party test dependencies; standard library unittest.
"""
from dataclasses import FrozenInstanceError
import json
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from allaccess_monitor.models import AvailabilityStatus, CheckResult, SectionInfo
from allaccess_monitor.state import AlertStateManager, AlertDecision


class TestAlertDecisionContract(unittest.TestCase):
    """Verifies AlertDecision dataclass properties."""

    def test_alert_decision_defaults(self):
        """Default values: should_alert mandatory, is_reminder=False, reason=''."""
        d = AlertDecision(should_alert=True)
        self.assertTrue(d.should_alert)
        self.assertFalse(d.is_reminder)
        self.assertEqual(d.reason, "")

    def test_alert_decision_custom_values(self):
        """Custom values for reminder decision."""
        d = AlertDecision(should_alert=True, is_reminder=True, reason="Cooldown expired")
        self.assertTrue(d.should_alert)
        self.assertTrue(d.is_reminder)
        self.assertEqual(d.reason, "Cooldown expired")


# ==============================================================================
# Edge Detection & Cooldown Throttling Tests
# ==============================================================================

class TestAlertStateManagerTransitions(unittest.TestCase):
    """Verifies state machine transitions between UNAVAILABLE, AVAILABLE, and REMINDER."""

    def setUp(self):
        self.manager = AlertStateManager(cooldown_seconds=1800)

    def test_initial_state_is_clean(self):
        """Fresh manager starts in unavailable state with no alerts sent."""
        self.assertFalse(self.manager.is_currently_available)
        self.assertIsNone(self.manager.last_alert_timestamp)

    def test_first_available_triggers_immediate_alert(self):
        """Transition UNAVAILABLE -> AVAILABLE returns should_alert=True, is_reminder=False."""
        decision = self.manager.process_check_result(is_available=True, current_time=1000.0)
        self.assertTrue(decision.should_alert)
        self.assertFalse(decision.is_reminder)
        self.assertTrue(self.manager.is_currently_available)
        self.assertEqual(self.manager.last_alert_timestamp, 1000.0)

    def test_consecutive_unavailable_never_alerts(self):
        """Multiple UNAVAILABLE checks return should_alert=False."""
        d1 = self.manager.process_check_result(is_available=False, current_time=1000.0)
        d2 = self.manager.process_check_result(is_available=False, current_time=1060.0)
        d3 = self.manager.process_check_result(is_available=False, current_time=1120.0)

        self.assertFalse(d1.should_alert)
        self.assertFalse(d2.should_alert)
        self.assertFalse(d3.should_alert)
        self.assertFalse(self.manager.is_currently_available)

    def test_cooldown_throttling_steady_state(self):
        """Available checks within cooldown_seconds are throttled (should_alert=False)."""
        # Initial alert at t=1000
        self.manager.process_check_result(is_available=True, current_time=1000.0)

        # Subsequent checks within 1800s window
        for t in [1001.0, 1060.0, 1500.0, 2799.0]:
            decision = self.manager.process_check_result(is_available=True, current_time=t)
            self.assertFalse(decision.should_alert, f"Alert should be throttled at t={t}")
            self.assertIn("cooldown", decision.reason.lower())

    def test_exact_cooldown_boundary_triggers_reminder(self):
        """At exactly current_time == last_alert + cooldown_seconds, reminder triggers."""
        self.manager.process_check_result(is_available=True, current_time=1000.0)

        # Exactly 1800s later (t=2800.0)
        decision = self.manager.process_check_result(is_available=True, current_time=2800.0)
        self.assertTrue(decision.should_alert)
        self.assertTrue(decision.is_reminder)
        self.assertEqual(self.manager.last_alert_timestamp, 2800.0)

    def test_past_cooldown_boundary_triggers_reminder(self):
        """Check at current_time > last_alert + cooldown_seconds triggers reminder."""
        self.manager.process_check_result(is_available=True, current_time=1000.0)

        # 2000s later (t=3000.0)
        decision = self.manager.process_check_result(is_available=True, current_time=3000.0)
        self.assertTrue(decision.should_alert)
        self.assertTrue(decision.is_reminder)
        self.assertEqual(self.manager.last_alert_timestamp, 3000.0)

    def test_reminder_resets_cooldown_window(self):
        """Sending a reminder alert restarts the cooldown timer from the reminder timestamp."""
        self.manager.process_check_result(is_available=True, current_time=1000.0)
        # Reminder at t=2800.0
        self.manager.process_check_result(is_available=True, current_time=2800.0)

        # Check at t=2860.0 (60s after reminder) is throttled!
        d_throttled = self.manager.process_check_result(is_available=True, current_time=2860.0)
        self.assertFalse(d_throttled.should_alert)

        # Check at t=4600.0 (2800 + 1800) triggers second reminder
        d_reminder2 = self.manager.process_check_result(is_available=True, current_time=4600.0)
        self.assertTrue(d_reminder2.should_alert)
        self.assertTrue(d_reminder2.is_reminder)

    def test_sold_out_resets_cooldown_timer(self):
        """
        Transition to UNAVAILABLE resets availability state.
        When tickets reappear, an immediate FIRST alert is sent without waiting for cooldown!
        """
        # Initial alert at t=1000
        d1 = self.manager.process_check_result(is_available=True, current_time=1000.0)
        self.assertTrue(d1.should_alert)

        # Sold out at t=1100
        d2 = self.manager.process_check_result(is_available=False, current_time=1100.0)
        self.assertFalse(d2.should_alert)
        self.assertFalse(self.manager.is_currently_available)

        # Tickets reappear at t=1160 (only 160s after first alert, well within 1800s cooldown)
        d3 = self.manager.process_check_result(is_available=True, current_time=1160.0)
        self.assertTrue(d3.should_alert, "Tickets reappearing must trigger immediate alert!")
        self.assertFalse(d3.is_reminder, "Must be a first detection alert, not a reminder!")


# ==============================================================================
# Error Immunity & Edge Cases
# ==============================================================================

class TestAlertStateManagerErrorImmunity(unittest.TestCase):
    """Verifies that transient errors do not corrupt state or trigger duplicate alerts."""

    def setUp(self):
        self.manager = AlertStateManager(cooldown_seconds=1800)

    def test_transient_error_preserves_available_state(self):
        """
        Scenario:
        1. Tickets available at t=1000 -> Alert sent.
        2. Network glitch / HTTP 502 / WAF at t=1060 -> Error result.
        3. Tickets still available at t=1120 -> Must NOT send duplicate initial alert!
        """
        # 1. Available -> Alert
        d1 = self.manager.process_check_result(is_available=True, current_time=1000.0)
        self.assertTrue(d1.should_alert)

        # 2. CheckResult with ERROR status at t=1060
        error_result = CheckResult(
            status=AvailabilityStatus.ERROR,
            section_name="Sivori media",
            error_message="HTTP 502 Bad Gateway"
        )
        d_error = self.manager.process_check_result(is_available=error_result, current_time=1060.0)
        self.assertFalse(d_error.should_alert)
        self.assertTrue(self.manager.is_currently_available, "State must remain AVAILABLE despite error!")

        # 3. Available at t=1120 (within 1800s cooldown) -> Must be throttled!
        d3 = self.manager.process_check_result(is_available=True, current_time=1120.0)
        self.assertFalse(d3.should_alert, "Must suppress alert during cooldown after transient error!")

    def test_transient_error_preserves_unavailable_state(self):
        """Errors while UNAVAILABLE keep the state UNAVAILABLE without alerting."""
        d1 = self.manager.process_check_result(is_available=False, current_time=1000.0)
        self.assertFalse(d1.should_alert)

        error_result = CheckResult(status=AvailabilityStatus.ERROR, section_name="Sivori", error_message="Timeout")
        d_error = self.manager.process_check_result(is_available=error_result, current_time=1060.0)
        self.assertFalse(d_error.should_alert)
        self.assertFalse(self.manager.is_currently_available)

    def test_is_error_flag_kwarg(self):
        """Accepts explicit is_error=True keyword argument."""
        self.manager.process_check_result(is_available=True, current_time=1000.0)
        d = self.manager.process_check_result(is_available=False, current_time=1060.0, is_error=True)
        self.assertFalse(d.should_alert)
        self.assertTrue(self.manager.is_currently_available)


class TestAlertStateManagerEdgeCases(unittest.TestCase):
    """Verifies clock skew, zero cooldown, and explicit reset."""

    def test_zero_cooldown_dispatches_every_available_tick(self):
        """When cooldown_seconds=0, every check while available alerts."""
        manager = AlertStateManager(cooldown_seconds=0)
        d1 = manager.process_check_result(is_available=True, current_time=1000.0)
        d2 = manager.process_check_result(is_available=True, current_time=1010.0)
        d3 = manager.process_check_result(is_available=True, current_time=1020.0)

        self.assertTrue(d1.should_alert)
        self.assertTrue(d2.should_alert)
        self.assertTrue(d3.should_alert)

    def test_clock_skew_backward(self):
        """System clock jumping backward is handled safely without crashing."""
        manager = AlertStateManager(cooldown_seconds=1800)
        manager.process_check_result(is_available=True, current_time=2000.0)

        # Clock jumps back to 1500.0
        decision = manager.process_check_result(is_available=True, current_time=1500.0)
        self.assertFalse(decision.should_alert)

    def test_default_current_time_uses_system_clock(self):
        """When current_time is omitted, manager defaults to time.monotonic() or time.time()."""
        manager = AlertStateManager(cooldown_seconds=1800)
        decision = manager.process_check_result(is_available=True)
        self.assertTrue(decision.should_alert)
        self.assertIsNotNone(manager.last_alert_timestamp)

    def test_explicit_reset_method(self):
        """state.reset() restores initial state."""
        manager = AlertStateManager(cooldown_seconds=1800)
        manager.process_check_result(is_available=True, current_time=1000.0)
        self.assertTrue(manager.is_currently_available)

        manager.reset()
        self.assertFalse(manager.is_currently_available)
        self.assertIsNone(manager.last_alert_timestamp)


# ==============================================================================
# State File Persistence Tests
# ==============================================================================

class TestAlertStateManagerPersistence(unittest.TestCase):
    """Verifies state file save, reload, corrupt file recovery, and disk error resilience."""

    def test_state_file_save_and_load(self):
        """State persists across process restarts via state file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / ".monitor_state.json"

            # Instance 1: Alert at t=1000.0
            manager1 = AlertStateManager(cooldown_seconds=1800, state_file=state_file)
            decision1 = manager1.process_check_result(is_available=True, current_time=1000.0)
            self.assertTrue(decision1.should_alert)
            self.assertTrue(state_file.exists())

            # Read file directly to verify contents
            data = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertTrue(data.get("is_currently_available"))
            self.assertEqual(data.get("last_alert_timestamp"), 1000.0)

            # Instance 2 (Simulated restart): instantiated with same state file
            manager2 = AlertStateManager(cooldown_seconds=1800, state_file=state_file)
            self.assertTrue(manager2.is_currently_available)
            self.assertEqual(manager2.last_alert_timestamp, 1000.0)

            # Check at t=1500.0 (500s later) must be throttled!
            decision2 = manager2.process_check_result(is_available=True, current_time=1500.0)
            self.assertFalse(decision2.should_alert)

    def test_corrupted_state_file_recovery(self):
        """Corrupted JSON in state file resets to fresh state without crashing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / ".monitor_state.json"
            state_file.write_text("{ corrupt json syntax ...", encoding="utf-8")

            # Must recover gracefully
            manager = AlertStateManager(state_file=state_file)
            self.assertFalse(manager.is_currently_available)
            self.assertIsNone(manager.last_alert_timestamp)

            # First check alerts cleanly
            decision = manager.process_check_result(is_available=True, current_time=1000.0)
            self.assertTrue(decision.should_alert)

    def test_missing_state_file_handled(self):
        """Non-existent state file path does not raise error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "does_not_exist.json"
            manager = AlertStateManager(state_file=state_file)
            self.assertFalse(manager.is_currently_available)

    def test_unwritable_directory_resilience(self):
        """PermissionError on saving state file logs warning without crashing daemon."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "subfolder" / "state.json"
            manager = AlertStateManager(state_file=state_file)

            with patch.object(Path, "write_text", side_effect=OSError("Disk read-only")):
                # Should not raise exception
                decision = manager.process_check_result(is_available=True, current_time=1000.0)
                self.assertTrue(decision.should_alert)


if __name__ == "__main__":
    unittest.main()
