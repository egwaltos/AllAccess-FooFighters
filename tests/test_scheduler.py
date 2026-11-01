"""
Unit tests for PollingScheduler and GracefulShutdown (Features F12 & F13).
Verifies scheduler loop execution, responsive event sleep, signal handling,
exponential backoff, jitter distribution, Retry-After parsing, error resilience,
and metrics tracking.
"""
import random
import signal
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from allaccess_monitor.checker.base import BaseTicketChecker
from allaccess_monitor.checker.mock import MockTicketChecker
from allaccess_monitor.models import AvailabilityStatus, CheckResult, SectionInfo, TicketAlert
from allaccess_monitor.notifier.base import BaseNotifier
from allaccess_monitor.scheduler import GracefulShutdown, PollingScheduler
from allaccess_monitor.state import AlertStateManager


class TestGracefulShutdown(unittest.TestCase):
    """Unit tests for GracefulShutdown lifecycle and signal trapping."""

    def test_initial_state_not_stopped(self):
        """Newly created GracefulShutdown has is_stopped=False."""
        shutdown = GracefulShutdown(register_signals=False)
        self.assertFalse(shutdown.is_stopped)

    def test_stop_action_sets_flag(self):
        """Calling stop() immediately sets is_stopped=True."""
        shutdown = GracefulShutdown(register_signals=False)
        shutdown.stop()
        self.assertTrue(shutdown.is_stopped)

    def test_wait_timeout_expires(self):
        """wait() returns False when timeout elapses without stop."""
        shutdown = GracefulShutdown(register_signals=False)
        start = time.time()
        stopped = shutdown.wait(timeout=0.03)
        self.assertFalse(stopped)
        self.assertLess(time.time() - start, 0.5)

    def test_wait_interrupted_by_stop(self):
        """wait() awakens immediately (<0.3s) when stop() is called from another thread."""
        shutdown = GracefulShutdown(register_signals=False)
        start = time.time()
        timer = threading.Timer(0.04, shutdown.stop)
        timer.start()
        try:
            stopped = shutdown.wait(timeout=5.0)
            elapsed = time.time() - start
            self.assertTrue(stopped)
            self.assertLess(elapsed, 0.5)
        finally:
            timer.cancel()

    def test_wait_zero_or_negative_timeout(self):
        """wait(0) or wait(-1) returns is_stopped immediately."""
        shutdown = GracefulShutdown(register_signals=False)
        self.assertFalse(shutdown.wait(0))
        self.assertFalse(shutdown.wait(-5.0))
        shutdown.stop()
        self.assertTrue(shutdown.wait(0))

    def test_signal_handler_first_call_stops(self):
        """Simulating SIGINT sets stop event."""
        shutdown = GracefulShutdown(register_signals=False)
        shutdown._signal_handler(signal.SIGINT, None)
        self.assertTrue(shutdown.is_stopped)
        self.assertEqual(shutdown._signal_count, 1)

    def test_signal_handler_sigterm(self):
        """Simulating SIGTERM sets stop event."""
        shutdown = GracefulShutdown(register_signals=False)
        sigterm = getattr(signal, "SIGTERM", signal.SIGINT)
        shutdown._signal_handler(sigterm, None)
        self.assertTrue(shutdown.is_stopped)

    def test_signal_handler_second_call_raises_system_exit(self):
        """Second consecutive signal forces emergency SystemExit(1)."""
        shutdown = GracefulShutdown(register_signals=False)
        shutdown._signal_handler(signal.SIGINT, None)
        self.assertTrue(shutdown.is_stopped)
        with self.assertRaises(SystemExit) as ctx:
            shutdown._signal_handler(signal.SIGINT, None)
        self.assertEqual(ctx.exception.code, 1)

    def test_signal_registration_safe_in_child_thread(self):
        """Registering signals in a worker thread does not raise an unhandled exception."""
        error_holder = []

        def worker():
            try:
                GracefulShutdown(register_signals=True)
            except Exception as e:
                error_holder.append(e)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=2.0)
        self.assertEqual(len(error_holder), 0)


class TestPollingSchedulerLifecycle(unittest.TestCase):
    """Unit tests for PollingScheduler tick execution and lifecycle."""

    def test_scheduler_initialization_defaults(self):
        """Scheduler initializes with sensible defaults."""
        checker = MockTicketChecker(status=AvailabilityStatus.UNAVAILABLE)
        scheduler = PollingScheduler(checker=checker)
        self.assertEqual(scheduler.interval_seconds, 30.0)
        self.assertEqual(scheduler.max_backoff, 300.0)
        self.assertEqual(scheduler.section_name, "Sivori media")
        self.assertEqual(scheduler.consecutive_errors, 0)
        self.assertFalse(scheduler.is_stopped)

    def test_scheduler_kwargs_aliases(self):
        """Scheduler recognizes kwarg aliases like poll_interval and target_section."""
        checker = MockTicketChecker(status=AvailabilityStatus.UNAVAILABLE)
        scheduler = PollingScheduler(
            checker=checker,
            poll_interval=15.0,
            target_section="Campo Delantero",
        )
        self.assertEqual(scheduler.interval_seconds, 15.0)
        self.assertEqual(scheduler.section_name, "Campo Delantero")

    def test_run_single_tick_available_triggers_alert(self):
        """Tick with available tickets evaluates in state manager and dispatches alert."""
        checker = MockTicketChecker(
            status=AvailabilityStatus.AVAILABLE,
            section_name="Sivori media",
            price=125000.0,
        )
        state_mgr = AlertStateManager()
        notifier = MagicMock(spec=BaseNotifier)
        notifier.send.return_value = True

        scheduler = PollingScheduler(
            checker=checker,
            state_manager=state_mgr,
            notifier=notifier,
            interval_seconds=10.0,
        )
        res = scheduler.run_single_tick()

        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)
        notifier.send.assert_called_once()
        sent_alert = notifier.send.call_args[0][0]
        self.assertIsInstance(sent_alert, TicketAlert)
        self.assertEqual(sent_alert.section_name, "Sivori media")
        self.assertEqual(scheduler.metrics["alerts_dispatched"], 1)

    def test_run_single_tick_unavailable_no_alert(self):
        """Tick with sold out tickets does not dispatch any alert."""
        checker = MockTicketChecker(status=AvailabilityStatus.UNAVAILABLE)
        state_mgr = AlertStateManager()
        notifier = MagicMock(spec=BaseNotifier)

        scheduler = PollingScheduler(
            checker=checker,
            state_manager=state_mgr,
            notifier=notifier,
            interval_seconds=10.0,
        )
        res = scheduler.run_single_tick()

        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)
        notifier.send.assert_not_called()
        self.assertEqual(scheduler.metrics["alerts_dispatched"], 0)

    def test_run_single_tick_cooldown_throttling(self):
        """Second available tick within cooldown does not send duplicate alert."""
        checker = MockTicketChecker(status=AvailabilityStatus.AVAILABLE)
        state_mgr = AlertStateManager(cooldown_seconds=1800)
        notifier = MagicMock(spec=BaseNotifier)
        notifier.send.return_value = True

        scheduler = PollingScheduler(
            checker=checker,
            state_manager=state_mgr,
            notifier=notifier,
            interval_seconds=10.0,
        )

        scheduler.run_single_tick()
        self.assertEqual(notifier.send.call_count, 1)

        scheduler.run_single_tick()
        self.assertEqual(notifier.send.call_count, 1)
        self.assertEqual(scheduler.metrics["alerts_dispatched"], 1)
        self.assertEqual(scheduler.metrics["total_checks"], 2)

    def test_run_single_tick_checker_exception_recovery(self):
        """Uncaught exception in checker does not crash tick; increments error streak."""
        checker = MagicMock(spec=BaseTicketChecker)
        checker.check_availability.side_effect = RuntimeError("Socket dropped")

        scheduler = PollingScheduler(
            checker=checker,
            state_manager=AlertStateManager(),
            notifier=MagicMock(),
            interval_seconds=10.0,
        )

        res = scheduler.run_single_tick()
        self.assertEqual(res.status, AvailabilityStatus.ERROR)
        self.assertIn("Socket dropped", res.error_message)
        self.assertEqual(scheduler.consecutive_errors, 1)

    def test_run_max_ticks_clean_termination(self):
        """scheduler.run(max_ticks=4) executes exactly 4 ticks and returns 4."""
        checker = MockTicketChecker(status=AvailabilityStatus.UNAVAILABLE)
        scheduler = PollingScheduler(
            checker=checker,
            state_manager=AlertStateManager(),
            notifier=MagicMock(),
            interval_seconds=0.01,
        )
        ticks = scheduler.run(max_ticks=4)
        self.assertEqual(ticks, 4)
        self.assertEqual(scheduler.metrics["total_checks"], 4)

    def test_run_terminated_by_shutdown_event(self):
        """Setting shutdown event terminates loop early."""
        checker = MockTicketChecker(status=AvailabilityStatus.UNAVAILABLE)
        scheduler = PollingScheduler(
            checker=checker,
            state_manager=AlertStateManager(),
            notifier=MagicMock(),
            interval_seconds=10.0,
        )
        # Stop after 1 tick
        with patch.object(scheduler._shutdown, "wait", return_value=True):
            ticks = scheduler.run(max_ticks=10)
            self.assertEqual(ticks, 1)

    def test_custom_section_passed_to_checker(self):
        """Custom section name is forwarded to checker.check_availability."""
        checker = MagicMock(spec=BaseTicketChecker)
        checker.check_availability.return_value = CheckResult(
            status=AvailabilityStatus.UNAVAILABLE,
            section_name="Campo Trasero",
        )
        scheduler = PollingScheduler(
            checker=checker,
            section_name="Campo Trasero",
            interval_seconds=1.0,
        )
        scheduler.run_single_tick()
        checker.check_availability.assert_called_once_with("Campo Trasero")

    def test_context_manager_and_close(self):
        """Context manager cleans up scheduler on exit."""
        checker = MagicMock()
        with PollingScheduler(checker=checker, interval_seconds=1.0) as s:
            self.assertFalse(s.is_stopped)
        self.assertTrue(s.is_stopped)
        checker.close.assert_called_once()


class TestExponentialBackoffAndJitter(unittest.TestCase):
    """Unit tests for Feature F13: Exponential Backoff, Jitter, and Retry-After."""

    def test_exponential_delay_growth_without_jitter(self):
        """compute_backoff_delay without jitter scales: interval * 2^(errors - 1)."""
        scheduler = PollingScheduler(interval_seconds=10.0, max_backoff=300.0)
        d1 = scheduler.compute_backoff_delay(consecutive_errors=1, apply_jitter=False)
        d2 = scheduler.compute_backoff_delay(consecutive_errors=2, apply_jitter=False)
        d3 = scheduler.compute_backoff_delay(consecutive_errors=3, apply_jitter=False)
        d4 = scheduler.compute_backoff_delay(consecutive_errors=4, apply_jitter=False)

        self.assertEqual(d1, 10)
        self.assertEqual(d2, 20)
        self.assertEqual(d3, 40)
        self.assertEqual(d4, 80)

    def test_delay_capped_at_max_backoff(self):
        """Backoff delay never exceeds max_backoff ceiling."""
        scheduler = PollingScheduler(interval_seconds=10.0, max_backoff=60.0)
        d = scheduler.compute_backoff_delay(consecutive_errors=10, apply_jitter=False)
        self.assertEqual(d, 60)

    def test_large_consecutive_errors_no_overflow(self):
        """100 consecutive errors does not cause integer overflow."""
        scheduler = PollingScheduler(interval_seconds=10.0, max_backoff=900.0)
        d = scheduler.compute_backoff_delay(consecutive_errors=100, apply_jitter=False)
        self.assertEqual(d, 900)

    def test_jitter_produces_variance(self):
        """Applying jitter generates multiple distinct delay samples."""
        scheduler = PollingScheduler(interval_seconds=10.0, max_backoff=300.0)
        samples = {scheduler.compute_backoff_delay(consecutive_errors=3, apply_jitter=True) for _ in range(25)}
        self.assertGreater(len(samples), 1)

    def test_jitter_lower_bound(self):
        """Jittered backoff is always >= 50% of the nominal delay."""
        scheduler = PollingScheduler(interval_seconds=20.0, max_backoff=300.0)
        # Nominal delay for errors=1 is 20.0; lower bound with uniform(0.5, 1.0) is 10.0
        for _ in range(50):
            d = scheduler.compute_backoff_delay(consecutive_errors=1, apply_jitter=True)
            self.assertGreaterEqual(d, 10.0)
            self.assertLessEqual(d, 20.0)

    def test_retry_after_integer_parsed(self):
        """CheckResult with HTTP 429 and Retry-After: 45 yields delay 45."""
        scheduler = PollingScheduler(interval_seconds=10.0, max_backoff=300.0)
        result = CheckResult(
            status=AvailabilityStatus.ERROR,
            section_name="Sivori",
            status_code=429,
            error_message="AllAccess returned HTTP 429 Rate Limit. Retry-After: 45",
        )
        delay = scheduler.compute_delay_from_result(result)
        self.assertEqual(delay, 45)

    def test_retry_after_capped_at_max_backoff(self):
        """Excessive Retry-After: 99999 is clamped to max_backoff."""
        scheduler = PollingScheduler(interval_seconds=10.0, max_backoff=300.0)
        result = CheckResult(
            status=AvailabilityStatus.ERROR,
            section_name="Sivori",
            status_code=429,
            error_message="Retry-After: 99999",
        )
        delay = scheduler.compute_delay_from_result(result)
        self.assertEqual(delay, 300)

    def test_retry_after_invalid_fallback(self):
        """Non-numeric Retry-After falls back to exponential backoff."""
        scheduler = PollingScheduler(interval_seconds=10.0, max_backoff=300.0)
        result = CheckResult(
            status=AvailabilityStatus.ERROR,
            section_name="Sivori",
            status_code=429,
            error_message="Retry-After: not_a_number_or_date",
        )
        delay = scheduler.compute_delay_from_result(result)
        self.assertGreaterEqual(delay, 5.0)

    def test_http_503_triggers_backoff(self):
        """HTTP 503 Service Unavailable triggers backoff delay."""
        scheduler = PollingScheduler(interval_seconds=10.0, max_backoff=300.0)
        result = CheckResult(
            status=AvailabilityStatus.ERROR,
            section_name="Sivori",
            status_code=503,
            error_message="AllAccess server error HTTP 503",
        )
        delay = scheduler.compute_delay_from_result(result)
        self.assertGreater(delay, 0)

    def test_connection_timeout_triggers_backoff(self):
        """Connection timed out error triggers backoff delay."""
        scheduler = PollingScheduler(interval_seconds=10.0, max_backoff=300.0)
        result = CheckResult(
            status=AvailabilityStatus.ERROR,
            section_name="Sivori",
            error_message="Connection timed out",
        )
        delay = scheduler.compute_delay_from_result(result)
        self.assertGreater(delay, 0)


class TestSchedulerMetrics(unittest.TestCase):
    """Unit tests for scheduler telemetry metrics tracking."""

    def test_metrics_keys_present(self):
        """Scheduler metrics exposes standard telemetry keys."""
        scheduler = PollingScheduler()
        m = scheduler.metrics
        self.assertIn("total_checks", m)
        self.assertIn("alerts_dispatched", m)
        self.assertIn("successful_checks", m)
        self.assertIn("failed_checks", m)
        self.assertIn("consecutive_errors", m)
        self.assertEqual(m["total_checks"], 0)
        self.assertEqual(m["alerts_dispatched"], 0)

    def test_metrics_copy_immutability(self):
        """Mutating returned metrics dictionary does not corrupt internal state."""
        scheduler = PollingScheduler()
        m = scheduler.metrics
        m["total_checks"] = 9999
        self.assertEqual(scheduler.metrics["total_checks"], 0)

    def test_metrics_counter_accuracy(self):
        """Metrics accurately count successful vs failed checks and alerts."""
        sequence = ["sold_out", "available", "sold_out"]
        checker = MockTicketChecker(sequence=sequence)
        notifier = MagicMock()
        notifier.send.return_value = True

        scheduler = PollingScheduler(
            checker=checker,
            state_manager=AlertStateManager(cooldown_seconds=1800),
            notifier=notifier,
            interval_seconds=0.01,
        )
        scheduler.run(max_ticks=3)

        m = scheduler.metrics
        self.assertEqual(m["total_checks"], 3)
        self.assertEqual(m["alerts_dispatched"], 1)
        self.assertEqual(m["successful_checks"], 3)
        self.assertEqual(m["failed_checks"], 0)
        self.assertEqual(m["consecutive_errors"], 0)


if __name__ == "__main__":
    unittest.main()
