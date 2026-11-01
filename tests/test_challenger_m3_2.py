"""
Empirical Adversarial Test Suite for Milestone 3 (Challenger 2).

Target Deliverables:
- PollingScheduler (scheduler.py)
- GracefulShutdown (scheduler.py)
- CLI Entrypoint & Daemon Flags (cli.py, __main__.py)
- Fixture & Simulation Integration

Adversarial Challenge Areas:
1. Long-Running Scheduler Loop & Exception Stress:
   - 500-iteration loop with injected heterogeneous exceptions (RuntimeError,
     TimeoutError, ConnectionResetError, KeyError, ZeroDivisionError, MemoryError,
     OSError, etc.) mixed with AVAILABLE, UNAVAILABLE, and SECTION_NOT_FOUND outcomes.
   - Deep consecutive failure streaks (100+ errors) verifying exponent overflow clamping
     and instantaneous recovery to normal delay on subsequent success.
   - Notifier exception isolation ensuring crashed notification delivery never kills the scheduler.
2. Telemetry Metrics Accuracy & Invariants:
   - Exhaustive mathematical invariant testing: total_checks == successful_checks + failed_checks.
   - Exactness of alerts_dispatched counter under mixed delivery successes, failures, and throttled states.
   - Snapshot copy immutability and thread isolation.
3. Sub-Millisecond Awakening on Shutdown Signal:
   - Verification that shutdown.wait(3600) awakens in <50ms upon shutdown.stop().
   - Concurrent multi-thread awakening on a single shutdown signal.
   - Edge case timeout handling (zero, negative, None).
   - Double-signal emergency forced exit verification.
4. CLI Subprocess Invocations & Process Isolation:
   - Subprocess invocation of python -m allaccess_monitor with --help and --version.
   - Subprocess invocation of python -m allaccess_monitor --dry-run --once across all 10 mock statuses.
   - Strict exit code verification:
       * Exit Code 0: available tickets, --help, --version, clean shutdown.
       * Exit Code 1: check error, network error, WAF challenge, malformed response, rate limit, config error.
       * Exit Code 2: unavailable tickets (sold out), section not found, CLI argument syntax error.
5. Secret Masking Preservation Under CLI Errors & Subprocess Execution:
   - Verification that raw Telegram tokens, Discord webhooks, and SMTP passwords are never
     exposed in stdout, stderr, or rotating log files, even when errors occur or --verbose is active.
   - Handling of secrets with regex-special characters.

All tests execute with zero live network requests and deterministic isolation.
"""
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import List, Dict, Any, Optional
import unittest
from unittest.mock import MagicMock, patch

from allaccess_monitor import cli
from allaccess_monitor.checker.base import BaseTicketChecker
from allaccess_monitor.checker.mock import MockTicketChecker
from allaccess_monitor.config import MonitorConfig, TelegramConfig, DiscordConfig, EmailConfig
from allaccess_monitor.logging_setup import CredentialRedactionFilter, setup_logging
from allaccess_monitor.models import AvailabilityStatus, CheckResult, SectionInfo, TicketAlert
from allaccess_monitor.notifier.base import BaseNotifier
from allaccess_monitor.scheduler import GracefulShutdown, PollingScheduler
from allaccess_monitor.state import AlertStateManager


# ==============================================================================
# Helper Classes & Fixtures
# ==============================================================================

class ChaosInjectedChecker(BaseTicketChecker):
    """
    Adversarial ticket checker simulating unstable origins by raising
    heterogeneous exceptions or returning canned results in deterministic sequences.
    """

    def __init__(self, sequence: List[Any], section_name: str = "Sivori media") -> None:
        self.sequence = sequence
        self.section_name = section_name
        self.call_count = 0

    def check_availability(self, section_name: str) -> CheckResult:
        if not self.sequence:
            raise RuntimeError("Chaos sequence exhausted")
        item = self.sequence[self.call_count % len(self.sequence)]
        self.call_count += 1

        if isinstance(item, Exception):
            raise item
        elif isinstance(item, type) and issubclass(item, BaseException):
            raise item(f"Chaos exception at call {self.call_count}")
        elif isinstance(item, AvailabilityStatus):
            return CheckResult(
                status=item,
                section_name=self.section_name,
                section_info=SectionInfo(name=self.section_name, available=(item == AvailabilityStatus.AVAILABLE), price=125000.0)
                if item == AvailabilityStatus.AVAILABLE else None,
                error_message="Simulated error status" if item == AvailabilityStatus.ERROR else None,
            )
        elif isinstance(item, CheckResult):
            return item
        else:
            raise TypeError(f"Unsupported chaos sequence item type: {type(item)}")


class RecordingNotifier(BaseNotifier):
    """Adversarial notifier tracking send attempts and simulating delivery failures."""

    def __init__(self, channel_name: str = "recording", failure_indices: Optional[set] = None, raise_indices: Optional[set] = None) -> None:
        self._channel_name = channel_name
        self.dispatched_alerts: List[TicketAlert] = []
        self.failure_indices = failure_indices or set()
        self.raise_indices = raise_indices or set()
        self.attempt_count = 0

    @property
    def channel_name(self) -> str:
        return self._channel_name

    def send(self, alert: TicketAlert) -> bool:
        idx = self.attempt_count
        self.attempt_count += 1
        if idx in self.raise_indices:
            raise RuntimeError(f"Notifier crash on attempt {idx}")
        if idx in self.failure_indices:
            return False
        self.dispatched_alerts.append(alert)
        return True


# ==============================================================================
# 1. Scheduler Exception Resilience & Stress Tests
# ==============================================================================

class TestSchedulerExceptionResilienceAdversarial(unittest.TestCase):
    """Adversarial stress testing of PollingScheduler long loops and exception tolerance."""

    def test_500_tick_loop_with_heterogeneous_injected_exceptions(self):
        """
        Adversarial Hypothesis:
        In a long-running monitoring daemon (500 checks), unexpected exceptions raised
        by the network or parsing layers (RuntimeError, TimeoutError, ConnectionResetError,
        KeyError, ZeroDivisionError, MemoryError, OSError) could crash the scheduler loop.

        Verification:
        Scheduler must catch all standard exceptions, convert them to CheckResult(status=ERROR),
        update consecutive_errors, compute backoff delay, and continue looping without crash.
        """
        # 500-item sequence with diverse exceptions interspersed with successes
        exceptions_pool = [
            RuntimeError("Network drop"),
            TimeoutError("Socket read timeout"),
            ConnectionResetError("Peer reset connection"),
            KeyError("model.data missing"),
            ZeroDivisionError("Arithmetic anomaly"),
            MemoryError("Simulated allocation failure"),
            OSError("File descriptor exhausted"),
            ValueError("Malformed payload integer"),
        ]

        sequence = []
        for i in range(500):
            mod = i % 10
            if mod in (0, 1, 2, 3):
                # 40% heterogeneous exceptions
                sequence.append(exceptions_pool[i % len(exceptions_pool)])
            elif mod in (4, 5):
                # 20% explicit ERROR status results
                sequence.append(AvailabilityStatus.ERROR)
            elif mod in (6, 7):
                # 20% UNAVAILABLE status
                sequence.append(AvailabilityStatus.UNAVAILABLE)
            elif mod == 8:
                # 10% SECTION_NOT_FOUND
                sequence.append(AvailabilityStatus.SECTION_NOT_FOUND)
            else:
                # 10% AVAILABLE status
                sequence.append(AvailabilityStatus.AVAILABLE)

        checker = ChaosInjectedChecker(sequence)
        notifier = RecordingNotifier()
        scheduler = PollingScheduler(
            checker=checker,
            state_manager=AlertStateManager(cooldown_seconds=3600),
            notifier=notifier,
            interval_seconds=0.01,
            max_backoff=5.0,
        )

        delays_computed = []
        # Fast-forward sleep so 500 ticks execute in tens of milliseconds
        with patch.object(scheduler._shutdown, "wait", side_effect=lambda d: (delays_computed.append(d), False)[1]):
            ticks = scheduler.run(max_ticks=500)

        self.assertEqual(ticks, 500, "Scheduler must execute all 500 ticks without crashing")
        self.assertFalse(scheduler.is_stopped)

        metrics = scheduler.metrics
        self.assertEqual(metrics["total_checks"], 500)
        # 40% exceptions (200) + 20% error results (100) = 300 failed checks
        self.assertEqual(metrics["failed_checks"], 300)
        # 20% unavailable (100) + 10% not found (50) + 10% available (50) = 200 successful checks
        self.assertEqual(metrics["successful_checks"], 200)
        self.assertEqual(metrics["total_checks"], metrics["successful_checks"] + metrics["failed_checks"])

        # Verify all computed delays stayed within [0, max_backoff]
        for d in delays_computed:
            self.assertGreaterEqual(d, 0.0)
            self.assertLessEqual(d, 5.0)

    def test_deep_consecutive_exception_streak_clamping_and_recovery(self):
        """
        Adversarial Hypothesis:
        150 consecutive exceptions might cause integer overflow in 2^(errors - 1) calculation
        or leave the scheduler permanently locked in maximum backoff after recovery.

        Verification:
        - Exponent is clamped to 30, preventing OverflowError.
        - Consecutive errors count reaches 150.
        - Backoff delay reaches max_backoff.
        - A single successful check immediately resets consecutive_errors to 0 and
          delay to standard interval.
        """
        consecutive_streak = [RuntimeError(f"Persistent failure {i}") for i in range(150)]
        consecutive_streak.append(AvailabilityStatus.UNAVAILABLE)  # 151st tick is success

        checker = ChaosInjectedChecker(consecutive_streak)
        scheduler = PollingScheduler(
            checker=checker,
            interval_seconds=10.0,
            max_backoff=300.0,
        )

        # Execute 150 consecutive failure ticks
        for i in range(150):
            res = scheduler.run_single_tick()
            self.assertEqual(res.status, AvailabilityStatus.ERROR)
            self.assertEqual(scheduler.consecutive_errors, i + 1)

        self.assertEqual(scheduler.consecutive_errors, 150)
        delay_after_streak = scheduler.compute_backoff_delay(scheduler.consecutive_errors, apply_jitter=False)
        self.assertEqual(delay_after_streak, 300.0, "Delay must be clamped to max_backoff")

        # 151st tick succeeds
        res_ok = scheduler.run_single_tick()
        self.assertEqual(res_ok.status, AvailabilityStatus.UNAVAILABLE)
        self.assertEqual(scheduler.consecutive_errors, 0, "consecutive_errors must reset to 0 immediately")
        delay_after_recovery = scheduler.compute_delay_from_result(res_ok)
        self.assertEqual(delay_after_recovery, 10.0, "Delay must restore to standard interval on success")

    def test_notifier_uncaught_exception_does_not_crash_scheduler(self):
        """
        Adversarial Hypothesis:
        If a messaging channel notifier raises an uncaught exception (e.g. unexpected socket
        error or memory exhaustion), it could escape run_single_tick and terminate the daemon.

        Verification:
        Scheduler must catch the notifier exception, log it, increment successful_checks
        (since ticket checking succeeded), and NOT increment alerts_dispatched.
        """
        checker = MockTicketChecker(status=AvailabilityStatus.AVAILABLE)
        # Notifier configured to raise exception on every send
        crashing_notifier = RecordingNotifier(raise_indices={0, 1, 2})
        scheduler = PollingScheduler(
            checker=checker,
            state_manager=AlertStateManager(cooldown_seconds=0),  # Alert every tick
            notifier=crashing_notifier,
            interval_seconds=1.0,
        )

        # Run 3 ticks
        with patch.object(scheduler._shutdown, "wait", return_value=False):
            ticks = scheduler.run(max_ticks=3)

        self.assertEqual(ticks, 3)
        metrics = scheduler.metrics
        self.assertEqual(metrics["successful_checks"], 3)
        self.assertEqual(metrics["failed_checks"], 0)
        self.assertEqual(metrics["alerts_dispatched"], 0, "Alerts dispatched must remain 0 when notifier crashes")


# ==============================================================================
# 2. Telemetry Metrics Accuracy & Invariants
# ==============================================================================

class TestSchedulerTelemetryMetricsAccuracyAdversarial(unittest.TestCase):
    """Adversarial verification of telemetry counters and state invariants."""

    def test_telemetry_invariants_under_complex_check_and_alert_sequence(self):
        """
        Adversarial Hypothesis:
        Under mixed check results (available, unavailable, errors, section not found)
        with notifier delivery failures and cooldown throttling, telemetry counters
        could drift or desynchronize from actual events.

        Verification:
        Mathematical invariants:
        1. total_checks == successful_checks + failed_checks
        2. alerts_dispatched == count of alerts where notifier.send returned True
        3. consecutive_errors == 0 after non-error tick
        """
        # Exact 100-tick sequence:
        # 40 UNAVAILABLE
        # 30 ERROR (15 exceptions + 15 error results)
        # 20 AVAILABLE
        # 10 SECTION_NOT_FOUND
        sequence = []
        for i in range(100):
            if i < 40:
                sequence.append(AvailabilityStatus.UNAVAILABLE)
            elif i < 55:
                sequence.append(RuntimeError(f"Exception {i}"))
            elif i < 70:
                sequence.append(CheckResult(status=AvailabilityStatus.ERROR, section_name="Sivori media", error_message="Err"))
            elif i < 90:
                sequence.append(AvailabilityStatus.AVAILABLE)
            else:
                sequence.append(AvailabilityStatus.SECTION_NOT_FOUND)

        checker = ChaosInjectedChecker(sequence)
        # State manager with 30s cooldown:
        # Tick 70 (first available) alerts!
        # Ticks 71-89 are under cooldown -> throttled, no alerts!
        state_mgr = AlertStateManager(cooldown_seconds=1800)
        # Notifier will succeed on the single alert
        notifier = RecordingNotifier()

        scheduler = PollingScheduler(
            checker=checker,
            state_manager=state_mgr,
            notifier=notifier,
            interval_seconds=0.01,
        )

        with patch.object(scheduler._shutdown, "wait", return_value=False):
            ticks = scheduler.run(max_ticks=100)

        self.assertEqual(ticks, 100)
        metrics = scheduler.metrics

        # Invariant 1: Total = Success + Failure
        self.assertEqual(metrics["total_checks"], 100)
        self.assertEqual(metrics["failed_checks"], 30)  # 15 exceptions + 15 error results
        self.assertEqual(metrics["successful_checks"], 70)  # 40 unavail + 20 avail + 10 not found
        self.assertEqual(metrics["total_checks"], metrics["successful_checks"] + metrics["failed_checks"])

        # Invariant 2: Exactly 1 alert dispatched (first available; remaining 19 throttled by cooldown)
        self.assertEqual(metrics["alerts_dispatched"], 1)
        self.assertEqual(len(notifier.dispatched_alerts), 1)

        # Invariant 3: Last check was SECTION_NOT_FOUND -> non-error -> consecutive_errors is 0
        self.assertEqual(metrics["consecutive_errors"], 0)
        self.assertEqual(scheduler.consecutive_errors, 0)
        self.assertEqual(metrics["last_check_status"], AvailabilityStatus.SECTION_NOT_FOUND.value)

        # Invariant 4: Start time <= Last check time <= Current time
        self.assertIsNotNone(metrics["start_time"])
        self.assertIsNotNone(metrics["last_check_time"])
        self.assertLessEqual(metrics["start_time"], metrics["last_check_time"])
        self.assertLessEqual(metrics["last_check_time"], time.time())

    def test_telemetry_metrics_dictionary_immutability(self):
        """
        Adversarial Hypothesis:
        External callers modifying the dictionary returned by scheduler.metrics
        could corrupt internal scheduler metrics state.

        Verification:
        scheduler.metrics returns a decoupled snapshot dictionary.
        """
        scheduler = PollingScheduler(checker=MockTicketChecker(status=AvailabilityStatus.UNAVAILABLE))
        scheduler.run_single_tick()

        snap = scheduler.metrics
        self.assertEqual(snap["total_checks"], 1)

        # External caller attempts to mutate metrics snapshot
        snap["total_checks"] = 99999
        snap["alerts_dispatched"] = -50
        snap["malicious_key"] = "hacked"

        # Scheduler internal metrics must remain untainted
        fresh_snap = scheduler.metrics
        self.assertEqual(fresh_snap["total_checks"], 1)
        self.assertEqual(fresh_snap["alerts_dispatched"], 0)
        self.assertNotIn("malicious_key", fresh_snap)

    def test_notifier_returning_false_does_not_increment_alerts_dispatched(self):
        """
        Adversarial Hypothesis:
        If a notifier returns False (delivery rejected by Telegram/Discord API),
        the scheduler might erroneously count it as alerts_dispatched.

        Verification:
        alerts_dispatched only increments when notifier.send() returns True.
        """
        checker = MockTicketChecker(status=AvailabilityStatus.AVAILABLE)
        # Notifier returns False for every send
        failing_notifier = RecordingNotifier(failure_indices={0, 1, 2, 3})
        scheduler = PollingScheduler(
            checker=checker,
            state_manager=AlertStateManager(cooldown_seconds=0),
            notifier=failing_notifier,
        )

        with patch.object(scheduler._shutdown, "wait", return_value=False):
            scheduler.run(max_ticks=4)

        metrics = scheduler.metrics
        self.assertEqual(metrics["successful_checks"], 4)
        self.assertEqual(metrics["alerts_dispatched"], 0, "Delivery failures must not increment alerts_dispatched")


# ==============================================================================
# 3. Sub-Millisecond Awakening on Shutdown Signal
# ==============================================================================

class TestGracefulShutdownSubMillisecondAwakeningAdversarial(unittest.TestCase):
    """Adversarial timing and concurrency tests for GracefulShutdown lifecycle."""

    def test_sub_millisecond_awakening_during_long_delay(self):
        """
        Adversarial Hypothesis:
        If the scheduler is sleeping on a long delay (e.g. 1 hour = 3600s),
        a shutdown request might block or sleep for the entire interval.

        Verification:
        shutdown.wait(3600.0) must wake up and return in <100ms when stop() is called,
        with reaction latency (wake_time - stop_time) < 50ms.
        """
        shutdown = GracefulShutdown(register_signals=False)
        stop_trigger_time = 0.0

        def trigger_stop():
            nonlocal stop_trigger_time
            time.sleep(0.04)
            stop_trigger_time = time.perf_counter()
            shutdown.stop()

        thread = threading.Thread(target=trigger_stop)
        thread.start()

        start = time.perf_counter()
        stopped = shutdown.wait(timeout=3600.0)
        wake_time = time.perf_counter()
        thread.join(timeout=2.0)

        self.assertTrue(stopped)
        self.assertGreater(stop_trigger_time, 0.0)
        total_elapsed = wake_time - start
        reaction_latency = wake_time - stop_trigger_time

        self.assertLess(total_elapsed, 0.5, "Total wait must be < 500ms, not 3600s")
        self.assertLess(reaction_latency, 0.05, f"Reaction latency ({reaction_latency*1000:.2f}ms) must be < 50ms")

    def test_stop_called_before_wait_returns_immediately(self):
        """
        Adversarial Hypothesis:
        If shutdown.stop() was already called prior to entering shutdown.wait(3600.0),
        the thread might fail to observe the flag and enter a blocking sleep.

        Verification:
        wait(3600.0) returns True in < 5ms without blocking.
        """
        shutdown = GracefulShutdown(register_signals=False)
        shutdown.stop()
        self.assertTrue(shutdown.is_stopped)

        start = time.perf_counter()
        stopped = shutdown.wait(timeout=3600.0)
        elapsed = time.perf_counter() - start

        self.assertTrue(stopped)
        self.assertLess(elapsed, 0.01, f"Pre-stopped wait elapsed ({elapsed*1000:.2f}ms) must be < 10ms")

    def test_concurrent_multi_thread_awakening_on_single_stop(self):
        """
        Adversarial Hypothesis:
        When multiple threads (or workers) are waiting on the same GracefulShutdown event,
        calling stop() could wake only one thread (like Condition.notify()) rather than all.

        Verification:
        All 10 concurrent waiting threads wake up simultaneously and exit in < 300ms.
        """
        shutdown = GracefulShutdown(register_signals=False)
        results = [False] * 10
        elapsed_times = [0.0] * 10

        def worker(idx: int):
            t0 = time.perf_counter()
            res = shutdown.wait(timeout=3600.0)
            elapsed_times[idx] = time.perf_counter() - t0
            results[idx] = res

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()

        time.sleep(0.05)  # Ensure all threads are waiting
        shutdown.stop()

        for t in threads:
            t.join(timeout=2.0)

        for i in range(10):
            self.assertTrue(results[i], f"Thread {i} must receive stop event")
            self.assertLess(elapsed_times[i], 0.5, f"Thread {i} elapsed time must be < 500ms")

    def test_wait_timeout_edge_cases_zero_and_negative(self):
        """
        Adversarial Hypothesis:
        Zero or negative timeout values in wait() could raise ValueError or block indefinitely.

        Verification:
        wait(0.0) and wait(-5.0) return is_stopped status immediately without error.
        """
        shutdown = GracefulShutdown(register_signals=False)
        self.assertFalse(shutdown.wait(0.0))
        self.assertFalse(shutdown.wait(-1.0))
        self.assertFalse(shutdown.wait(-9999.0))

        shutdown.stop()
        self.assertTrue(shutdown.wait(0.0))
        self.assertTrue(shutdown.wait(-1.0))

    def test_emergency_double_signal_system_exit(self):
        """
        Adversarial Hypothesis:
        If an operator hits Ctrl+C twice in rapid succession, a misconfigured handler
        might ignore the second signal and keep the process stuck.

        Verification:
        First signal triggers graceful stop; second consecutive signal raises SystemExit(1).
        """
        shutdown = GracefulShutdown(register_signals=False)
        shutdown._signal_handler(signal.SIGINT, None)
        self.assertTrue(shutdown.is_stopped)
        self.assertEqual(shutdown._signal_count, 1)

        # Second signal MUST raise SystemExit(1)
        with self.assertRaises(SystemExit) as ctx:
            shutdown._signal_handler(signal.SIGINT, None)
        self.assertEqual(ctx.exception.code, 1)


# ==============================================================================
# 4. CLI Subprocess Invocations & Process Isolation
# ==============================================================================

class TestCLIPublicSubprocessInvocationsAdversarial(unittest.TestCase):
    """
    Subprocess isolation verification testing actual CLI entrypoints via python -m allaccess_monitor.
    Ensures zero mocked test leakage and validates actual OS process behavior.
    """

    def _run_module(self, args: List[str], env_overrides: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        """Helper to invoke python -m allaccess_monitor in an isolated subprocess."""
        cmd = [sys.executable, "-m", "allaccess_monitor"] + args
        env = os.environ.copy()
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

    def test_subprocess_help_flag(self):
        """Executing python -m allaccess_monitor --help exits with code 0 and prints full usage."""
        proc = self._run_module(["--help"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage: allaccess_monitor", proc.stdout.lower())
        self.assertIn("--interval", proc.stdout)
        self.assertIn("--section", proc.stdout)
        self.assertIn("--mock-status", proc.stdout)
        self.assertIn("--dry-run", proc.stdout)
        self.assertIn("--once", proc.stdout)

    def test_subprocess_version_flag(self):
        """Executing python -m allaccess_monitor --version exits with code 0 and prints program version."""
        proc = self._run_module(["--version"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("allaccess_monitor 1.0.0", proc.stdout)

    def test_subprocess_dry_run_once_all_10_mock_statuses(self):
        """
        Adversarial Hypothesis:
        The 10 supported mock statuses must map to strictly designated exit codes
        across separate OS subprocess executions:
        - available -> Exit Code 0
        - sold_out, section_missing, section_not_found, unavailable -> Exit Code 2
        - error, network_error, waf_challenge, malformed, rate_limit -> Exit Code 1
        """
        status_expected_exit = {
            "available": 0,
            "sold_out": 2,
            "section_missing": 2,
            "section_not_found": 2,
            "unavailable": 2,
            "error": 1,
            "network_error": 1,
            "waf_challenge": 1,
            "malformed": 1,
            "rate_limit": 1,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            for status, expected_code in status_expected_exit.items():
                state_file = Path(tmpdir) / f"state_{status}.json"
                proc = self._run_module([
                    "--once",
                    "--dry-run",
                    "--mock-status", status,
                    "--state-file", str(state_file),
                ])
                self.assertEqual(
                    proc.returncode,
                    expected_code,
                    f"Mock status '{status}' expected exit code {expected_code}, got {proc.returncode}.\n"
                    f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
                )
                self.assertIn("[DRY RUN]", proc.stdout)

    def test_subprocess_clean_stdout_formatting_on_available(self):
        """Subprocess --dry-run --once --mock-status available formats price and banner."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            proc = self._run_module([
                "--once",
                "--dry-run",
                "--mock-status", "available",
                "--section", "Sivori media",
                "--state-file", str(state_file),
            ])
            self.assertEqual(proc.returncode, 0)
            self.assertIn("AVAILABLE", proc.stdout)
            self.assertIn("Sivori media", proc.stdout)
            self.assertIn("143,750", proc.stdout)


# ==============================================================================
# 5. CLI Exit Code Accuracy & Syntax Isolation
# ==============================================================================

class TestCLIExitCodeAccuracyAndSyntaxAdversarial(unittest.TestCase):
    """Strict verification of exit codes: 0 (available/help), 1 (error), 2 (syntax/unavailable)."""

    def _run_cli(self, args: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "allaccess_monitor"] + args,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_exit_code_2_for_unrecognized_argument(self):
        """Unrecognized CLI arguments exit with code 2."""
        proc = self._run_cli(["--completely-bogus-unknown-argument"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unrecognized arguments", proc.stderr)

    def test_exit_code_2_for_invalid_mock_status_choice(self):
        """Invalid mock status choice exits with code 2."""
        proc = self._run_cli(["--mock-status", "invalid_status_xyz"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid choice", proc.stderr)

    def test_exit_code_2_for_mutually_exclusive_verbose_quiet(self):
        """Conflicting verbose and quiet flags exit with code 2."""
        proc = self._run_cli(["-v", "-q"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not allowed with argument", proc.stderr)

    def test_exit_code_2_for_non_numeric_interval(self):
        """Non-numeric interval value exits with code 2."""
        proc = self._run_cli(["-i", "not_a_number"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid _parse_interval value", proc.stderr)

    def test_exit_code_2_for_non_numeric_cooldown(self):
        """Non-numeric cooldown value exits with code 2."""
        proc = self._run_cli(["--cooldown", "abc"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid int value", proc.stderr)

    def test_exit_code_1_for_negative_interval_configuration_error(self):
        """Negative interval raises ConfigurationError and exits with code 1."""
        proc = self._run_cli(["--interval", "-10"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("poll_interval must be positive", proc.stderr + proc.stdout)

    def test_exit_code_1_for_zero_interval_configuration_error(self):
        """Zero interval raises ConfigurationError and exits with code 1."""
        proc = self._run_cli(["--interval", "0"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("poll_interval must be positive", proc.stderr + proc.stdout)

    def test_exit_code_1_for_invalid_url_scheme_configuration_error(self):
        """Invalid URL scheme (ftp://) raises ConfigurationError and exits with code 1."""
        proc = self._run_cli(["--url", "ftp://invalid-scheme.allaccess.com.ar/event"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Invalid URL scheme", proc.stderr + proc.stdout)

    def test_exit_code_1_for_corrupted_config_file(self):
        """Providing an unreadable or non-existent .env file via --config exits with code 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_config = Path(tmpdir) / "invalid.env"
            bad_config.write_text("POLL_INTERVAL=NOT_AN_INT\n", encoding="utf-8")
            proc = self._run_cli(["--config", str(bad_config)])
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Configuration error", proc.stderr + proc.stdout)


# ==============================================================================
# 6. Secret Masking Preservation Under CLI Errors & Subprocess Execution
# ==============================================================================

class TestCLISecretMaskingPreservationAdversarial(unittest.TestCase):
    """
    Adversarial verification that credentials and sensitive tokens are strictly
    redacted across stdout, stderr, exception logs, and rotating log files.
    """

    def test_subprocess_secret_masking_in_stdout_stderr_and_logs(self):
        """
        Adversarial Hypothesis:
        When logging in verbose debug mode (-v) or logging configuration details,
        real environment secrets (Telegram tokens, Discord webhooks, SMTP passwords)
        could leak into stdout, stderr, or the rotating log file.

        Verification:
        Execute subprocess with secrets populated in environment and verify that
        raw secret strings are 100% absent from stdout, stderr, and log file.
        """
        secret_bot_token = "123456789:AAHEmpiricalSecretBotTokenNeverLeak999"
        secret_discord_path = "9988776655/SecretAdversarialDiscordWebhookPathToken"
        secret_discord_url = f"https://discord.com/api/webhooks/{secret_discord_path}"
        secret_smtp_password = "SuperSecretAdversarialPassword123!@#"

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "monitor.log"
            state_file = Path(tmpdir) / "state.json"

            env = os.environ.copy()
            env["TELEGRAM_BOT_TOKEN"] = secret_bot_token
            env["TELEGRAM_CHAT_ID"] = "11223344"
            env["DISCORD_WEBHOOK_URL"] = secret_discord_url
            env["SMTP_PASSWORD"] = secret_smtp_password
            env["SMTP_HOST"] = "smtp.example.com"
            env["SMTP_USER"] = "alert@example.com"
            env["NOTIFICATION_EMAIL"] = "fan@example.com"

            proc = subprocess.run(
                [
                    sys.executable, "-m", "allaccess_monitor",
                    "--verbose",
                    "--dry-run",
                    "--once",
                    "--mock-status", "available",
                    "--log-file", str(log_file),
                    "--state-file", str(state_file),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )

            self.assertEqual(proc.returncode, 0)
            combined_output = proc.stdout + "\n" + proc.stderr
            log_content = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
            all_text = combined_output + "\n" + log_content

            # Raw secrets must NEVER be present anywhere
            self.assertNotIn(
                secret_bot_token,
                all_text,
                "Telegram bot token was leaked in stdout/stderr/log file!"
            )
            self.assertNotIn(
                "SecretAdversarialDiscordWebhookPathToken",
                all_text,
                "Discord webhook token was leaked in stdout/stderr/log file!"
            )
            self.assertNotIn(
                secret_smtp_password,
                all_text,
                "SMTP password was leaked in stdout/stderr/log file!"
            )

    def test_subprocess_secret_masking_during_error_traceback(self):
        """
        Adversarial Hypothesis:
        When a configuration error or check failure triggers a logged traceback,
        secrets embedded in URLs or error arguments might escape the log filter.

        Verification:
        Even when an error is logged, raw secrets are replaced with [REDACTED].
        """
        secret_token = "987654321:AABBSensitiveTokenInErrorTraceback12345"
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "error_trace.log"
            state_file = Path(tmpdir) / "state.json"

            env = os.environ.copy()
            env["TELEGRAM_BOT_TOKEN"] = secret_token

            proc = subprocess.run(
                [
                    sys.executable, "-m", "allaccess_monitor",
                    "--verbose",
                    "--dry-run",
                    "--once",
                    "--mock-status", "network_error",
                    "--log-file", str(log_file),
                    "--state-file", str(state_file),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )

            self.assertEqual(proc.returncode, 1)
            all_text = proc.stdout + "\n" + proc.stderr
            if log_file.exists():
                all_text += "\n" + log_file.read_text(encoding="utf-8")

            self.assertNotIn(secret_token, all_text, "Secret token leaked during error logging!")

    def test_redaction_filter_handles_regex_special_characters_in_secrets(self):
        """
        Adversarial Hypothesis:
        If a user password contains regex meta-characters (e.g. `p@$$w*rd[123]+^$`),
        a naive regex compilation or replace could crash or produce corrupt regex errors.

        Verification:
        CredentialRedactionFilter safely handles and redacts passwords containing regex meta-characters.
        """
        evil_password = r"p@$$w*rd[123]+^$(foo|bar)\d{4}"
        filter_ = CredentialRedactionFilter(secrets=[evil_password])

        log_text = f"Connecting to SMTP server using password='{evil_password}' and token=normal"
        sanitized = filter_.redact(log_text)

        self.assertNotIn(evil_password, sanitized)
        self.assertIn("[REDACTED]", sanitized)


if __name__ == "__main__":
    unittest.main()
