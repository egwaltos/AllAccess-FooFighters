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
from typing import List, Optional, TextIO, Tuple
import unittest
from unittest.mock import MagicMock, patch

from allaccess_monitor.models import (
    AvailabilityStatus,
    CheckResult,
    MonitoredTarget,
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


# ==============================================================================
# Category 8: Milestone 2 Targeted Adversarial Challenges
# ==============================================================================

class TestFlappingStormDampeningAdversarial(unittest.TestCase):
    """
    Adversarial challenge: Flapping Storm Defense.
    Rapidly alternate target availability between AVAILABLE and UNAVAILABLE 50 times
    in simulated 5-second intervals. Verify no alert spam occurs due to flapping dampening.
    """

    def test_flapping_storm_50_rapid_oscillations_suppresses_alert_spam(self):
        """
        Simulate 50 rapid state oscillations at 5-second intervals.
        flapping_cooldown_seconds = 30.0.
        t=0 (AVAILABLE): Initial alert #1 (should_alert=True).
        t=5 (UNAVAILABLE): State transitions to unavailable, last_unavailable_time=5.
        t=10 (AVAILABLE): Flapping detected (10-5 = 5s < 30s) -> should_alert=False (dampened).
        t=15 (UNAVAILABLE): last_unavailable_time=15.
        ... alternating 50 times (25 available, 25 unavailable).
        Expected: exactly 1 alert sent across the entire storm, 0 reminder alerts, 24 dampened.
        """
        manager = AlertStateManager(
            cooldown_seconds=1800,
            flapping_cooldown_seconds=30.0,
        )
        target = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/flapping-fest",
            target_section="Platea VIP",
        )

        alerts_triggered = []
        dampened_checks = []

        for step in range(50):
            current_time = float(step * 5)  # 0, 5, 10, 15, ..., 245
            is_avail = (step % 2 == 0)

            if is_avail:
                result = CheckResult(
                    status=AvailabilityStatus.AVAILABLE,
                    event_url=target.event_url,
                    section_name=target.target_section,
                    section=SectionInfo(
                        name=target.target_section,
                        available=True,
                        price=50000.0,
                    ),
                )
            else:
                result = CheckResult(
                    status=AvailabilityStatus.UNAVAILABLE,
                    event_url=target.event_url,
                    section_name=target.target_section,
                )

            decision = manager.evaluate(result, current_time=current_time, target=target)

            if decision.should_alert:
                alerts_triggered.append((step, current_time, decision))
            elif is_avail and "flapping" in decision.reason.lower():
                dampened_checks.append((step, current_time, decision))

        # Verification: Exactly 1 alert was triggered (the initial discovery at step 0)
        self.assertEqual(len(alerts_triggered), 1, f"Expected exactly 1 alert, got {len(alerts_triggered)}")
        self.assertEqual(alerts_triggered[0][0], 0, "Initial alert must occur at step 0")
        self.assertFalse(alerts_triggered[0][2].is_reminder)

        # Verification: All subsequent 24 available checks were dampened
        self.assertEqual(len(dampened_checks), 24, f"Expected 24 dampened checks, got {len(dampened_checks)}")
        self.assertEqual(manager.get_target_state(target).total_alerts_sent, 1)

    def test_flapping_recovery_after_stabilization(self):
        """
        After rapid flapping stops and tickets remain UNAVAILABLE beyond flapping_cooldown_seconds,
        a genuine return to AVAILABLE must successfully trigger an alert.
        """
        manager = AlertStateManager(
            cooldown_seconds=1800,
            flapping_cooldown_seconds=20.0,
        )
        target = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/recovery-test",
            target_section="Campo",
        )

        res_avail = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            event_url=target.event_url,
            section_name=target.target_section,
        )
        res_unavail = CheckResult(
            status=AvailabilityStatus.UNAVAILABLE,
            event_url=target.event_url,
            section_name=target.target_section,
        )

        # 1. Initial edge
        d0 = manager.evaluate(res_avail, current_time=100.0, target=target)
        self.assertTrue(d0.should_alert)

        # 2. Flapping: drops at 105, returns at 110 (5s < 20s) -> Dampened!
        manager.evaluate(res_unavail, current_time=105.0, target=target)
        d_flap = manager.evaluate(res_avail, current_time=110.0, target=target)
        self.assertFalse(d_flap.should_alert)

        # 3. Drops at 115, and remains unavailable until t=200 (elapsed = 85s >= 20s flapping cooldown)
        manager.evaluate(res_unavail, current_time=115.0, target=target)

        # 4. Returns at t=200 -> Genuine recovery! Must alert!
        d_rec = manager.evaluate(res_avail, current_time=200.0, target=target)
        self.assertTrue(d_rec.should_alert, "Return after stable unavailable window must alert")
        self.assertFalse(d_rec.is_reminder)
        self.assertEqual(manager.get_target_state(target).total_alerts_sent, 2)


class TestPriceDropContinuousAvailabilityAdversarial(unittest.TestCase):
    """
    Adversarial challenge: Price Drop on Continuous Availability.
    Simulate tickets available at $50,000, then dropping to $40,000, then $30,000.
    Verify an immediate alert is generated on price drop even within normal 1800s cooldown.
    """

    def test_price_drop_triggers_immediate_alerts_within_cooldown(self):
        """
        Verify cascading price drops trigger immediate edge alerts within cooldown:
        t=100: $50,000 -> Alert #1 (initial edge)
        t=150: $40,000 (elapsed 50s < 1800s) -> Alert #2 (price drop)
        t=200: $30,000 (elapsed 50s < 1800s) -> Alert #3 (price drop)
        """
        manager = AlertStateManager(cooldown_seconds=1800)
        target = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/price-drop-event",
            target_section="Platea Preferencial",
        )

        def make_price_result(price: float) -> CheckResult:
            return CheckResult(
                status=AvailabilityStatus.AVAILABLE,
                event_url=target.event_url,
                section_name=target.target_section,
                section=SectionInfo(
                    name=target.target_section,
                    available=True,
                    price=price,
                ),
            )

        # Step 1: Initial alert at $50,000
        d1 = manager.evaluate(make_price_result(50000.0), current_time=100.0, target=target)
        self.assertTrue(d1.should_alert)
        self.assertFalse(d1.is_reminder)
        self.assertEqual(manager.get_target_state(target).last_observed_price, 50000.0)
        self.assertEqual(manager.get_target_state(target).last_alert_time, 100.0)

        # Step 2: 50 seconds later (well within 1800s cooldown), price drops to $40,000
        d2 = manager.evaluate(make_price_result(40000.0), current_time=150.0, target=target)
        self.assertTrue(d2.should_alert, "Price drop from 50k to 40k must trigger immediate alert")
        self.assertFalse(d2.is_reminder, "Price drop alerts must not be marked as passive reminders")
        self.assertIn("Price drop detected from 50000.00 to 40000.00", d2.reason)
        self.assertEqual(manager.get_target_state(target).last_observed_price, 40000.0)
        self.assertEqual(manager.get_target_state(target).last_alert_time, 150.0)

        # Step 3: Another 50 seconds later, price drops to $30,000
        d3 = manager.evaluate(make_price_result(30000.0), current_time=200.0, target=target)
        self.assertTrue(d3.should_alert, "Price drop from 40k to 30k must trigger immediate alert")
        self.assertFalse(d3.is_reminder)
        self.assertIn("Price drop detected from 40000.00 to 30000.00", d3.reason)
        self.assertEqual(manager.get_target_state(target).last_observed_price, 30000.0)
        self.assertEqual(manager.get_target_state(target).last_alert_time, 200.0)

        self.assertEqual(manager.get_target_state(target).total_alerts_sent, 3)

    def test_price_drop_with_total_price_field(self):
        """Verify price drop detection works when price is conveyed via section_info.total_price."""
        manager = AlertStateManager(cooldown_seconds=1800)
        target = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/total-price-event",
            target_section="Campo",
        )

        res1 = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            event_url=target.event_url,
            section_name=target.target_section,
            section=SectionInfo(
                name="Campo",
                available=True,
                price=50000.0,
                service_charge=7500.0,  # total = 57500.0
            ),
        )
        d1 = manager.evaluate(res1, current_time=1000.0, target=target)
        self.assertTrue(d1.should_alert)
        self.assertEqual(manager.get_target_state(target).last_observed_price, 57500.0)

        # Total price drops to 46000.0 (40000 + 6000)
        res2 = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            event_url=target.event_url,
            section_name=target.target_section,
            section=SectionInfo(
                name="Campo",
                available=True,
                price=40000.0,
                service_charge=6000.0,  # total = 46000.0
            ),
        )
        d2 = manager.evaluate(res2, current_time=1050.0, target=target)
        self.assertTrue(d2.should_alert)
        self.assertIn("Price drop detected", d2.reason)
        self.assertEqual(manager.get_target_state(target).last_observed_price, 46000.0)


class TestPriceIncreaseAndIdenticalSuppressionAdversarial(unittest.TestCase):
    """
    Adversarial challenge: Price Increase or Identical Price within Cooldown.
    Simulate tickets available at $50,000, then rising to $60,000 or staying at $50,000 within cooldown.
    Verify alerts are strictly suppressed.
    """

    def test_price_increase_and_identical_strictly_suppressed(self):
        """
        Verify:
        t=100: $50,000 -> Alert #1 (initial edge)
        t=120: $50,000 -> Throttled (identical price within cooldown)
        t=150: $60,000 -> Throttled (price increase within cooldown)
        t=180: $60,000 -> Throttled (identical higher price within cooldown)
        t=210: $75,000 -> Throttled (further price increase within cooldown)
        t=250: $65,000 -> Alert #2 (price drop from $75,000 to $65,000)
        """
        manager = AlertStateManager(cooldown_seconds=1800)
        target = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/surge-event",
            target_section="Platea Alta",
        )

        def make_res(p: float) -> CheckResult:
            return CheckResult(
                status=AvailabilityStatus.AVAILABLE,
                event_url=target.event_url,
                section_name=target.target_section,
                section=SectionInfo(name=target.target_section, available=True, price=p),
            )

        # 1. Initial detection at $50,000 -> Alert #1
        d1 = manager.evaluate(make_res(50000.0), current_time=100.0, target=target)
        self.assertTrue(d1.should_alert)

        # 2. Identical price at $50,000 20s later -> Suppressed
        d2 = manager.evaluate(make_res(50000.0), current_time=120.0, target=target)
        self.assertFalse(d2.should_alert, "Identical price within cooldown must be throttled")
        self.assertIn("throttled", d2.reason.lower())

        # 3. Price surge to $60,000 50s later -> Suppressed
        d3 = manager.evaluate(make_res(60000.0), current_time=150.0, target=target)
        self.assertFalse(d3.should_alert, "Price increase within cooldown must NOT trigger an alert")
        self.assertIn("throttled", d3.reason.lower())
        self.assertEqual(manager.get_target_state(target).last_observed_price, 60000.0)

        # 4. Identical price at $60,000 -> Suppressed
        d4 = manager.evaluate(make_res(60000.0), current_time=180.0, target=target)
        self.assertFalse(d4.should_alert)

        # 5. Further price surge to $75,000 -> Suppressed
        d5 = manager.evaluate(make_res(75000.0), current_time=210.0, target=target)
        self.assertFalse(d5.should_alert, "Further price increase must be suppressed")
        self.assertEqual(manager.get_target_state(target).last_observed_price, 75000.0)

        # 6. Price drops from $75,000 to $65,000 -> Alert #2!
        d6 = manager.evaluate(make_res(65000.0), current_time=250.0, target=target)
        self.assertTrue(d6.should_alert, "Price drop from 75k to 65k must trigger alert")
        self.assertIn("Price drop detected from 75000.00 to 65000.00", d6.reason)

        # Total alerts across all steps must be exactly 2
        self.assertEqual(manager.get_target_state(target).total_alerts_sent, 2)


class TestMultiTargetConcurrencyAndIsolationAdversarial(unittest.TestCase):
    """
    Adversarial challenge: Multi-Target Concurrency & Isolation.
    Simulate 20 distinct targets checking concurrently.
    Verify target A's cooldown never resets or blocks target B's alert.
    """

    def test_target_cooldown_cross_talk_immunity(self):
        """
        Verify target A's alert and cooldown never blocks or resets target B's alert.
        Target A triggers alert at t=100.
        Target B triggers alert at t=105 (within Target A's cooldown).
        Target A checks again at t=110 (throttled).
        Target A sells out at t=120 (Target A resets; Target B remains available and in cooldown).
        Target A returns at t=130 (Target A alerts; Target B throttled).
        """
        manager = AlertStateManager(cooldown_seconds=1800)

        target_a = MonitoredTarget(event_url="https://www.allaccess.com.ar/event/event-alpha", target_section="Section A")
        target_b = MonitoredTarget(event_url="https://www.allaccess.com.ar/event/event-beta", target_section="Section B")

        res_a_avail = CheckResult(status=AvailabilityStatus.AVAILABLE, event_url=target_a.event_url, section_name=target_a.target_section)
        res_a_unavail = CheckResult(status=AvailabilityStatus.UNAVAILABLE, event_url=target_a.event_url, section_name=target_a.target_section)
        res_b_avail = CheckResult(status=AvailabilityStatus.AVAILABLE, event_url=target_b.event_url, section_name=target_b.target_section)

        # t=100: Target A alerts
        da1 = manager.evaluate(res_a_avail, current_time=100.0, target=target_a)
        self.assertTrue(da1.should_alert)

        # t=105: Target B alerts immediately (not blocked by A's cooldown!)
        db1 = manager.evaluate(res_b_avail, current_time=105.0, target=target_b)
        self.assertTrue(db1.should_alert, "Target B must alert despite Target A being in active cooldown")

        # t=110: Target A checked again -> throttled
        da2 = manager.evaluate(res_a_avail, current_time=110.0, target=target_a)
        self.assertFalse(da2.should_alert)

        # t=120: Target A sells out -> Target A reset
        manager.evaluate(res_a_unavail, current_time=120.0, target=target_a)
        self.assertFalse(manager.is_target_available(target_a))
        self.assertTrue(manager.is_target_available(target_b), "Target A reset must NOT affect Target B availability")

        # t=125: Target B checked -> still throttled under Target B's own cooldown
        db2 = manager.evaluate(res_b_avail, current_time=125.0, target=target_b)
        self.assertFalse(db2.should_alert, "Target B must remain throttled under its own cooldown")

        # t=130: Target A reappears -> Target A alerts immediately!
        da3 = manager.evaluate(res_a_avail, current_time=130.0, target=target_a)
        self.assertTrue(da3.should_alert, "Target A reappearance must alert independently")

        self.assertEqual(manager.get_target_state(target_a).total_alerts_sent, 2)
        self.assertEqual(manager.get_target_state(target_b).total_alerts_sent, 1)

    def test_20_targets_concurrent_execution_stress(self):
        """
        Simulate 20 distinct targets checking concurrently using a thread pool.
        Verify zero race conditions, zero state corruptions, and exact per-target alert tallies.
        """
        import concurrent.futures

        manager = AlertStateManager(cooldown_seconds=1800)
        targets = [
            MonitoredTarget(
                event_url=f"https://www.allaccess.com.ar/event/event-{i // 4}",
                target_section=f"Section-{i % 4}",
            )
            for i in range(20)
        ]

        def check_target(index: int, t_val: float) -> Tuple[int, AlertDecision]:
            tgt = targets[index]
            res = CheckResult(
                status=AvailabilityStatus.AVAILABLE,
                event_url=tgt.event_url,
                section_name=tgt.target_section,
            )
            dec = manager.evaluate(res, current_time=t_val, target=tgt)
            return index, dec

        # Wave 1: All 20 targets check concurrently at t=100.0. All 20 must alert!
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(check_target, idx, 100.0) for idx in range(20)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        for idx, dec in results:
            self.assertTrue(dec.should_alert, f"Target {idx} should have alerted on initial check")

        # Wave 2: All 20 targets check concurrently at t=150.0 (within cooldown). All 20 must throttle!
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(check_target, idx, 150.0) for idx in range(20)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        for idx, dec in results:
            self.assertFalse(dec.should_alert, f"Target {idx} should have throttled under cooldown")

        # Every target must have recorded exactly 1 alert
        for tgt in targets:
            self.assertEqual(manager.get_target_state(tgt).total_alerts_sent, 1)
        self.assertEqual(manager.total_alerts_sent, 20)


class TestDualLayerAtomicStatePersistenceCrashAdversarial(unittest.TestCase):
    """
    Adversarial challenge: Dual-Layer Atomic State Persistence & Crash Resilience.
    Simulate abrupt power failure / process crash during state save, verify dual-layer
    atomic file writing (.tmp + atomic rename) prevents state loss.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "state.json"
        self.tmp_file = self.state_file.with_suffix(f"{self.state_file.suffix}.tmp")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_crash_during_tmp_write_preserves_original_state(self):
        """
        Simulate process crash while writing the temporary file (.tmp):
        1. Save good state with 3 targets.
        2. Simulate crash by writing truncated garbage to .tmp file.
        3. Load state in a new manager: verify original state is 100% preserved.
        """
        manager1 = AlertStateManager(cooldown_seconds=1800, state_file=self.state_file)
        target1 = MonitoredTarget(event_url="https://www.allaccess.com.ar/event/event-1", target_section="Sec-1")
        target2 = MonitoredTarget(event_url="https://www.allaccess.com.ar/event/event-2", target_section="Sec-2")
        
        manager1.evaluate(CheckResult(status=AvailabilityStatus.AVAILABLE, event_url=target1.event_url, section_name=target1.target_section), current_time=100.0, target=target1)
        manager1.evaluate(CheckResult(status=AvailabilityStatus.AVAILABLE, event_url=target2.event_url, section_name=target2.target_section), current_time=200.0, target=target2)

        self.assertTrue(self.state_file.exists())
        original_content = self.state_file.read_text(encoding="utf-8")
        self.assertIn("event-1", original_content)
        self.assertIn("event-2", original_content)

        # Simulate sudden power cut leaving half-written corrupted .tmp file
        self.tmp_file.write_text('{"version": 2, "is_currently_available": tr', encoding="utf-8")

        # New process starts up and loads state_file
        manager2 = AlertStateManager(cooldown_seconds=1800, state_file=self.state_file)
        self.assertTrue(manager2.is_target_available(target1))
        self.assertTrue(manager2.is_target_available(target2))
        self.assertEqual(manager2.get_last_alert_time(target1), 100.0)
        self.assertEqual(manager2.get_last_alert_time(target2), 200.0)

    def test_stale_corrupted_tmp_overwritten_atomically_on_subsequent_save(self):
        """
        Verify that an orphaned .tmp file left behind by a previous crash
        is cleanly overwritten and does not prevent successful atomic save.
        """
        manager = AlertStateManager(cooldown_seconds=1800, state_file=self.state_file)
        
        # Write binary trash into the .tmp file
        self.tmp_file.write_bytes(b"\xde\xad\xbe\xef\x00\x00\xff\xff")
        self.assertTrue(self.tmp_file.exists())

        # Manager executes check and saves state
        target = MonitoredTarget(event_url="https://www.allaccess.com.ar/event/fresh-event", target_section="Platea")
        manager.evaluate(
            CheckResult(status=AvailabilityStatus.AVAILABLE, event_url=target.event_url, section_name=target.target_section),
            current_time=500.0,
            target=target,
        )

        # The state_file must exist and be valid JSON
        self.assertTrue(self.state_file.exists())
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertIn("fresh-event", json.dumps(data))
        # The temporary file should have been renamed / replaced
        self.assertFalse(self.tmp_file.exists())

    def test_atomic_persistence_avoids_in_place_corruption_on_disk_error(self):
        """
        If writing to the temporary file fails (e.g. disk quota exceeded, OSError),
        the primary state_file must remain completely untouched.
        """
        manager = AlertStateManager(cooldown_seconds=1800, state_file=self.state_file)
        target = MonitoredTarget(event_url="https://www.allaccess.com.ar/event/base-event", target_section="General")
        manager.evaluate(
            CheckResult(status=AvailabilityStatus.AVAILABLE, event_url=target.event_url, section_name=target.target_section),
            current_time=1000.0,
            target=target,
        )

        initial_content = self.state_file.read_text(encoding="utf-8")

        # Simulate sudden disk write failure during .tmp creation
        with patch.object(Path, "write_text", side_effect=OSError("Disk write error: ENOSPC")):
            target2 = MonitoredTarget(event_url="https://www.allaccess.com.ar/event/new-event", target_section="VIP")
            manager.evaluate(
                CheckResult(status=AvailabilityStatus.AVAILABLE, event_url=target2.event_url, section_name=target2.target_section),
                current_time=1100.0,
                target=target2,
            )

        # Primary state_file must be completely unmodified and valid
        self.assertEqual(self.state_file.read_text(encoding="utf-8"), initial_content)


if __name__ == "__main__":
    unittest.main()

