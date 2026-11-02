"""
State machine and alert throttling management for AllAccess ticket monitoring.
Implements edge detection, target-partitioned cooldown throttling, reminder alerts,
reset on unavailable, transient check error immunity, monotonic time tracking,
and robust atomic multi-target state persistence.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Optional, Union, Callable, Dict, Any, Tuple
import unicodedata

from allaccess_monitor.models import AvailabilityStatus, CheckResult

logger = logging.getLogger(__name__)

DEFAULT_TARGET_KEY: str = "__default__"


def normalize_section_name(text: Optional[str]) -> str:
    """Normalize section names stripping accents, diacritics, and irregular whitespace."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(text))
    ascii_bytes = nfkd.encode("ASCII", "ignore")
    ascii_str = ascii_bytes.decode("utf-8")
    return " ".join(ascii_str.split()).lower()


def make_target_key(
    target: Any = None,
    event_url: Optional[str] = None,
    section_name: Optional[str] = None,
) -> str:
    """
    Construct a deterministic canonical target key string.
    Format: '<normalized_url>::<normalized_section>'
    """
    raw_url = event_url or ""
    raw_sec = section_name or ""

    if target is not None:
        if isinstance(target, (tuple, list)) and len(target) >= 2:
            raw_url = target[0]
            raw_sec = target[1]
        elif hasattr(target, "event_url") and hasattr(target, "section_name"):
            raw_url = target.event_url
            raw_sec = target.section_name
        elif hasattr(target, "event_url") and hasattr(target, "target_section"):
            raw_url = target.event_url
            raw_sec = target.target_section
        elif isinstance(target, str):
            if "::" in target:
                parts = target.split("::", 1)
                raw_url = parts[0]
                raw_sec = parts[1]
            elif raw_url:
                raw_sec = target
            else:
                raw_sec = target

    norm_url = str(raw_url).strip().rstrip("/").lower() if raw_url else ""
    norm_sec = normalize_section_name(raw_sec) if raw_sec else ""

    if not norm_url and not norm_sec:
        return DEFAULT_TARGET_KEY

    return f"{norm_url}::{norm_sec}"


@dataclass
class AlertDecision:
    """Decision output produced by AlertStateManager evaluating a check result."""
    should_alert: bool
    is_reminder: bool = False
    reason: str = ""


@dataclass
class TargetState:
    """State record governing a single monitored target."""
    is_currently_available: bool = False
    last_alert_time: Optional[float] = None
    consecutive_errors: int = 0
    total_alerts_sent: int = 0
    last_observed_price: Optional[float] = None
    last_unavailable_time: Optional[float] = None

    @property
    def is_available(self) -> bool:
        return self.is_currently_available

    @property
    def last_alert_timestamp(self) -> Optional[float]:
        return self.last_alert_time

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "is_currently_available": self.is_currently_available,
            "last_alert_time": self.last_alert_time,
            "last_alert_timestamp": self.last_alert_time,
            "consecutive_errors": self.consecutive_errors,
            "total_alerts_sent": self.total_alerts_sent,
        }
        if self.last_observed_price is not None:
            data["last_observed_price"] = self.last_observed_price
        if self.last_unavailable_time is not None:
            data["last_unavailable_time"] = self.last_unavailable_time
        return data


class AlertStateManager:
    """
    Finite State Machine (FSM) governing ticket alert dispatching with target partitioning.

    Key capabilities:
    1. Target Partitioning: Tracks availability and cooldown independently per (event_url, sector).
    2. Edge Detection: Immediate alert when tickets transition from UNAVAILABLE -> AVAILABLE.
    3. Cooldown Throttling: Suppresses duplicate notifications while tickets remain available.
    4. Reminder Alerts: Dispatches a reminder when availability persists beyond cooldown_seconds.
    5. State Reset: Transition to UNAVAILABLE resets state so subsequent availability alerts immediately.
    6. Transient Error Immunity: Check errors (HTTP 5xx, timeouts, WAF) preserve availability state.
    7. Monotonic Time Tracking: Uses time.monotonic() by default to prevent clock skew issues.
    8. Partitioned Persistence: Atomically saves and loads multi-target state to/from JSON.
    9. Price Drop Detection: Re-alerts immediately if price drops on continuously available tickets.
    10. 100% Backward Compatibility: Exposes is_currently_available and last_alert_time properties.
    """

    def __init__(
        self,
        cooldown_seconds: int = 1800,
        state_file: Optional[Union[Path, str]] = None,
        initial_available: bool = False,
        time_func: Optional[Callable[[], float]] = None,
        notify_on_sold_out: bool = False,
        flapping_cooldown_seconds: float = 0.0,
    ) -> None:
        if cooldown_seconds < 0:
            raise ValueError(f"cooldown_seconds cannot be negative, got {cooldown_seconds}")

        self.cooldown_seconds: int = int(cooldown_seconds)
        self.state_file: Optional[Path] = Path(state_file) if state_file else None
        self.time_func: Callable[[], float] = time_func or time.monotonic
        self.notify_on_sold_out: bool = notify_on_sold_out
        self.flapping_cooldown_seconds: float = float(flapping_cooldown_seconds)

        # Multi-target partitioned state registry
        self._targets: Dict[str, TargetState] = {
            DEFAULT_TARGET_KEY: TargetState(is_currently_available=initial_available)
        }
        self._current_target_key: str = DEFAULT_TARGET_KEY

        # Attempt to recover persisted state if file is specified
        if self.state_file:
            self._load_state()

    # --------------------------------------------------------------------------
    # Backward-Compatible Properties Delegating to Primary / Current Target
    # --------------------------------------------------------------------------

    def _get_primary_target_state(self) -> TargetState:
        """Helper returning the current target state or default target state."""
        if self._current_target_key in self._targets:
            return self._targets[self._current_target_key]
        if DEFAULT_TARGET_KEY in self._targets:
            return self._targets[DEFAULT_TARGET_KEY]
        if self._targets:
            return next(iter(self._targets.values()))
        fresh = TargetState()
        self._targets[DEFAULT_TARGET_KEY] = fresh
        return fresh

    @property
    def is_currently_available(self) -> bool:
        """Availability status of current/primary target."""
        return self._get_primary_target_state().is_currently_available

    @is_currently_available.setter
    def is_currently_available(self, val: bool) -> None:
        self._get_primary_target_state().is_currently_available = bool(val)

    @property
    def is_available(self) -> bool:
        """Alias for is_currently_available."""
        return self.is_currently_available

    @property
    def last_alert_time(self) -> Optional[float]:
        """Timestamp of last alert sent for current/primary target."""
        return self._get_primary_target_state().last_alert_time

    @last_alert_time.setter
    def last_alert_time(self, val: Optional[float]) -> None:
        self._get_primary_target_state().last_alert_time = float(val) if val is not None else None

    @property
    def last_alert_timestamp(self) -> Optional[float]:
        """Alias for last_alert_time."""
        return self.last_alert_time

    @property
    def consecutive_errors(self) -> int:
        """Consecutive error count for current/primary target."""
        return self._get_primary_target_state().consecutive_errors

    @consecutive_errors.setter
    def consecutive_errors(self, val: int) -> None:
        self._get_primary_target_state().consecutive_errors = int(val)

    @property
    def total_alerts_sent(self) -> int:
        """Total alerts dispatched across all monitored targets."""
        return sum(ts.total_alerts_sent for ts in self._targets.values())

    @total_alerts_sent.setter
    def total_alerts_sent(self, val: int) -> None:
        self._get_primary_target_state().total_alerts_sent = int(val)

    # --------------------------------------------------------------------------
    # Multi-Target State Access Helpers
    # --------------------------------------------------------------------------

    def get_target_key(
        self,
        target: Any = None,
        event_url: Optional[str] = None,
        section_name: Optional[str] = None,
    ) -> str:
        """Public helper to construct target key."""
        return make_target_key(target=target, event_url=event_url, section_name=section_name)

    def get_target_state(
        self,
        target: Any = None,
        event_url: Optional[str] = None,
        section_name: Optional[str] = None,
    ) -> TargetState:
        """Retrieve or create TargetState for a specific target."""
        key = self.get_target_key(target=target, event_url=event_url, section_name=section_name)
        if key not in self._targets:
            self._targets[key] = TargetState()
        return self._targets[key]

    def is_target_available(
        self,
        target: Any = None,
        event_url: Optional[str] = None,
        section_name: Optional[str] = None,
    ) -> bool:
        """Return True if specific target is currently available."""
        return self.get_target_state(target, event_url, section_name).is_currently_available

    def get_last_alert_time(
        self,
        target: Any = None,
        event_url: Optional[str] = None,
        section_name: Optional[str] = None,
    ) -> Optional[float]:
        """Return last alert timestamp for specific target."""
        return self.get_target_state(target, event_url, section_name).last_alert_time

    # --------------------------------------------------------------------------
    # Core FSM Evaluation
    # --------------------------------------------------------------------------

    def evaluate(
        self,
        result: CheckResult,
        current_time: Optional[float] = None,
        target: Optional[Any] = None,
    ) -> AlertDecision:
        """
        Evaluate availability status of a CheckResult for its target.
        Implements the AlertStateManager contract from PROJECT.md.
        """
        return self.process_check_result(
            result=result,
            current_time=current_time,
            target=target if target is not None else result,
        )

    def process_check_result(
        self,
        is_available: Union[CheckResult, AvailabilityStatus, bool, None] = None,
        current_time: Optional[float] = None,
        is_error: bool = False,
        result: Optional[Union[CheckResult, AvailabilityStatus, bool]] = None,
        target: Optional[Any] = None,
        target_key: Optional[str] = None,
    ) -> AlertDecision:
        """
        Evaluate availability status and decide whether to alert, remind, or suppress.
        Evaluates independently for the target partition.
        """
        target_obj = result if result is not None else is_available
        if target_obj is None:
            raise ValueError("process_check_result requires an availability status or CheckResult")

        # 1. Resolve canonical target partition key
        if target_key:
            effective_key = target_key
        elif target is not None:
            effective_key = self.get_target_key(target)
        else:
            effective_key = DEFAULT_TARGET_KEY

        self._current_target_key = effective_key
        target_state = self._targets.setdefault(effective_key, TargetState())

        # 2. Determine if check result signifies transient error or availability
        check_is_error = is_error
        check_is_avail = False

        if isinstance(target_obj, CheckResult):
            if target_obj.status == AvailabilityStatus.ERROR:
                check_is_error = True
            else:
                check_is_avail = (target_obj.status == AvailabilityStatus.AVAILABLE)
        elif isinstance(target_obj, AvailabilityStatus):
            if target_obj == AvailabilityStatus.ERROR:
                check_is_error = True
            else:
                check_is_avail = (target_obj == AvailabilityStatus.AVAILABLE)
        elif isinstance(target_obj, bool):
            check_is_avail = target_obj
        else:
            if hasattr(target_obj, "status") and getattr(target_obj, "status") == AvailabilityStatus.ERROR:
                check_is_error = True
            elif hasattr(target_obj, "is_available"):
                check_is_avail = bool(getattr(target_obj, "is_available"))
            else:
                check_is_avail = bool(target_obj)

        now = float(current_time) if current_time is not None else self.time_func()

        # Rule 1: Transient Error Immunity (partitioned per target)
        if check_is_error:
            target_state.consecutive_errors += 1
            logger.debug(
                "Transient check error encountered for target '%s' (consecutive=%d). Preserving availability: %s",
                effective_key,
                target_state.consecutive_errors,
                target_state.is_currently_available,
            )
            return AlertDecision(
                should_alert=False,
                is_reminder=False,
                reason=f"Transient check error (consecutive_errors={target_state.consecutive_errors}); availability state preserved.",
            )

        # Successful check: clear error streak for this target
        target_state.consecutive_errors = 0

        # Rule 2: Tickets Detected as Available
        if check_is_avail:
            # Extract effective price for budget ceiling guard
            effective_price: Optional[float] = None
            sec_info = getattr(target_obj, "section_info", None) or getattr(target_obj, "section", None)
            if sec_info is not None:
                tot_p = getattr(sec_info, "total_price", None)
                effective_price = tot_p if tot_p is not None else getattr(sec_info, "price", None)
            elif getattr(target_obj, "price", None) is not None:
                effective_price = getattr(target_obj, "price", None)

            target_max_price = getattr(target, "max_price", None) if target is not None else None
            is_over_budget = (
                target_max_price is not None
                and effective_price is not None
                and effective_price > target_max_price
            )

            if is_over_budget:
                # Tickets are available on-site, but exceed the configured budget ceiling.
                # Mark availability so physical state is accurate, but DO NOT record last_alert_time
                # or increment total_alerts_sent, ensuring subsequent price drops immediately trigger alerts.
                if not target_state.is_currently_available:
                    target_state.is_currently_available = True
                    target_state.last_observed_price = effective_price
                    self._save_state()
                    logger.info(
                        "Tickets available for '%s' but price %.2f exceeds max_price %.2f. Suppressing alert.",
                        effective_key,
                        effective_price,
                        target_max_price,
                    )
                return AlertDecision(
                    should_alert=False,
                    is_reminder=False,
                    reason=f"Tickets available but price ({effective_price}) exceeds max_price ({target_max_price}). Alert suppressed.",
                )

            # Flapping defense (if flapping_cooldown_seconds > 0)
            if (
                self.flapping_cooldown_seconds > 0
                and not target_state.is_currently_available
                and target_state.last_unavailable_time is not None
            ):
                unavail_elapsed = now - target_state.last_unavailable_time
                if 0 <= unavail_elapsed < self.flapping_cooldown_seconds:
                    logger.debug(
                        "Tickets rapidly oscillating (flapping) for '%s' (unavailable for only %.1fs < %.1fs). Throttling alert.",
                        effective_key,
                        unavail_elapsed,
                        self.flapping_cooldown_seconds,
                    )
                    target_state.is_currently_available = True
                    target_state.last_observed_price = effective_price
                    self._save_state()
                    return AlertDecision(
                        should_alert=False,
                        is_reminder=False,
                        reason=f"Rapid status flapping detected ({unavail_elapsed:.1f}s < {self.flapping_cooldown_seconds:.1f}s). Alert dampened.",
                    )

            # Tickets are within budget ceiling!
            if not target_state.is_currently_available or target_state.last_alert_time is None:
                # Transition: UNAVAILABLE -> AVAILABLE, or first detection within budget ceiling!
                target_state.is_currently_available = True
                target_state.last_alert_time = now
                target_state.last_observed_price = effective_price
                target_state.last_unavailable_time = None
                target_state.total_alerts_sent += 1
                self._save_state()
                logger.info(
                    "Ticket availability edge detected for '%s': Alerting immediately (price: %s).",
                    effective_key,
                    f"{effective_price:.2f}" if effective_price is not None else "N/A",
                )
                return AlertDecision(
                    should_alert=True,
                    is_reminder=False,
                    reason="Initial ticket availability detected (edge transition)!",
                )
            else:
                # Steady State: AVAILABLE -> AVAILABLE
                # Check for Price Drop re-alerting
                if (
                    effective_price is not None
                    and target_state.last_observed_price is not None
                    and effective_price < target_state.last_observed_price
                ):
                    prev_p = target_state.last_observed_price
                    target_state.last_alert_time = now
                    target_state.last_observed_price = effective_price
                    target_state.total_alerts_sent += 1
                    self._save_state()
                    logger.info(
                        "Price drop detected for '%s' (%.2f -> %.2f). Re-alerting immediately.",
                        effective_key,
                        prev_p,
                        effective_price,
                    )
                    return AlertDecision(
                        should_alert=True,
                        is_reminder=False,
                        reason=f"Price drop detected from {prev_p:.2f} to {effective_price:.2f}!",
                    )

                if effective_price is not None:
                    target_state.last_observed_price = effective_price

                last_alert = target_state.last_alert_time
                elapsed = now - last_alert

                # Clock skew detection: system time jumped backwards
                if elapsed < 0:
                    logger.warning(
                        "Clock skew detected for '%s': current_time (%.2f) is before last_alert_time (%.2f). "
                        "Adjusting reference timestamp without alerting.",
                        effective_key,
                        now,
                        last_alert,
                    )
                    target_state.last_alert_time = now
                    self._save_state()
                    return AlertDecision(
                        should_alert=False,
                        is_reminder=False,
                        reason="Clock skew detected; reference timestamp adjusted.",
                    )

                # Check cooldown threshold
                if elapsed >= self.cooldown_seconds:
                    # Cooldown expired: send reminder alert
                    target_state.last_alert_time = now
                    target_state.total_alerts_sent += 1
                    self._save_state()
                    logger.info(
                        "Tickets remain available for '%s' and cooldown expired (elapsed=%.1fs >= %ds). Dispatching reminder.",
                        effective_key,
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
                        "Tickets continuously available for '%s'. Alert throttled under cooldown (%ds remaining).",
                        effective_key,
                        remaining,
                    )
                    return AlertDecision(
                        should_alert=False,
                        is_reminder=False,
                        reason=f"Tickets continuously available. Alert throttled ({remaining}s remaining in cooldown).",
                    )

        # Rule 3: Tickets Detected as Unavailable
        else:
            if target_state.is_currently_available:
                # Transition: AVAILABLE -> UNAVAILABLE (Tickets Sold Out / Vanished)
                logger.info(
                    "Ticket availability transition for '%s': AVAILABLE -> UNAVAILABLE (State reset).",
                    effective_key,
                )
                target_state.is_currently_available = False
                target_state.last_alert_time = None
                target_state.last_observed_price = None
                target_state.last_unavailable_time = now
                self._save_state()
                return AlertDecision(
                    should_alert=False,
                    is_reminder=False,
                    reason="Tickets are unavailable (sold out). State reset.",
                )
            target_state.last_unavailable_time = now
            return AlertDecision(
                should_alert=False,
                is_reminder=False,
                reason="Tickets are unavailable.",
            )

    def reset(self, target: Any = None) -> None:
        """
        Explicitly reset state to UNAVAILABLE and clear alert history.
        If target is specified, resets only that partition; otherwise resets all partitions.
        """
        if target is not None:
            key = self.get_target_key(target)
            if key in self._targets:
                self._targets[key] = TargetState()
        else:
            self._targets = {DEFAULT_TARGET_KEY: TargetState()}
            self._current_target_key = DEFAULT_TARGET_KEY

        self._save_state()
        logger.info("AlertStateManager state explicitly reset (target=%s).", target)

    # --------------------------------------------------------------------------
    # Persistence Layer
    # --------------------------------------------------------------------------

    def _save_state(self) -> None:
        """Persist multi-target state to JSON file using atomic write."""
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            primary = self._get_primary_target_state()
            data = {
                "version": 2,
                "is_currently_available": primary.is_currently_available,
                "last_alert_time": primary.last_alert_time,
                "last_alert_timestamp": primary.last_alert_time,
                "consecutive_errors": primary.consecutive_errors,
                "total_alerts_sent": self.total_alerts_sent,
                "cooldown_seconds": self.cooldown_seconds,
                "saved_at_utc": datetime.now(timezone.utc).isoformat(),
                "targets": {
                    k: ts.to_dict() for k, ts in self._targets.items()
                },
            }
            temp_file = self.state_file.with_suffix(f"{self.state_file.suffix}.tmp")
            temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temp_file.replace(self.state_file)
            logger.debug("Persisted monitor state to %s", self.state_file)
        except OSError as e:
            logger.warning("Could not persist monitor state to %s: %s", self.state_file, e)

    def _load_state(self) -> None:
        """Load state from JSON file with backward-compatible migration and fault tolerance."""
        if not self.state_file or not self.state_file.exists():
            return
        try:
            content = self.state_file.read_text(encoding="utf-8")
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError(f"State file payload must be a JSON object, got {type(data).__name__}")

            # Top-level legacy fields with strict type coercion
            top_avail = bool(data.get("is_currently_available", False))
            lat = data.get("last_alert_time", data.get("last_alert_timestamp"))
            top_lat = float(lat) if lat is not None else None
            top_errors = int(data.get("consecutive_errors", 0))
            top_alerts = int(data.get("total_alerts_sent", 0))

            raw_targets = data.get("targets")
            if isinstance(raw_targets, dict) and raw_targets:
                self._targets.clear()
                for key, t_data in raw_targets.items():
                    if not isinstance(t_data, dict):
                        continue
                    t_avail = bool(t_data.get("is_currently_available", False))
                    t_lat_raw = t_data.get("last_alert_time", t_data.get("last_alert_timestamp"))
                    t_lat = float(t_lat_raw) if t_lat_raw is not None else None
                    t_errors = int(t_data.get("consecutive_errors", 0))
                    t_alerts = int(t_data.get("total_alerts_sent", 0))
                    t_lop_raw = t_data.get("last_observed_price")
                    t_lop = float(t_lop_raw) if t_lop_raw is not None else None
                    t_lut_raw = t_data.get("last_unavailable_time")
                    t_lut = float(t_lut_raw) if t_lut_raw is not None else None
                    self._targets[str(key)] = TargetState(
                        is_currently_available=t_avail,
                        last_alert_time=t_lat,
                        consecutive_errors=t_errors,
                        total_alerts_sent=t_alerts,
                        last_observed_price=t_lop,
                        last_unavailable_time=t_lut,
                    )

                if DEFAULT_TARGET_KEY not in self._targets:
                    self._targets[DEFAULT_TARGET_KEY] = TargetState(
                        is_currently_available=top_avail,
                        last_alert_time=top_lat,
                        consecutive_errors=top_errors,
                        total_alerts_sent=top_alerts,
                    )
            else:
                # Legacy single-target format
                self._targets = {
                    DEFAULT_TARGET_KEY: TargetState(
                        is_currently_available=top_avail,
                        last_alert_time=top_lat,
                        consecutive_errors=top_errors,
                        total_alerts_sent=top_alerts,
                    )
                }

            self._current_target_key = (
                DEFAULT_TARGET_KEY if DEFAULT_TARGET_KEY in self._targets else next(iter(self._targets.keys()))
            )

            logger.info(
                "Loaded monitor state from %s: targets=%d, available=%s, last_alert=%s",
                self.state_file,
                len(self._targets),
                self.is_currently_available,
                self.last_alert_time,
            )
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as e:
            logger.warning(
                "Could not load state from %s (data corrupted or unreadable: %s). Initializing fresh state.",
                self.state_file,
                e,
            )
            self._targets = {DEFAULT_TARGET_KEY: TargetState()}
            self._current_target_key = DEFAULT_TARGET_KEY

    def to_dict(self) -> Dict[str, Any]:
        """Returns snapshot of current state as a dictionary."""
        primary = self._get_primary_target_state()
        return {
            "is_currently_available": primary.is_currently_available,
            "last_alert_time": primary.last_alert_time,
            "cooldown_seconds": self.cooldown_seconds,
            "consecutive_errors": primary.consecutive_errors,
            "total_alerts_sent": self.total_alerts_sent,
            "state_file": str(self.state_file) if self.state_file else None,
            "targets": {k: ts.to_dict() for k, ts in self._targets.items()},
        }

    def __repr__(self) -> str:
        primary = self._get_primary_target_state()
        return (
            f"AlertStateManager(targets={len(self._targets)}, "
            f"current_target='{self._current_target_key}', "
            f"available={primary.is_currently_available}, "
            f"cooldown_seconds={self.cooldown_seconds}, "
            f"last_alert_time={primary.last_alert_time}, "
            f"consecutive_errors={primary.consecutive_errors})"
        )
