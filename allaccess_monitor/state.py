"""
State machine and alert throttling management for AllAccess ticket monitoring.
Implements edge detection, cooldown throttling, reminder alerts, reset on unavailable,
transient check error immunity, monotonic time tracking, and robust state persistence.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Optional, Union, Callable, Dict, Any

from allaccess_monitor.models import AvailabilityStatus, CheckResult

logger = logging.getLogger(__name__)


@dataclass
class AlertDecision:
    """Decision output produced by AlertStateManager evaluating a check result."""
    should_alert: bool
    is_reminder: bool = False
    reason: str = ""


class AlertStateManager:
    """
    Finite State Machine (FSM) governing ticket alert dispatching.

    Key capabilities:
    1. Edge Detection: Immediate alert when tickets transition from UNAVAILABLE -> AVAILABLE.
    2. Cooldown Throttling: Suppresses duplicate notifications while tickets remain available.
    3. Reminder Alerts: Dispatches a reminder when availability persists beyond cooldown_seconds.
    4. State Reset: Transition to UNAVAILABLE resets state so subsequent availability alerts immediately.
    5. Transient Error Immunity: Check errors (HTTP 5xx, timeouts, WAF) preserve availability state.
    6. Monotonic Time Tracking: Uses time.monotonic() by default to prevent clock skew issues.
    7. Persistence: Atomically saves and loads state to/from a JSON state file across daemon restarts.
    """

    def __init__(
        self,
        cooldown_seconds: int = 1800,
        state_file: Optional[Union[Path, str]] = None,
        initial_available: bool = False,
        time_func: Optional[Callable[[], float]] = None,
        notify_on_sold_out: bool = False,
    ) -> None:
        """
        Initialize AlertStateManager.

        :param cooldown_seconds: Minimum duration in seconds between alerts for sustained availability.
        :param state_file: Optional path to JSON file for persisting monitor state.
        :param initial_available: Initial availability assumption (default False).
        :param time_func: Callable returning current time in seconds (default time.monotonic).
        :param notify_on_sold_out: Flag for sold out notifications (default False).
        """
        if cooldown_seconds < 0:
            raise ValueError(f"cooldown_seconds cannot be negative, got {cooldown_seconds}")

        self.cooldown_seconds: int = int(cooldown_seconds)
        self.state_file: Optional[Path] = Path(state_file) if state_file else None
        self.time_func: Callable[[], float] = time_func or time.monotonic
        self.notify_on_sold_out: bool = notify_on_sold_out

        self.is_currently_available: bool = initial_available
        self.last_alert_time: Optional[float] = None
        self.consecutive_errors: int = 0
        self.total_alerts_sent: int = 0

        # Attempt to recover persisted state if file is specified
        if self.state_file:
            self._load_state()

    @property
    def is_available(self) -> bool:
        """Alias for is_currently_available."""
        return self.is_currently_available

    @property
    def last_alert_timestamp(self) -> Optional[float]:
        """Alias for last_alert_time."""
        return self.last_alert_time

    def process_check_result(
        self,
        is_available: Union[CheckResult, AvailabilityStatus, bool, None] = None,
        current_time: Optional[float] = None,
        is_error: bool = False,
        result: Optional[Union[CheckResult, AvailabilityStatus, bool]] = None,
    ) -> AlertDecision:
        """
        Evaluate availability status and decide whether to alert, remind, or suppress.

        Accepts either boolean availability, an AvailabilityStatus enum, or a CheckResult domain object.

        :param is_available: Boolean availability, CheckResult, or AvailabilityStatus.
        :param current_time: Optional explicit timestamp (for deterministic simulation and unit tests).
        :param is_error: If True, treats check as a transient error preserving current availability state.
        :param result: Alternative keyword alias for is_available.
        :return: AlertDecision indicating whether an alert should be dispatched and why.
        """
        target = result if result is not None else is_available
        if target is None:
            raise ValueError("process_check_result requires an availability status or CheckResult")

        # Determine if check result signifies a transient error
        check_is_error = is_error
        check_is_avail = False

        if isinstance(target, CheckResult):
            if target.status == AvailabilityStatus.ERROR:
                check_is_error = True
            else:
                check_is_avail = (target.status == AvailabilityStatus.AVAILABLE)
        elif isinstance(target, AvailabilityStatus):
            if target == AvailabilityStatus.ERROR:
                check_is_error = True
            else:
                check_is_avail = (target == AvailabilityStatus.AVAILABLE)
        elif isinstance(target, bool):
            check_is_avail = target
        else:
            if hasattr(target, "status") and getattr(target, "status") == AvailabilityStatus.ERROR:
                check_is_error = True
            elif hasattr(target, "is_available"):
                check_is_avail = bool(getattr(target, "is_available"))
            else:
                check_is_avail = bool(target)

        now = float(current_time) if current_time is not None else self.time_func()

        # Rule 1: Transient Error Immunity
        if check_is_error:
            self.consecutive_errors += 1
            logger.debug(
                "Transient check error encountered (consecutive=%d). Preserving availability state: %s",
                self.consecutive_errors,
                self.is_currently_available,
            )
            return AlertDecision(
                should_alert=False,
                is_reminder=False,
                reason=f"Transient check error (consecutive_errors={self.consecutive_errors}); availability state preserved.",
            )

        # Successful check: clear error streak
        self.consecutive_errors = 0

        # Rule 2: Tickets Detected as Available
        if check_is_avail:
            if not self.is_currently_available:
                # Transition: UNAVAILABLE -> AVAILABLE (First Detection Edge!)
                self.is_currently_available = True
                self.last_alert_time = now
                self.total_alerts_sent += 1
                self._save_state()
                logger.info(
                    "Ticket availability edge detected: UNAVAILABLE -> AVAILABLE. Alerting immediately."
                )
                return AlertDecision(
                    should_alert=True,
                    is_reminder=False,
                    reason="Initial ticket availability detected (edge transition)!",
                )
            else:
                # Steady State: AVAILABLE -> AVAILABLE
                last_alert = self.last_alert_time if self.last_alert_time is not None else now
                elapsed = now - last_alert

                # Clock skew detection: system time jumped backwards
                if elapsed < 0:
                    logger.warning(
                        "Clock skew detected: current_time (%.2f) is before last_alert_time (%.2f). "
                        "Adjusting reference timestamp without alerting.",
                        now,
                        last_alert,
                    )
                    self.last_alert_time = now
                    self._save_state()
                    return AlertDecision(
                        should_alert=False,
                        is_reminder=False,
                        reason="Clock skew detected; reference timestamp adjusted.",
                    )

                # Check cooldown threshold
                if elapsed >= self.cooldown_seconds:
                    # Cooldown expired: send reminder alert
                    self.last_alert_time = now
                    self.total_alerts_sent += 1
                    self._save_state()
                    logger.info(
                        "Tickets remain available and cooldown expired (elapsed=%.1fs >= %ds). Dispatching reminder alert.",
                        elapsed,
                        self.cooldown_seconds,
                    )
                    return AlertDecision(
                        should_alert=True,
                        is_reminder=True,
                        reason=f"Cooldown expired ({int(elapsed)}s >= {self.cooldown_seconds}s). Sending reminder alert.",
                    )
                else:
                    # Within cooldown: throttle duplicate notification
                    remaining = int(self.cooldown_seconds - elapsed)
                    logger.debug(
                        "Tickets continuously available. Alert throttled under cooldown (%ds remaining).",
                        remaining,
                    )
                    return AlertDecision(
                        should_alert=False,
                        is_reminder=False,
                        reason=f"Tickets continuously available. Alert throttled ({remaining}s remaining in cooldown).",
                    )

        # Rule 3: Tickets Detected as Unavailable
        else:
            if self.is_currently_available:
                # Transition: AVAILABLE -> UNAVAILABLE (Tickets Sold Out / Vanished)
                logger.info("Ticket availability transition: AVAILABLE -> UNAVAILABLE (State reset).")
                self.is_currently_available = False
                self.last_alert_time = None
                self._save_state()
                return AlertDecision(
                    should_alert=False,
                    is_reminder=False,
                    reason="Tickets are unavailable (sold out). State reset.",
                )
            return AlertDecision(
                should_alert=False,
                is_reminder=False,
                reason="Tickets are unavailable.",
            )

    def reset(self) -> None:
        """Explicitly reset state to UNAVAILABLE and clear alert history."""
        self.is_currently_available = False
        self.last_alert_time = None
        self.consecutive_errors = 0
        self._save_state()
        logger.info("AlertStateManager state explicitly reset.")

    def _save_state(self) -> None:
        """Persist state to JSON file using atomic write."""
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "is_currently_available": self.is_currently_available,
                "last_alert_time": self.last_alert_time,
                "last_alert_timestamp": self.last_alert_time,
                "consecutive_errors": self.consecutive_errors,
                "total_alerts_sent": self.total_alerts_sent,
                "cooldown_seconds": self.cooldown_seconds,
                "saved_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            temp_file = self.state_file.with_suffix(f"{self.state_file.suffix}.tmp")
            temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temp_file.replace(self.state_file)
            logger.debug("Persisted monitor state to %s", self.state_file)
        except OSError as e:
            logger.warning("Could not persist monitor state to %s: %s", self.state_file, e)

    def _load_state(self) -> None:
        """Load state from JSON file with fault tolerance against corruption."""
        if not self.state_file or not self.state_file.exists():
            return
        try:
            content = self.state_file.read_text(encoding="utf-8")
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError(f"State file payload must be a JSON object, got {type(data).__name__}")
            self.is_currently_available = bool(data.get("is_currently_available", False))
            lat = data.get("last_alert_time", data.get("last_alert_timestamp"))
            self.last_alert_time = float(lat) if lat is not None else None
            self.consecutive_errors = int(data.get("consecutive_errors", 0))
            self.total_alerts_sent = int(data.get("total_alerts_sent", 0))
            logger.info(
                "Loaded monitor state from %s: available=%s, last_alert=%s",
                self.state_file,
                self.is_currently_available,
                self.last_alert_time,
            )
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as e:
            logger.warning(
                "Could not load state from %s (data corrupted or unreadable: %s). Initializing fresh state.",
                self.state_file,
                e,
            )
            self.is_currently_available = False
            self.last_alert_time = None
            self.consecutive_errors = 0
            self.total_alerts_sent = 0

    def to_dict(self) -> Dict[str, Any]:
        """Returns snapshot of current state as a dictionary."""
        return {
            "is_currently_available": self.is_currently_available,
            "last_alert_time": self.last_alert_time,
            "cooldown_seconds": self.cooldown_seconds,
            "consecutive_errors": self.consecutive_errors,
            "total_alerts_sent": self.total_alerts_sent,
            "state_file": str(self.state_file) if self.state_file else None,
        }

    def __repr__(self) -> str:
        return (
            f"AlertStateManager(available={self.is_currently_available}, "
            f"cooldown_seconds={self.cooldown_seconds}, "
            f"last_alert_time={self.last_alert_time}, "
            f"consecutive_errors={self.consecutive_errors})"
        )
