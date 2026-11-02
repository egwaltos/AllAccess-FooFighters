"""
Responsive Polling Scheduler and Graceful Shutdown Handler.
Features F12 (Responsive Polling Scheduler) and F13 (Exponential Backoff & Jitter).
Orchestrates ticket availability checking, state evaluation, multi-channel alerting,
exponential backoff, full jitter, HTTP 429 Retry-After parsing, OS signal trapping,
sub-millisecond interruptible waiting, and telemetry metrics tracking.
"""
from dataclasses import replace
import email.utils
import logging
import math
import random
import re
import signal
import sys
import threading
import time
from typing import Optional, Dict, Any, Union, List

from allaccess_monitor.checker.base import BaseTicketChecker
from allaccess_monitor.models import AvailabilityStatus, CheckResult, MonitoredTarget, TicketAlert
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
    Continuous monitoring loop orchestrating ticket checks across multiple targets,
    state evaluation, multi-channel alerting, and exponential backoff resilience.
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
        targets: Optional[List[Any]] = None,
        alert_burst_delay: float = 0.0,
        reservation_engine: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        self.checker = checker
        self.state_manager = state_manager or AlertStateManager()
        self.notifier = notifier
        self.config = config
        self.reservation_engine = reservation_engine
        self.alert_burst_delay = max(0.0, float(alert_burst_delay if alert_burst_delay is not None else kwargs.get("alert_burst_delay", 0.0)))

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

        # Resolve primary section_name for backward compatibility
        resolved_section = section_name or kwargs.get("target_section")
        if resolved_section is None and config is not None:
            resolved_section = getattr(config, "target_section", None)
        self.section_name = str(resolved_section or "Sivori media")

        # Resolve monitored targets
        resolved_targets = targets or kwargs.get("targets")
        if resolved_targets is None and config is not None:
            resolved_targets = getattr(config, "targets", None)

        checker_url = getattr(checker, "event_url", None)
        if not isinstance(checker_url, str):
            checker_url = None

        config_url = getattr(config, "event_url", None) if config else None
        if not isinstance(config_url, str):
            config_url = None

        default_url = checker_url or config_url or "https://www.allaccess.com.ar/event/foo-fighters"

        self.targets: List[MonitoredTarget] = []
        if resolved_targets:
            for t in resolved_targets:
                if isinstance(t, MonitoredTarget):
                    self.targets.append(t)
                elif isinstance(t, dict):
                    self.targets.append(MonitoredTarget(
                        event_url=t.get("event_url") or t.get("url") or default_url,
                        target_section=t.get("target_section") or t.get("section") or self.section_name,
                        event_name=t.get("event_name") or t.get("name"),
                        max_price=t.get("max_price"),
                    ))
                elif isinstance(t, str):
                    self.targets.append(MonitoredTarget(
                        event_url=default_url,
                        target_section=t.strip(),
                    ))
                else:
                    self.targets.append(t)
        else:
            self.targets = [MonitoredTarget(event_url=default_url, target_section=self.section_name)]

        self._shutdown = shutdown or GracefulShutdown()
        self._consecutive_errors: int = 0
        self._last_tick_results: List[CheckResult] = []
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

    @property
    def last_results(self) -> List[CheckResult]:
        """List of CheckResults produced in the most recent polling cycle."""
        return list(self._last_tick_results)

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

    def compute_delay_for_tick(self) -> float:
        """Compute optimal delay following a multi-target tick, honoring Retry-After across any target."""
        if not self._last_tick_results:
            return float(self.interval_seconds)
        delays = [self.compute_delay_from_result(r) for r in self._last_tick_results]
        return max(delays) if delays else float(self.interval_seconds)

    def _check_single_target(self, target: MonitoredTarget) -> CheckResult:
        """Execute check for a single target defensively."""
        try:
            if self.checker is None:
                raise RuntimeError("No checker configured for PollingScheduler")

            target_section = getattr(target, "target_section", str(target))
            target_url = getattr(target, "event_url", None)

            checker_url = getattr(self.checker, "event_url", None)
            url_changed = (
                isinstance(target_url, str)
                and isinstance(checker_url, str)
                and target_url != checker_url
            )

            try:
                if url_changed:
                    self.checker.event_url = target_url
                result = self.checker.check_availability(target_section)
            finally:
                if url_changed:
                    self.checker.event_url = checker_url

            # Enrich result event_name / event_url if custom target attributes provided
            if hasattr(target, "event_name") and target.event_name and (not result.event_name or result.event_name == "Foo Fighters"):
                result = replace(result, event_name=target.event_name)
            if hasattr(target, "event_url") and target.event_url and not result.event_url:
                result = replace(result, event_url=target.event_url)

            return result
        except Exception as exc:
            logger.error(
                "Checker raised unexpected exception for target '%s': %s",
                getattr(target, "target_section", target),
                exc,
                exc_info=True,
            )
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=getattr(target, "target_section", str(target)),
                event_url=getattr(target, "event_url", ""),
                error_message=f"Checker exception: {exc}",
                status_code=None,
            )

    def run_single_tick(self) -> CheckResult:
        """
        Execute a single check-evaluate-notify cycle across all configured targets.
        Evaluates state independently per target, dispatches alerts, and updates metrics.
        Returns the primary CheckResult (prioritizing AVAILABLE over other statuses).
        Never raises uncaught exceptions.
        """
        results: List[CheckResult] = []
        any_error = False
        any_success = False

        # Optimized batch check if checker supports check_targets and multiple targets configured
        if len(self.targets) > 1 and hasattr(self.checker, "check_targets") and callable(getattr(self.checker, "check_targets")):
            try:
                batch = self.checker.check_targets(self.targets)
                if isinstance(batch, (list, tuple)) and all(isinstance(r, CheckResult) for r in batch):
                    results = list(batch)
            except Exception as exc:
                logger.error("Checker.check_targets raised unexpected exception: %s", exc, exc_info=True)
                results = [
                    CheckResult(
                        status=AvailabilityStatus.ERROR,
                        section_name=t.target_section,
                        event_url=t.event_url,
                        error_message=f"Checker exception: {exc}",
                        status_code=None,
                    )
                    for t in self.targets
                ]

        if not results:
            for target in self.targets:
                res = self._check_single_target(target)
                results.append(res)

        self._last_tick_results = results
        primary_result: Optional[CheckResult] = None
        tick_alerts_sent = 0

        for target, result in zip(self.targets, results):
            self._metrics["total_checks"] += 1
            self._metrics["last_check_status"] = result.status.value
            self._metrics["last_check_time"] = time.time()

            if result.status == AvailabilityStatus.ERROR:
                any_error = True
                self._metrics["failed_checks"] += 1
                logger.warning(
                    "Check error on '%s' section '%s': %s",
                    result.event_url,
                    target.target_section,
                    result.error_message,
                )
            else:
                any_success = True
                self._metrics["successful_checks"] += 1

            # State machine evaluation (handles transient error immunity internally)
            if self.state_manager is not None:
                try:
                    decision = self.state_manager.process_check_result(
                        result=result,
                        target=target,
                    )
                except TypeError:
                    try:
                        decision = self.state_manager.process_check_result(result)
                    except TypeError:
                        decision = self.state_manager.process_check_result(
                            is_available=(result.status == AvailabilityStatus.AVAILABLE)
                        )

                if decision.should_alert:
                    # Budget limit guard
                    effective_price = (
                        result.section_info.total_price
                        if (result.section_info and result.section_info.total_price is not None)
                        else (result.section_info.price if result.section_info else None)
                    )
                    if target.max_price is not None and effective_price is not None:
                        if effective_price > target.max_price:
                            logger.info(
                                "Tickets available for '%s' but price %.2f exceeds max_price %.2f. Suppressing alert.",
                                target.target_section,
                                effective_price,
                                target.max_price,
                            )
                            if self.state_manager is not None and hasattr(self.state_manager, "get_target_state"):
                                ts = self.state_manager.get_target_state(target)
                                ts.last_alert_time = None
                                if ts.total_alerts_sent > 0:
                                    ts.total_alerts_sent -= 1
                            continue

                    alert = result.to_alert(is_reminder=decision.is_reminder)
                    if self.notifier is not None:
                        if self.alert_burst_delay > 0 and tick_alerts_sent > 0:
                            self._shutdown.wait(self.alert_burst_delay)
                        try:
                            sent = self.notifier.send(alert)
                            if sent:
                                tick_alerts_sent += 1
                                self._metrics["alerts_dispatched"] += 1
                                logger.info(
                                    "Alert dispatched successfully for '%s' - '%s'.",
                                    result.event_name,
                                    result.section_name,
                                )
                            else:
                                logger.warning(
                                    "Notifier returned False for alert on '%s' - '%s'.",
                                    result.event_name,
                                    result.section_name,
                                )
                        except Exception as exc:
                            logger.error("Notifier raised unhandled exception during send: %s", exc, exc_info=True)

                    # Optional automatic cart reservation hook (runs after alert is sent)
                    if self.reservation_engine is not None and result.status == AvailabilityStatus.AVAILABLE:
                        try:
                            self.reservation_engine.reserve_for_target(target, result)
                        except Exception as exc:
                            logger.error(
                                "Auto-reservation hook failed for '%s': %s",
                                target.target_section,
                                exc,
                                exc_info=True,
                            )

            # Prioritize AVAILABLE status for primary result
            if primary_result is None:
                primary_result = result
            elif primary_result.status != AvailabilityStatus.AVAILABLE and result.status == AvailabilityStatus.AVAILABLE:
                primary_result = result

        # Streak update
        if any_error and not any_success:
            self._consecutive_errors += 1
        elif any_success:
            self._consecutive_errors = 0
        self._metrics["consecutive_errors"] = self._consecutive_errors

        return primary_result if primary_result is not None else results[0]

    def run_targets_tick(self) -> List[CheckResult]:
        """Execute a single check cycle and return all target CheckResult instances."""
        self.run_single_tick()
        return list(self._last_tick_results)

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
            "Starting PollingScheduler (interval=%.2fs, max_backoff=%.2fs, targets=%d)...",
            self.interval_seconds,
            self.max_backoff,
            len(self.targets),
        )

        try:
            while not self._shutdown.is_stopped:
                result = self.run_single_tick()
                ticks += 1

                delay = self.compute_delay_for_tick()
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
