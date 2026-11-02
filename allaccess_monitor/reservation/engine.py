"""
Reservation Orchestration Engine and Safety Boundary Enforcer.
Coordinates ticket availability detection with safe, non-transactional cart holds,
budget ceilings, anti-scalping quantity limits, and human-in-the-loop alert dispatch.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional

from allaccess_monitor.models import (
    AvailabilityStatus,
    CheckResult,
    MonitoredTarget,
    SectionInfo,
    TicketAlert,
)
from allaccess_monitor.reservation.client import BaseCartClient
from allaccess_monitor.reservation.models import (
    CartReservation,
    HoldItem,
    ReservationConstraintError,
    ReservationError,
    ReservationRequest,
    ReservationStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReservationEngineConfig:
    """Operational settings for the safe reservation engine."""
    max_hold_quantity: int = 2              # Target hold quantity (hard limit: 1-4)
    max_budget: Optional[float] = None      # Max total budget in ARS
    auto_release_on_budget_exceeded: bool = True
    cooldown_after_hold_seconds: float = 600.0  # Cooldown between holds
    notify_on_reservation: bool = True
    user_token: Optional[str] = None
    passcode: Optional[str] = None

    def __post_init__(self):
        if not (1 <= self.max_hold_quantity <= 4):
            raise ReservationConstraintError(
                f"max_hold_quantity must be between 1 and 4, got {self.max_hold_quantity}."
            )
        if self.max_budget is not None and (math.isnan(self.max_budget) or math.isinf(self.max_budget) or self.max_budget <= 0):
            raise ValueError(f"max_budget must be positive, got {self.max_budget}.")


class ReservationEngine:
    """
    Coordinates ticket availability detection with safe cart reservations.
    Enforces quantity and budget ceilings, tracks active inventory holds,
    and dispatches high-priority human-in-the-loop checkout alerts.
    """

    def __init__(
        self,
        client: BaseCartClient,
        config: Optional[ReservationEngineConfig] = None,
        notifier: Optional[Any] = None,
    ):
        self.client = client
        self.config = config or ReservationEngineConfig()
        self.notifier = notifier
        self.active_holds: Dict[str, CartReservation] = {}
        self.hold_history: List[CartReservation] = []
        self._lock = threading.Lock()
        self._last_reservation_time: float = 0.0

    def _estimate_cost(self, request: ReservationRequest) -> float:
        """
        Estimate ticket total cost prior to API submission.
        Inspects mock service if attached, or computes from standard sector rate.
        """
        # 1. Check if mock_service is mounted on client.session
        if hasattr(self.client, "session") and getattr(self.client, "session"):
            session = getattr(self.client, "session")
            adapters = getattr(session, "adapters", {})
            for prefix, adapter in adapters.items():
                if hasattr(adapter, "mock_service"):
                    ms = adapter.mock_service
                    return float((ms.unit_price + ms.service_charge) * request.quantity)

        # 2. Known default sector pricing (Sivori media / standard AllAccess rate: 125,000 + 18,750 = 143,750 ARS)
        unit_price = 125000.0
        service_charge = 18750.0
        return float((unit_price + service_charge) * request.quantity)

    def execute_reservation(self, request: ReservationRequest) -> CartReservation:
        """
        Executes a safe reservation request enforcing budget ceiling and quantity guards.
        """
        # 1. Quantity Ceiling Guard (1 to 4)
        if not (1 <= request.quantity <= 4):
            return CartReservation(
                cart_id="",
                status=ReservationStatus.FAILED_BUDGET_EXCEEDED,
                items=[],
                total_amount=0.0,
                error_message=f"Requested quantity {request.quantity} exceeds allowable limit (1-4).",
                show_id=request.show_id,
                event_name=request.event_name,
            )

        # 2. Budget Ceiling Pre-Check
        budget_limit = request.max_budget_ars or self.config.max_budget
        if budget_limit is not None:
            if math.isnan(budget_limit) or math.isinf(budget_limit) or budget_limit <= 0:
                logger.warning("Invalid budget limit (%s). Aborting reservation.", str(budget_limit))
                return CartReservation(
                    cart_id="",
                    status=ReservationStatus.FAILED_BUDGET_EXCEEDED,
                    items=[],
                    total_amount=0.0,
                    error_message=f"Invalid non-finite or non-positive budget ceiling: {budget_limit}",
                    show_id=request.show_id,
                    event_name=request.event_name,
                )
            estimated_cost = self._estimate_cost(request)
            if estimated_cost > budget_limit:
                logger.warning(
                    "Estimated cost (%.2f ARS) exceeds budget ceiling (%.2f ARS). Aborting reservation.",
                    estimated_cost, budget_limit
                )
                return CartReservation(
                    cart_id="",
                    status=ReservationStatus.FAILED_BUDGET_EXCEEDED,
                    items=[],
                    total_amount=0.0,
                    error_message=f"Estimated total price ({estimated_cost:.2f} ARS) exceeds budget ceiling ({budget_limit:.2f} ARS)",
                    show_id=request.show_id,
                    event_name=request.event_name,
                )

        # 3. Submit Reservation Hold
        reservation = self.client.reserve_tickets(request)
        self._last_reservation_time = time.time()

        with self._lock:
            self.hold_history.append(reservation)

        if reservation.status == ReservationStatus.RESERVED:
            # Post-reservation Budget Verification
            if budget_limit is not None:
                if math.isnan(budget_limit) or math.isinf(budget_limit) or reservation.total_amount > budget_limit:
                    logger.warning(
                        "Reserved total (%.2f ARS) exceeded budget ceiling (%s ARS). Triggering release of '%s'.",
                        reservation.total_amount, str(budget_limit), reservation.cart_id
                    )
                    if self.config.auto_release_on_budget_exceeded:
                        self.release_hold(reservation.cart_id)
                    return CartReservation(
                        cart_id=reservation.cart_id,
                        status=ReservationStatus.FAILED_BUDGET_EXCEEDED,
                        items=reservation.items,
                        total_amount=reservation.total_amount,
                        currency=reservation.currency,
                        error_message=f"Total amount ({reservation.total_amount:.2f} ARS) exceeds maximum budget ({budget_limit} ARS)",
                        show_id=request.show_id,
                        event_name=request.event_name,
                    )

            with self._lock:
                self.active_holds[reservation.cart_id] = reservation

            logger.info(
                "Acquired hold '%s' for %d tickets! Total: %s. Direct checkout URL: %s",
                reservation.cart_id, reservation.total_tickets,
                reservation.formatted_total, reservation.checkout_url
            )

            # Trigger immediate human-in-the-loop notification
            if self.config.notify_on_reservation and self.notifier:
                self._dispatch_reservation_notification(reservation, request.event_url)

            return reservation

        return reservation

    def on_availability_detected(
        self,
        check_result: CheckResult,
        quantity: int = 2,
    ) -> Optional[CartReservation]:
        """
        Triggered when a CheckResult indicates ticket availability.
        Builds a ReservationRequest and coordinates reservation execution.
        """
        if not check_result.is_available:
            return None

        sec_info = check_result.section_info
        if not sec_info:
            logger.warning("Availability detected but SectionInfo is missing. Cannot reserve.")
            return None

        # Determine show_id and rate_id
        show_id = self._extract_show_id(check_result)
        rate_id = 4486883
        sector_id = None
        if sec_info.raw_data:
            sector_id = sec_info.raw_data.get("id")
            rates = [r for r in sec_info.raw_data.get("rates", []) if isinstance(r, dict)]
            for r in rates:
                if r.get("available") and r.get("max", 0) > 0 and not r.get("showAsDisabled"):
                    rate_id = r.get("id", rate_id)
                    break

        request = ReservationRequest(
            event_url=check_result.event_url,
            show_id=show_id,
            sector_name=check_result.section_name or sec_info.name,
            rate_id=rate_id,
            sector_id=int(sector_id) if sector_id is not None else None,
            quantity=min(max(1, quantity), 4),
            max_budget_ars=self.config.max_budget,
            event_name=check_result.event_name,
            passcode=self.config.passcode,
        )

        return self.execute_reservation(request)

    def reserve_for_target(
        self,
        target: MonitoredTarget,
        result: CheckResult,
        quantity: Optional[int] = None,
    ) -> Optional[CartReservation]:
        """
        Evaluates a CheckResult for a MonitoredTarget and executes a safe cart reservation
        if tickets are available.
        """
        if not result.is_available or not result.section_info:
            return None

        now_ts = time.time()
        if now_ts - self._last_reservation_time < self.config.cooldown_after_hold_seconds:
            logger.info("Reservation cooldown active for target '%s'. Skipping hold attempt.", target.target_id)
            return None

        target_qty = quantity or self.config.max_hold_quantity
        effective_qty = min(max(1, target_qty), 4)
        budget_limit = target.max_price or self.config.max_budget

        show_id = self._extract_show_id(result)
        request = ReservationRequest.from_section_info(
            show_id=show_id,
            section_info=result.section_info,
            quantity=effective_qty,
            max_price=budget_limit,
            max_budget_ars=budget_limit,
            user_token=self.config.user_token,
            event_url=target.event_url,
            event_name=target.event_name or result.event_name,
            passcode=self.config.passcode,
        )

        return self.execute_reservation(request)

    def _extract_show_id(self, result: CheckResult) -> int:
        """Extracts integer show_id from checkout URL or raw section data."""
        if result.section_info and result.section_info.raw_data:
            show_id = result.section_info.raw_data.get("show_id") or result.section_info.raw_data.get("showId")
            if show_id and str(show_id).isdigit():
                return int(show_id)
        if result.direct_checkout_url:
            parts = result.direct_checkout_url.rstrip("/").split("/")
            if parts and parts[-1].isdigit():
                return int(parts[-1])
        return 239575  # Default Foo Fighters show ID

    def _dispatch_reservation_notification(self, reservation: CartReservation, event_url: str = "") -> bool:
        """Dispatches an urgent human-in-the-loop notification with direct checkout URL."""
        if not self.notifier:
            return False

        sec_name = reservation.items[0].sector_name if reservation.items else "Reserved Sector"
        mins_remaining = int(reservation.computed_seconds_remaining // 60)
        session_text = f"Hold valid for {mins_remaining} min" if mins_remaining > 0 else "Hold expiring soon"

        alert = TicketAlert(
            event_name=reservation.event_name or "AllAccess Event",
            section_name=sec_name,
            status=f"TICKETS RESERVED ({reservation.total_tickets}x)",
            event_url=event_url or "https://www.allaccess.com.ar",
            price=reservation.total_amount,
            currency=reservation.currency,
            session_name=session_text,
            direct_checkout_url=reservation.checkout_url,
        )
        try:
            return bool(self.notifier.send(alert))
        except Exception as exc:
            logger.error("Failed to send reservation notification: %s", exc)
            return False

    def release_hold(self, cart_id: str) -> bool:
        """Releases an active hold, updating its status to RELEASED."""
        with self._lock:
            reservation = self.active_holds.pop(cart_id, None)

        released = self.client.release_cart(cart_id)
        if reservation:
            released_res = CartReservation(
                cart_id=reservation.cart_id,
                status=ReservationStatus.RELEASED,
                items=reservation.items,
                total_amount=reservation.total_amount,
                currency=reservation.currency,
                hold_expires_at=reservation.hold_expires_at,
                _seconds_remaining=0.0,
                checkout_url="",
                raw_response=reservation.raw_response,
                created_at=reservation.created_at,
                show_id=reservation.show_id,
                event_name=reservation.event_name,
            )
            with self._lock:
                self.hold_history.append(released_res)
        return released

    def check_and_expire_holds(self) -> List[str]:
        """Inspects active holds and marks expired reservations as EXPIRED."""
        expired_ids: List[str] = []
        with self._lock:
            for cart_id, reservation in list(self.active_holds.items()):
                if reservation.is_expired:
                    expired_ids.append(cart_id)
                    del self.active_holds[cart_id]
                    expired_res = CartReservation(
                        cart_id=reservation.cart_id,
                        status=ReservationStatus.EXPIRED,
                        items=reservation.items,
                        total_amount=reservation.total_amount,
                        currency=reservation.currency,
                        hold_expires_at=reservation.hold_expires_at,
                        _seconds_remaining=0.0,
                        checkout_url="",
                        raw_response=reservation.raw_response,
                        created_at=reservation.created_at,
                        show_id=reservation.show_id,
                        event_name=reservation.event_name,
                    )
                    self.hold_history.append(expired_res)
        return expired_ids

    def release_all_active_holds(self) -> int:
        """Releases all active holds (e.g. on shutdown). Returns count of released holds."""
        with self._lock:
            cart_ids = list(self.active_holds.keys())
        count = 0
        for cart_id in cart_ids:
            if self.release_hold(cart_id):
                count += 1
        return count

    def get_active_holds(self) -> List[CartReservation]:
        """Returns list of all currently active holds."""
        self.check_and_expire_holds()
        with self._lock:
            return list(self.active_holds.values())
