"""
Empirical Challenge & Adversarial Stress Test Suite for Milestone 2 (Challenger 2).

Adversarial Focus Areas:
1. State Throttler Robustness:
   - Boundary cooldown conditions & sub-millisecond precision
   - High-frequency check polling (10,000 continuous checks, rapid flapping)
   - Transient check error state freezing & immunity
   - Corrupted/unparseable state file recovery & atomic persistence
   - Backward clock skew tolerance
2. Notification Delivery & Isolation:
   - Windows console non-ASCII / UnicodeEncodeError resilience
   - CompositeNotifier multiplexing, multi-channel exception isolation, and delivery report accuracy
   - Payload escaping and rate-limit recovery across messaging channels

Zero third-party test dependencies; standard library unittest only.
Zero live network calls; 100% deterministic mocking.
"""
from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path
import tempfile
import time
from typing import List, Optional, TextIO
import unittest
from unittest.mock import MagicMock, patch

from allaccess_monitor.models import (
    AvailabilityStatus,
    CheckResult,
    SectionInfo,
    TicketAlert,
)
from allaccess_monitor.state import AlertStateManager, AlertDecision
from allaccess_monitor.notifier.base import BaseNotifier
from allaccess_monitor.notifier.console import ConsoleNotifier
from allaccess_monitor.notifier.composite import CompositeNotifier
from allaccess_monitor.notifier.telegram import TelegramNotifier
from allaccess_monitor.notifier.discord import DiscordNotifier
from allaccess_monitor.notifier.email import EmailNotifier


def make_test_alert(
    event_name: str = "Foo Fighters",
    section_name: str = "Sivori media",
    status: str = "AVAILABLE",
    event_url: str = "https://www.allaccess.com.ar/event/foo-fighters",
    price: Optional[float] = 125000.0,
    is_reminder: bool = False,
) -> TicketAlert:
    """Helper to construct standardized TicketAlert fixtures."""
    return TicketAlert(
        event_name=event_name,
        section_name=section_name,
        status=status,
        event_url=event_url,
        price=price,
        currency="ARS",
        timestamp=datetime.now(timezone.utc),
        is_reminder=is_reminder,
    )


# ==============================================================================
# Category 1: Cooldown Boundary Precision & Sub-Millisecond Exactness
# ==============================================================================

class TestCooldownBoundaryPrecisionAdversarial(unittest.TestCase):
    """Adversarial boundary stress tests on cooldown thresholds and time math."""

    def test_cooldown_sub_millisecond_exactness(self):
        """
        Verify exact sub-millisecond precision on cooldown boundary:
        t_alert = 1000.0, cooldown = 1800.0 (expiry = 2800.0).
        t = 2799.999 -> throttled
        t = 2799.999999 -> throttled
        t = 2800.000000 -> reminder alert triggered!
        t = 2800.000001 -> throttled again
        """
        manager = AlertStateManager(cooldown_seconds=1800)
        
        # Initial rising edge at t = 1000.0
        d0 = manager.process_check_result(is_available=True, current_time=1000.0)
        self.assertTrue(d0.should_alert)
        self.assertFalse(d0.is_reminder)
        self.assertEqual(manager.last_alert_time, 1000.0)

        # 1 millisecond before boundary (2799.999) -> throttled
        d1 = manager.process_check_result(is_available=True, current_time=2799.999)
        self.assertFalse(d1.should_alert)
        self.assertIn("throttled", d1.reason.lower())

        # 1 microsecond before boundary (2799.999999) -> throttled
        d2 = manager.process_check_result(is_available=True, current_time=2799.999999)
        self.assertFalse(d2.should_alert)

        # Exact boundary (2800.000000) -> reminder alert!
        d3 = manager.process_check_result(is_available=True, current_time=2800.0)
        self.assertTrue(d3.should_alert)
        self.assertTrue(d3.is_reminder)
        self.assertEqual(manager.last_alert_time, 2800.0)

        # 1 microsecond after new reminder (2800.000001) -> throttled under new window
        d4 = manager.process_check_result(is_available=True, current_time=2800.000001)
        self.assertFalse(d4.should_alert)

        # 1 millisecond after new reminder (2800.001) -> throttled
        d5 = manager.process_check_result(is_available=True, current_time=2800.001)
        self.assertFalse(d5.should_alert)

        # Exact boundary for second reminder (4600.0) -> second reminder alert!
        d6 = manager.process_check_result(is_available=True, current_time=4600.0)
        self.assertTrue(d6.should_alert)
        self.assertTrue(d6.is_reminder)
        self.assertEqual(manager.total_alerts_sent, 3)

    def test_zero_cooldown_triggers_alert_on_every_available_tick(self):
        """Zero cooldown (cooldown_seconds=0) must alert on initial tick and remind on every subsequent available tick."""
        manager = AlertStateManager(cooldown_seconds=0)

        # Tick 1: initial edge
        d1 = manager.process_check_result(is_available=True, current_time=100.0)
        self.assertTrue(d1.should_alert)
        self.assertFalse(d1.is_reminder)

        # Tick 2: same timestamp (elapsed = 0.0 >= 0) -> reminder
        d2 = manager.process_check_result(is_available=True, current_time=100.0)
        self.assertTrue(d2.should_alert)
        self.assertTrue(d2.is_reminder)

        # Tick 3: subsequent timestamp -> reminder
        d3 = manager.process_check_result(is_available=True, current_time=100.1)
        self.assertTrue(d3.should_alert)
        self.assertTrue(d3.is_reminder)
        self.assertEqual(manager.total_alerts_sent, 3)

    def test_negative_cooldown_raises_value_error(self):
        """Constructing manager with negative cooldown must raise ValueError immediately."""
        with self.assertRaises(ValueError):
            AlertStateManager(cooldown_seconds=-1)

    def test_backward_clock_skew_resilience(self):
        """
        If system time jumps backward (NTP sync or clock adjustment):
        - AlertStateManager must NOT crash or emit spurious alerts.
        - Must log warning and adjust reference timestamp.
        - Once time advances forward by cooldown, reminder alerts resume normally.
        """
        manager = AlertStateManager(cooldown_seconds=300)

        # Initial alert at t = 2000.0
        d1 = manager.process_check_result(is_available=True, current_time=2000.0)
        self.assertTrue(d1.should_alert)
        self.assertEqual(manager.last_alert_time, 2000.0)

        # System clock jumps backward to t = 1500.0 (-500s skew)
        with self.assertLogs("allaccess_monitor.state", level="WARNING") as log_ctx:
            d2 = manager.process_check_result(is_available=True, current_time=1500.0)
            self.assertFalse(d2.should_alert)
            self.assertIn("Clock skew detected", log_ctx.output[0])
            self.assertEqual(manager.last_alert_time, 1500.0)

        # Check at t = 1700.0 (elapsed = 200s < 300s cooldown) -> throttled
        d3 = manager.process_check_result(is_available=True, current_time=1700.0)
        self.assertFalse(d3.should_alert)

        # Check at t = 1800.0 (elapsed = 300s >= 300s cooldown) -> reminder alert!
        d4 = manager.process_check_result(is_available=True, current_time=1800.0)
        self.assertTrue(d4.should_alert)
        self.assertTrue(d4.is_reminder)

    def test_explicit_reset_clears_state_and_allows_instant_re_alert(self):
        """Calling reset() transitions state to UNAVAILABLE and clears last_alert_time."""
        manager = AlertStateManager(cooldown_seconds=1800)
        manager.process_check_result(is_available=True, current_time=1000.0)
        self.assertTrue(manager.is_currently_available)

        manager.reset()
        self.assertFalse(manager.is_currently_available)
        self.assertIsNone(manager.last_alert_time)

        # Next available check alerts immediately even if 1 second later
        d = manager.process_check_result(is_available=True, current_time=1001.0)
        self.assertTrue(d.should_alert)
        self.assertFalse(d.is_reminder)


# ==============================================================================
# Category 2: High Frequency Polling Stress & Flapping
# ==============================================================================

class TestHighFrequencyPollingStressAdversarial(unittest.TestCase):
    """Stress tests simulating thousands of checks under rapid polling and flapping."""

    def test_10000_consecutive_checks_sustained_availability(self):
        """
        Simulate 10,000 rapid polling checks (e.g. 1 check/sec for 10,000 seconds)
        with sustained ticket availability and cooldown = 1800s.
        Mathematical expectation:
          t = 0: Initial Alert #1
          t = 1800: Reminder #2
          t = 3600: Reminder #3
          t = 5400: Reminder #4
          t = 7200: Reminder #5
          t = 9000: Reminder #6
        Total alerts MUST equal exactly 6.
        All remaining 9,994 checks MUST return should_alert = False.
        """
        cooldown = 1800
        manager = AlertStateManager(cooldown_seconds=cooldown)

        alerts_received = []
        reminders_received = []

        start_perf = time.perf_counter()
        for tick in range(10000):
            current_time = float(tick)
            decision = manager.process_check_result(is_available=True, current_time=current_time)
            if decision.should_alert:
                if decision.is_reminder:
                    reminders_received.append(tick)
                else:
                    alerts_received.append(tick)
        duration = time.perf_counter() - start_perf

        # Verify mathematical correctness
        self.assertEqual(len(alerts_received), 1)
        self.assertEqual(alerts_received, [0])

        expected_reminders = [1800, 3600, 5400, 7200, 9000]
        self.assertEqual(reminders_received, expected_reminders)
        self.assertEqual(manager.total_alerts_sent, 6)

        # Performance assertion: 10,000 in-memory checks must execute in < 0.5 seconds
        self.assertLess(duration, 0.5, f"10,000 checks took too long: {duration:.3f}s")

    def test_5000_checks_flapping_oscillation_stress(self):
        """
        Stress test rapid state flapping (oscillating between True and False 5,000 times).
        Every True represents UNAVAILABLE -> AVAILABLE (must alert with is_reminder=False).
        Every False represents AVAILABLE -> UNAVAILABLE (resets the state machine).
        Expected outcome: exactly 2,500 initial alerts, 0 reminders, exactly 2,500 state resets.
        """
        manager = AlertStateManager(cooldown_seconds=1800)

        initial_alerts = 0
        reminders = 0
        resets = 0

        for tick in range(5000):
            is_avail = (tick % 2 == 0)
            decision = manager.process_check_result(is_available=is_avail, current_time=float(tick))
            if decision.should_alert:
                if decision.is_reminder:
                    reminders += 1
                else:
                    initial_alerts += 1
            elif not is_avail and "reset" in decision.reason.lower():
                resets += 1

        self.assertEqual(initial_alerts, 2500)
        self.assertEqual(reminders, 0)
        self.assertEqual(resets, 2500)
        self.assertEqual(manager.total_alerts_sent, 2500)

    def test_5000_continuous_unavailable_checks_never_alerts(self):
        """5,000 continuous checks with is_available=False must NEVER fire any alert."""
        manager = AlertStateManager(cooldown_seconds=60)
        for tick in range(5000):
            decision = manager.process_check_result(is_available=False, current_time=float(tick))
            self.assertFalse(decision.should_alert)
        self.assertEqual(manager.total_alerts_sent, 0)


# ==============================================================================
# Category 3: Transient Error State Freezing & Immunity
# ==============================================================================

class TestTransientErrorImmunityAdversarial(unittest.TestCase):
    """Stress tests verifying that network errors NEVER trigger alerts or reset cooldown."""

    def test_interleaving_transient_errors_preserve_available_state(self):
        """
        Available -> 100 random transient errors -> Still Available within cooldown.
        Errors must freeze availability state and never trigger alerts.
        """
        manager = AlertStateManager(cooldown_seconds=1800)

        # Initial available detection at t = 100.0
        d_init = manager.process_check_result(is_available=True, current_time=100.0)
        self.assertTrue(d_init.should_alert)
        self.assertTrue(manager.is_currently_available)
        self.assertEqual(manager.last_alert_time, 100.0)

        # 100 error checks between t = 101.0 and t = 1500.0
        for i in range(1, 101):
            t = 100.0 + i * 10.0
            error_result = CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name="Sivori media",
                error_message="HTTP 503 Service Unavailable / AWS WAF Challenge",
            )
            d_err = manager.process_check_result(result=error_result, current_time=t)
            self.assertFalse(d_err.should_alert)
            self.assertIn("transient check error", d_err.reason.lower())
            self.assertEqual(manager.consecutive_errors, i)
            # State must remain available and alert timestamp unmodified
            self.assertTrue(manager.is_currently_available)
            self.assertEqual(manager.last_alert_time, 100.0)

        # Successful check at t = 1500.0 (within 1800s cooldown)
        d_avail = manager.process_check_result(is_available=True, current_time=1500.0)
        self.assertFalse(d_avail.should_alert)
        self.assertEqual(manager.consecutive_errors, 0)
        self.assertTrue(manager.is_currently_available)

        # Subsequent check at t = 1900.0 (cooldown expired: 1900 - 100 = 1800 >= 1800)
        d_remind = manager.process_check_result(is_available=True, current_time=1900.0)
        self.assertTrue(d_remind.should_alert)
        self.assertTrue(d_remind.is_reminder)

    def test_transient_errors_preserve_unavailable_state_without_spurious_alert(self):
        """
        Starting UNAVAILABLE: 50 errors occur.
        Errors must not accidentally set availability to True or trigger alerts.
        When genuine AVAILABLE check arrives, it must trigger immediate initial alert.
        """
        manager = AlertStateManager(cooldown_seconds=1800)
        self.assertFalse(manager.is_currently_available)

        for i in range(1, 51):
            d = manager.process_check_result(
                is_available=AvailabilityStatus.ERROR,
                current_time=float(i),
            )
            self.assertFalse(d.should_alert)
            self.assertFalse(manager.is_currently_available)
            self.assertEqual(manager.consecutive_errors, i)

        # Genuine ticket availability at t = 1000.0
        d_avail = manager.process_check_result(is_available=True, current_time=1000.0)
        self.assertTrue(d_avail.should_alert)
        self.assertFalse(d_avail.is_reminder)
        self.assertEqual(manager.consecutive_errors, 0)
        self.assertTrue(manager.is_currently_available)

    def test_sold_out_vs_error_distinction(self):
        """
        Sold out (UNAVAILABLE) genuine transition resets the state machine.
        A subsequent AVAILABLE check immediately triggers an alert.
        In contrast, an ERROR preserves the state without resetting.
        """
        manager = AlertStateManager(cooldown_seconds=1800)

        # Alert #1 at t = 100.0
        manager.process_check_result(is_available=True, current_time=100.0)
        self.assertTrue(manager.is_currently_available)

        # Transient error at t = 150.0 -> State preserved (still available)
        d_err = manager.process_check_result(is_available=AvailabilityStatus.ERROR, current_time=150.0)
        self.assertFalse(d_err.should_alert)
        self.assertTrue(manager.is_currently_available)

        # Genuine sold out (UNAVAILABLE) at t = 200.0 -> State reset!
        d_sold = manager.process_check_result(is_available=False, current_time=200.0)
        self.assertFalse(d_sold.should_alert)
        self.assertFalse(manager.is_currently_available)
        self.assertIsNone(manager.last_alert_time)

        # Tickets reappear at t = 250.0 (only 150s after initial alert!)
        # Because tickets genuinely sold out in between, this is a NEW rising edge: Alert immediately!
        d_new = manager.process_check_result(is_available=True, current_time=250.0)
        self.assertTrue(d_new.should_alert)
        self.assertFalse(d_new.is_reminder)
        self.assertEqual(manager.total_alerts_sent, 2)

    def test_all_error_representation_polymorphism(self):
        """Verify process_check_result handles CheckResult, AvailabilityStatus, is_error=True kwarg, and duck-typed objects."""
        manager = AlertStateManager()

        # 1. CheckResult with ERROR
        r1 = CheckResult(status=AvailabilityStatus.ERROR, section_name="Sivori")
        d1 = manager.process_check_result(r1)
        self.assertFalse(d1.should_alert)

        # 2. AvailabilityStatus.ERROR enum
        d2 = manager.process_check_result(AvailabilityStatus.ERROR)
        self.assertFalse(d2.should_alert)

        # 3. is_error=True kwarg
        d3 = manager.process_check_result(is_available=False, is_error=True)
        self.assertFalse(d3.should_alert)

        # 4. Duck-typed object with status=AvailabilityStatus.ERROR
        class CustomErrorObj:
            status = AvailabilityStatus.ERROR
        d4 = manager.process_check_result(CustomErrorObj())
        self.assertFalse(d4.should_alert)

        self.assertEqual(manager.consecutive_errors, 4)


# ==============================================================================
# Category 4: Corrupted State File Recovery & Atomic Persistence
# ==============================================================================

class TestCorruptedStateFileRecoveryAdversarial(unittest.TestCase):
    """Stress tests on corrupted, truncated, invalid-type, and permission-denied state files."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "monitor_state.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_corrupted_json_syntax_recovery(self):
        """Truncated or invalid JSON syntax must be handled gracefully without crashing."""
        self.state_path.write_text('{"is_currently_available": tru', encoding="utf-8")
        manager = AlertStateManager(state_file=self.state_path)
        self.assertFalse(manager.is_currently_available)
        self.assertIsNone(manager.last_alert_time)
        self.assertEqual(manager.consecutive_errors, 0)

    def test_corrupted_binary_garbage_and_null_bytes(self):
        """Binary garbage and null bytes in state file must be caught and recover cleanly."""
        self.state_path.write_bytes(b"\x00\x00\xff\xfe\x01\x02\x03\x04")
        manager = AlertStateManager(state_file=self.state_path)
        self.assertFalse(manager.is_currently_available)
        self.assertIsNone(manager.last_alert_time)

    def test_corrupted_non_dict_json_roots(self):
        """Valid JSON containing non-object roots (list, number, string, null) must recover fresh."""
        bad_payloads = [
            json.dumps([1, 2, 3]),
            json.dumps("just a string"),
            json.dumps(12345),
            json.dumps(True),
            json.dumps(None),
        ]
        for payload in bad_payloads:
            self.state_path.write_text(payload, encoding="utf-8")
            manager = AlertStateManager(state_file=self.state_path)
            self.assertFalse(manager.is_currently_available)
            self.assertIsNone(manager.last_alert_time)

    def test_corrupted_dict_invalid_data_types(self):
        """State file with invalid value types (e.g. string for timestamp, dict for error count)."""
        bad_data = {
            "is_currently_available": True,
            "last_alert_time": "not_a_valid_float_timestamp",
            "consecutive_errors": ["not", "an", "int"],
        }
        self.state_path.write_text(json.dumps(bad_data), encoding="utf-8")
        manager = AlertStateManager(state_file=self.state_path)
        self.assertFalse(manager.is_currently_available)
        self.assertIsNone(manager.last_alert_time)

    def test_state_file_path_is_existing_directory(self):
        """State file pointing to a directory instead of a regular file must be handled gracefully."""
        dir_path = Path(self.temp_dir.name) / "directory_state"
        dir_path.mkdir()
        manager = AlertStateManager(state_file=dir_path)
        self.assertFalse(manager.is_currently_available)

    def test_stale_tmp_file_overwritten_cleanly_during_save(self):
        """A stale .tmp file from an interrupted process must not prevent atomic save."""
        tmp_file = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        tmp_file.write_text("STALE ORPHANED TMP FILE", encoding="utf-8")

        manager = AlertStateManager(state_file=self.state_path)
        manager.process_check_result(is_available=True, current_time=1000.0)

        # Verify state_file exists and has valid JSON
        self.assertTrue(self.state_path.exists())
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertTrue(data["is_currently_available"])
        self.assertEqual(data["last_alert_time"], 1000.0)

    def test_roundtrip_save_and_reload_persistence(self):
        """Verify saved state matches state loaded in a new AlertStateManager instance."""
        manager1 = AlertStateManager(cooldown_seconds=600, state_file=self.state_path)
        manager1.process_check_result(is_available=True, current_time=12345.67)

        manager2 = AlertStateManager(cooldown_seconds=600, state_file=self.state_path)
        self.assertTrue(manager2.is_currently_available)
        self.assertEqual(manager2.last_alert_time, 12345.67)
        self.assertEqual(manager2.total_alerts_sent, 1)

    def test_unwritable_directory_resilience(self):
        """When saving to an unwritable path raises OSError, state manager logs warning and continues."""
        unwritable_path = Path(self.temp_dir.name) / "sub" / "state.json"
        manager = AlertStateManager(state_file=unwritable_path)
        with patch.object(Path, "mkdir", side_effect=OSError("Disk full or permission denied")):
            with self.assertLogs("allaccess_monitor.state", level="WARNING"):
                manager.process_check_result(is_available=True, current_time=100.0)
        # In-memory state remains valid despite disk error
        self.assertTrue(manager.is_currently_available)


# ==============================================================================
# Category 5: Windows Console & Non-ASCII Character Resilience
# ==============================================================================

class NonASCIIThrowingStream(io.StringIO):
    """Simulates a Windows terminal with strict ASCII or legacy cp1252 codepage."""

    def __init__(self, allowed_encoding: str = "ascii"):
        super().__init__()
        self.allowed_encoding = allowed_encoding

    def write(self, s: str) -> int:
        try:
            s.encode(self.allowed_encoding)
        except UnicodeEncodeError as exc:
            raise UnicodeEncodeError(self.allowed_encoding, s, exc.start, exc.end, "simulated legacy terminal") from None
        return super().write(s)


class TestConsoleNotifierWindowsResilienceAdversarial(unittest.TestCase):
    """Stress tests verifying ConsoleNotifier never crashes on Windows non-ASCII / codepage errors."""

    def test_console_notifier_unicode_encode_error_fallback(self):
        """
        When target stream raises UnicodeEncodeError (e.g. emojis/accents on Windows cp1252),
        ConsoleNotifier must catch it, write a sanitized ASCII fallback banner, and return True.
        """
        strict_stream = NonASCIIThrowingStream(allowed_encoding="ascii")
        notifier = ConsoleNotifier(stream=strict_stream)

        alert = make_test_alert(
            event_name="Foo Fighters 🎸 🤘 - Estadio Mâs Monumental",
            section_name="Platea Sívori Media 🎟️",
            price=125000.0,
        )

        success = notifier.send(alert)
        self.assertTrue(success, "ConsoleNotifier.send() should return True even when stream raises UnicodeEncodeError")

        output = strict_stream.getvalue()
        self.assertIn("TICKETS AVAILABLE", output)
        self.assertIn("Foo Fighters", output)
        self.assertIn("S", output)  # Sanitized replacement for accented characters
        # Verify the output is strictly valid ASCII
        output.encode("ascii")

    def test_console_notifier_cp1252_strict_resilience(self):
        """Test with cp1252 codepage containing non-cp1252 emoji and Asian symbols."""
        strict_stream = NonASCIIThrowingStream(allowed_encoding="cp1252")
        notifier = ConsoleNotifier(stream=strict_stream)

        alert = make_test_alert(
            event_name="Foo Fighters 🎸 [フ・ファイターズ] 🎵",
            section_name="Sívori media 🎟️",
        )

        success = notifier.send(alert)
        self.assertTrue(success)
        output = strict_stream.getvalue()
        self.assertTrue(len(output) > 0)

    def test_console_notifier_stream_write_oserror(self):
        """If stream raises OSError (e.g. broken pipe), ConsoleNotifier returns False without raising exception."""
        broken_stream = MagicMock()
        broken_stream.write.side_effect = BrokenPipeError("Errno 32 Broken pipe")
        notifier = ConsoleNotifier(stream=broken_stream)

        alert = make_test_alert()
        success = notifier.send(alert)
        self.assertFalse(success)

    def test_console_notifier_dynamic_stdout_resolution(self):
        """ConsoleNotifier(stream=None) resolves to sys.stdout dynamically."""
        notifier = ConsoleNotifier(stream=None)
        mock_stdout = io.StringIO()
        with patch("sys.stdout", mock_stdout):
            self.assertIs(notifier.stream, mock_stdout)
            alert = make_test_alert()
            notifier.send(alert)
            self.assertIn("TICKETS AVAILABLE", mock_stdout.getvalue())

    def test_console_notifier_reminder_prefix(self):
        """Alert with is_reminder=True prints [REMINDER] in banner; regular alert prints [ALERT]."""
        stream_reminder = io.StringIO()
        stream_alert = io.StringIO()

        ConsoleNotifier(stream=stream_reminder).send(make_test_alert(is_reminder=True))
        ConsoleNotifier(stream=stream_alert).send(make_test_alert(is_reminder=False))

        self.assertIn("[REMINDER] TICKETS AVAILABLE", stream_reminder.getvalue())
        self.assertIn("[ALERT] TICKETS AVAILABLE", stream_alert.getvalue())


# ==============================================================================
# Category 6: Composite Notifier Channel Isolation & Delivery Reporting
# ==============================================================================

class FlakyMockNotifier(BaseNotifier):
    """Deterministic mock notifier for composite stress tests."""

    def __init__(self, name: str, behavior: str = "success"):
        self._name = name
        self.behavior = behavior
        self.calls = 0

    @property
    def channel_name(self) -> str:
        return self._name

    def send(self, alert: TicketAlert) -> bool:
        self.calls += 1
        if self.behavior == "success":
            return True
        elif self.behavior == "fail":
            return False
        elif self.behavior == "crash_runtime":
            raise RuntimeError(f"Channel '{self._name}' crashed with unhandled network exception!")
        elif self.behavior == "crash_value":
            raise ValueError(f"Channel '{self._name}' crashed with bad payload parameter!")
        elif self.behavior == "crash_zerodiv":
            raise ZeroDivisionError(f"Channel '{self._name}' division by zero!")
        return False


class TestCompositeNotifierIsolationAndReportingAdversarial(unittest.TestCase):
    """Stress tests on CompositeNotifier exception containment and last_delivery_report mapping."""

    def test_composite_multi_channel_exception_isolation_and_reporting(self):
        """
        Verify that 5 notifiers with mixed behaviors (success, failure, different crash types)
        run to completion, isolate exceptions, and populate last_delivery_report accurately.
        """
        n1 = FlakyMockNotifier("ch_telegram", behavior="success")
        n2 = FlakyMockNotifier("ch_discord", behavior="crash_runtime")
        n3 = FlakyMockNotifier("ch_email", behavior="fail")
        n4 = FlakyMockNotifier("ch_sms", behavior="crash_value")
        n5 = FlakyMockNotifier("ch_console", behavior="success")

        composite = CompositeNotifier([n1, n2, n3, n4, n5])
        alert = make_test_alert()

        # composite.send() must return True (because ch_telegram and ch_console succeeded)
        result = composite.send(alert)
        self.assertTrue(result)

        # All 5 notifiers must have been invoked exactly once
        self.assertEqual(n1.calls, 1)
        self.assertEqual(n2.calls, 1)
        self.assertEqual(n3.calls, 1)
        self.assertEqual(n4.calls, 1)
        self.assertEqual(n5.calls, 1)

        # last_delivery_report must accurately record boolean delivery outcomes
        expected_report = {
            "ch_telegram": True,
            "ch_discord": False,
            "ch_email": False,
            "ch_sms": False,
            "ch_console": True,
        }
        self.assertEqual(composite.last_delivery_report, expected_report)

    def test_composite_all_channels_crash_returns_false_safely(self):
        """When all channels raise unhandled exceptions, composite.send() returns False and logs error."""
        n1 = FlakyMockNotifier("ch1", behavior="crash_runtime")
        n2 = FlakyMockNotifier("ch2", behavior="crash_value")
        n3 = FlakyMockNotifier("ch3", behavior="crash_zerodiv")

        composite = CompositeNotifier([n1, n2, n3])
        alert = make_test_alert()

        result = composite.send(alert)
        self.assertFalse(result)
        self.assertEqual(composite.last_delivery_report, {"ch1": False, "ch2": False, "ch3": False})

    def test_composite_empty_notifiers_list(self):
        """Composite with no registered notifiers returns False and empty report."""
        composite = CompositeNotifier([])
        alert = make_test_alert()

        result = composite.send(alert)
        self.assertFalse(result)
        self.assertEqual(composite.last_delivery_report, {})

    def test_composite_add_notifier_and_length(self):
        """Verify dynamic registration via add_notifier() and len()."""
        composite = CompositeNotifier()
        self.assertEqual(len(composite), 0)

        n1 = FlakyMockNotifier("ch1")
        composite.add_notifier(n1)
        self.assertEqual(len(composite), 1)

        # Adding same notifier instance again is ignored
        composite.add_notifier(n1)
        self.assertEqual(len(composite), 1)


# ==============================================================================
# Category 7: Channel HTML/Embed XSS & Payload Safety
# ==============================================================================

class TestChannelPayloadSafetyAdversarial(unittest.TestCase):
    """Stress tests on HTML escaping and payload safety for Telegram and Discord."""

    @patch("requests.post")
    def test_telegram_xss_injection_escaping(self, mock_post):
        """
        Verify malicious HTML injection strings in event name and section name
        are XML-entity escaped so Telegram Bot API parses message cleanly.
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "result": {}}
        mock_post.return_value = mock_resp

        notifier = TelegramNotifier(bot_token="123456:ABC-DEF", chat_id="987654321")
        alert = make_test_alert(
            event_name='Foo & <script>alert("xss")</script> > "Rock"',
            section_name='Sívori <tag> & "Media"',
        )

        success = notifier.send(alert)
        self.assertTrue(success)

        called_payload = mock_post.call_args[1]["json"]
        text = called_payload["text"]

        # Raw HTML tags must NOT be present
        self.assertNotIn("<script>", text)
        self.assertNotIn("<tag>", text)
        # XML entities MUST be present
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;tag&gt;", text)
        self.assertIn("&amp;", text)

    @patch("requests.post")
    def test_telegram_429_retry_after_handling(self, mock_post):
        """Telegram HTTP 429 response with retry_after parameter returns False cleanly."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {"ok": False, "parameters": {"retry_after": 45}}
        mock_post.return_value = mock_resp

        notifier = TelegramNotifier(bot_token="token", chat_id="123")
        success = notifier.send(make_test_alert())
        self.assertFalse(success)

    @patch("requests.post")
    def test_discord_429_retry_after_handling(self, mock_post):
        """Discord HTTP 429 response with retry_after in body or headers returns False cleanly."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {"retry_after": 2.5, "message": "You are being rate limited."}
        mock_resp.headers = {"Retry-After": "3"}
        mock_post.return_value = mock_resp

        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/xyz")
        success = notifier.send(make_test_alert())
        self.assertFalse(success)


if __name__ == "__main__":
    unittest.main()
