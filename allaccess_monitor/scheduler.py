"""
Responsive Polling Scheduler and Graceful Shutdown Handler.
Features F12 (Responsive Polling Scheduler) and F13 (Exponential Backoff & Jitter).
Orchestrates ticket availability checking, state evaluation, multi-channel alerting,
exponential backoff, full jitter, HTTP 429 Retry-After parsing, OS signal trapping,
sub-millisecond interruptible waiting, and telemetry metrics tracking.
"""
import email.utils
import logging
import math
import random
import re
import signal
import sys
import threading
import time
from typing import Optional, Dict, Any, Union

from allaccess_monitor.checker.base import BaseTicketChecker
from allaccess_monitor.models import AvailabilityStatus, CheckResult, TicketAlert
from allaccess_monitor.notifier.base import BaseNotifier
from allaccess_monitor.state import AlertStateManager

logger = logging.getLogger(__name__)


class GracefulShutdown:
    """
    Manages process lifecycle and responsive interruptible wait across Windows and POSIX.
    Uses threading.Event for sub-millisecond thread awakening.
    Tracks signal count: first signal triggers graceful shutdown,
    second consecutive signal forces immediate termination via SystemExit(1).
    """

    def __init__(self, register_signals: bool = True) -> None:
        self._stop_event = threading.Event()
        self._signal_count = 0
        if register_signals:
            self._register_signals()

    def _register_signals(self) -> None:
        """Register OS signal handlers safely (avoids error when not on main thread)."""
        try:
            signal.signal(signal.SIGINT, self._signal_handler)
        except (ValueError, AttributeError, RuntimeError):
            logger.debug("SIGINT handler could not be registered (likely not main thread).")

        sigterm = getattr(signal, "SIGTERM", None)
        if sigterm is not None:
            try:
                signal.signal(sigterm, self._signal_handler)
            except (ValueError, AttributeError, RuntimeError):
                logger.debug("SIGTERM handler could not be registered (likely not main thread).")

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Handle incoming termination signals."""
        self._signal_count += 1
        sig_name = str(signum)
        try:
            if hasattr(signal, "Signals"):
                sig_name = signal.Signals(signum).name
        except Exception:
            pass

        if self._signal_count == 1:
            logger.info("Received signal %s (%d). Stopping scheduler gracefully...", sig_name, signum)
            self.stop()
        else:
            logger.warning("Received repeat signal %s (%d). Forcing immediate exit.", sig_name, signum)
            sys.exit(1)

    @property
    def is_stopped(self) -> bool:
        """Returns True if a stop signal or call has occurred."""
        return self._stop_event.is_set()

    def stop(self) -> None:
        """Signal the scheduler loop to stop immediately."""
        self._stop_event.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """
        Wait on the stop event up to `timeout` seconds.
        Returns True if stop was requested, False if timeout elapsed.
        """
        if timeout is not None and timeout <= 0:
            return self.is_stopped
        return self._stop_event.wait(timeout=timeout)


class PollingScheduler:
    """
    Continuous monitoring loop orchestrating ticket checks, state evaluation,
    multi-channel alerting, and exponential backoff resilience.
    """

    def __init__(
        self,
        checker: Optional[BaseTicketChecker] = None,
        state_manager: Optional[AlertStateManager] = None,
        notifier: Optional[BaseNotifier] = None,
        interval_seconds: Optional[float] = None,
        max_backoff: float = 300.0,
        section_name: Optional[str] = None,
        shutdown: Optional[GracefulShutdown] = None,
        config: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        self.checker = checker
        self.state_manager = state_manager or AlertStateManager()
        self.notifier = notifier
        self.config = config

        # Resolve interval_seconds with kwargs aliases and config
        resolved_interval = interval_seconds
        if resolved_interval is None:
            resolved_interval = kwargs.get("poll_interval") or kwargs.get("check_interval")
        if resolved_interval is None and config is not None:
            resolved_interval = getattr(config, "check_interval_seconds", None) or getattr(config, "poll_interval", None)
        if resolved_interval is None:
            resolved_interval = 30.0

        self.interval_seconds = max(0.001, float(resolved_interval))

        # Resolve max_backoff
        resolved_max_backoff = max_backoff
        if config is not None and hasattr(config, "max_backoff"):
            resolved_max_backoff = getattr(config, "max_backoff")
        self.max_backoff = max(self.interval_seconds, float(resolved_max_backoff))

        # Resolve section_name
        resolved_section = section_name or kwargs.get("target_section")
        if resolved_section is None and config is not None:
            resolved_section = getattr(config, "target_section", None)
        self.section_name = str(resolved_section or "Sivori media")

        self._shutdown = shutdown or GracefulShutdown()
        self._consecutive_errors: int = 0
        self._metrics: Dict[str, Any] = {
            "total_checks": 0,
            "alerts_dispatched": 0,
            "successful_checks": 0,
            "failed_checks": 0,
            "consecutive_errors": 0,
            "last_check_status": None,
            "last_check_time": None,
            "start_time": time.time(),
        }

    @property
    def consecutive_errors(self) -> int:
        """Current streak of consecutive check failures."""
        return self._consecutive_errors

    @property
    def is_stopped(self) -> bool:
        """Returns True if the scheduler loop has been stopped."""
        return self._shutdown.is_stopped

    @property
    def metrics(self) -> Dict[str, Any]:
        """Returns a snapshot copy of telemetry metrics."""
        return dict(self._metrics)

    def stop(self) -> None:
        """Signals graceful shutdown to terminate scheduler."""
        self._shutdown.stop()

    def compute_backoff_delay(self, consecutive_errors: int, apply_jitter: bool = True) -> float:
        """
        Compute exponential backoff delay with optional uniform jitter.
        Formula: base * 2^(errors - 1) clamped to max_backoff.
        Exponent is clamped to 30 to prevent overflow.
        """
        if consecutive_errors <= 0:
            return float(self.interval_seconds)

        exp = min(consecutive_errors - 1, 30)
        raw_delay = float(self.interval_seconds * (2 ** exp))
        clamped_delay = min(raw_delay, float(self.max_backoff))

        if apply_jitter:
            return random.uniform(0.5, 1.0) * clamped_delay

        return int(clamped_delay) if clamped_delay.is_integer() else clamped_delay

    def compute_delay_from_result(self, result: CheckResult) -> float:
        """
        Determine sleep delay following a check result.
        Prioritizes HTTP 429 Retry-After header, applies exponential backoff on error,
        and uses standard interval on success.
        """
        # 1. Parse Retry-After if present in error_message or result attribute
        if result.error_message and "retry-after:" in result.error_message.lower():
            match = re.search(r"retry-after:\s*([^\s,;]+)", result.error_message, re.IGNORECASE)
            if match:
                token = match.group(1).strip()
                try:
                    num_val = float(token)
                    clamped = min(max(0.0, num_val), float(self.max_backoff))
                    return int(clamped) if clamped.is_integer() else clamped
                except ValueError:
                    # Try parsing RFC 7231 / RFC 1123 HTTP-date
                    try:
                        date_tuple = email.utils.parsedate_to_datetime(token)
                        diff = max(0.0, (date_tuple.timestamp() - time.time()))
                        clamped = min(diff, float(self.max_backoff))
                        return int(clamped) if clamped.is_integer() else clamped
                    except Exception:
                        pass
                    # Non-numeric unparseable date falls back to exponential backoff
                    return self.compute_backoff_delay(max(1, self._consecutive_errors), apply_jitter=True)

        # 2. Check for Error status or HTTP error code
        is_err = (result.status == AvailabilityStatus.ERROR) or bool(
            result.status_code and result.status_code >= 400
        )
        if is_err:
            return self.compute_backoff_delay(max(1, self._consecutive_errors), apply_jitter=True)

        # 3. Standard successful interval
        return float(self.interval_seconds)

    def run_single_tick(self) -> CheckResult:
        """
        Execute a single check-evaluate-notify cycle.
        Returns the CheckResult instance. Never raises uncaught exceptions.
        """
        try:
            if self.checker is None:
                raise RuntimeError("No checker configured for PollingScheduler")
            result = self.checker.check_availability(self.section_name)
        except Exception as exc:
            logger.error("Checker raised unexpected exception: %s", exc, exc_info=True)
            result = CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=self.section_name,
                error_message=f"Checker exception: {exc}",
                status_code=None,
            )

        self._metrics["total_checks"] += 1
        self._metrics["last_check_status"] = result.status.value
        self._metrics["last_check_time"] = time.time()

        if result.status == AvailabilityStatus.ERROR:
            self._consecutive_errors += 1
            self._metrics["failed_checks"] += 1
            self._metrics["consecutive_errors"] = self._consecutive_errors
            logger.warning(
                "Check error on section '%s' (consecutive errors: %d): %s",
                self.section_name,
                self._consecutive_errors,
                result.error_message,
            )
        else:
            self._consecutive_errors = 0
            self._metrics["successful_checks"] += 1
            self._metrics["consecutive_errors"] = 0

            # State machine evaluation (only on non-error results)
            if self.state_manager is not None:
                is_avail = (result.status == AvailabilityStatus.AVAILABLE)
                decision = self.state_manager.process_check_result(is_available=is_avail)

                if decision.should_alert:
                    alert = result.to_alert(is_reminder=decision.is_reminder)
                    if self.notifier is not None:
                        try:
                            sent = self.notifier.send(alert)
                            if sent:
                                self._metrics["alerts_dispatched"] += 1
                                logger.info("Alert dispatched successfully for '%s'.", result.section_name)
                            else:
                                logger.warning("Notifier returned False for alert on '%s'.", result.section_name)
                        except Exception as exc:
                            logger.error("Notifier raised unhandled exception during send: %s", exc, exc_info=True)

        return result

    def run_once(self) -> CheckResult:
        """Alias for run_single_tick() for one-shot execution."""
        return self.run_single_tick()

    def run(self, max_ticks: Optional[int] = None) -> int:
        """
        Execute continuous polling loop until shutdown signal or max_ticks reached.
        Returns total ticks executed.
        """
        if max_ticks is not None and max_ticks <= 0:
            return 0

        ticks = 0
        logger.info(
            "Starting PollingScheduler (interval=%.2fs, max_backoff=%.2fs, section='%s')...",
            self.interval_seconds,
            self.max_backoff,
            self.section_name,
        )

        try:
            while not self._shutdown.is_stopped:
                result = self.run_single_tick()
                ticks += 1

                delay = self.compute_delay_from_result(result)
                stopped = self._shutdown.wait(delay)
                if stopped:
                    logger.info("Shutdown signaled during wait. Exiting loop.")
                    break

                if max_ticks is not None and ticks >= max_ticks:
                    logger.debug("Reached max_ticks (%d). Terminating loop.", max_ticks)
                    break
        finally:
            logger.info("PollingScheduler terminated after %d ticks.", ticks)

        return ticks

    def close(self) -> None:
        """Closes scheduler and releases underlying checker/notifier resources."""
        self.stop()
        if hasattr(self.checker, "close"):
            try:
                self.checker.close()
            except Exception:
                pass

    def __enter__(self) -> "PollingScheduler":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
