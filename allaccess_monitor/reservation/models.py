"""
Domain models, status enums, and safety boundaries for the AllAccess Reservation Subsystem.
Strictly non-transactional: enforces quantity limits, budget ceilings, and zero payment processing.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
import re
from typing import Any, Dict, List, Optional, Union

from allaccess_monitor.models import SectionInfo


PROHIBITED_PAYMENT_KEYWORDS = frozenset({
    "card", "credit_card", "debit_card", "card_number", "pan", "account_number",
    "cvv", "cvc", "cid", "security_code", "verification_code", "exp_month", "exp_year",
    "expiry", "expiration_date", "pin", "payment_token", "payment_method", "billing_address",
    "decidir_token", "mercado_pago_token", "stripe_token", "cbu", "alias_cbu", "line_id"
})


class ReservationError(Exception):
    """Base exception for all reservation operations."""
    pass


class NonTransactionalSafetyViolation(ReservationError):
    """Raised when payment credentials or transaction fields are introduced."""
    pass


class ReservationConstraintError(ReservationError, ValueError):
    """Raised when quantity ceilings or budget constraints are violated."""
    pass


class ReservationAPIError(ReservationError):
    """Base exception for HTTP errors returned by AllAccess/Crowder."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class SoldOutError(ReservationAPIError):
    """Raised when target sector/rate inventory is exhausted (HTTP 409)."""
    pass


class RateLimitError(ReservationAPIError):
    """Raised when rate limits are triggered (HTTP 429)."""
    def __init__(self, message: str, retry_after: Optional[int] = None, **kwargs):
        super().__init__(message, status_code=429, **kwargs)
        self.retry_after = retry_after


class ChallengeError(ReservationAPIError):
    """Raised when AWS WAF or Queue-It challenges intercept the request (HTTP 403 / 503)."""
    pass


class AuthenticationRequiredError(ReservationAPIError):
    """Raised when user session is unauthenticated or token is expired (HTTP 401)."""
    pass


class ReservationStatus(str, Enum):
    """Lifecycle status of a cart reservation hold."""
    PENDING = "PENDING"
    RESERVED = "RESERVED"
    EXPIRED = "EXPIRED"
    FAILED_SOLD_OUT = "FAILED_SOLD_OUT"
    FAILED_RATE_LIMITED = "FAILED_RATE_LIMITED"
    FAILED_CHALLENGE = "FAILED_CHALLENGE"
    FAILED_BUDGET_EXCEEDED = "FAILED_BUDGET_EXCEEDED"
    FAILED_AUTH = "FAILED_AUTH"
    RELEASED = "RELEASED"

    @property
    def is_success(self) -> bool:
        """True if reservation hold was successfully acquired."""
        return self == ReservationStatus.RESERVED

    @property
    def is_successful(self) -> bool:
        """Alias for is_success."""
        return self.is_success

    @property
    def is_terminal(self) -> bool:
        """True if status represents a final, non-actionable state."""
        return self in (
            ReservationStatus.EXPIRED,
            ReservationStatus.FAILED_SOLD_OUT,
            ReservationStatus.FAILED_RATE_LIMITED,
            ReservationStatus.FAILED_CHALLENGE,
            ReservationStatus.FAILED_BUDGET_EXCEEDED,
            ReservationStatus.RELEASED,
        )

    @property
    def is_failure(self) -> bool:
        """True if status represents an operational or network failure."""
        return self in (
            ReservationStatus.FAILED_SOLD_OUT,
            ReservationStatus.FAILED_RATE_LIMITED,
            ReservationStatus.FAILED_CHALLENGE,
            ReservationStatus.FAILED_BUDGET_EXCEEDED,
            ReservationStatus.FAILED_AUTH,
        )


@dataclass(frozen=True)
class HoldItem:
    """Individual ticket line item held in a cart reservation."""
    sector_name: str = ""
    rate_id: Optional[int] = None
    rate_name: str = ""
    quantity: int = 1
    price: float = 0.0                  # Base unit price in ARS
    fee: float = 0.0                    # Service charge per unit in ARS
    subtotal: float = 0.0               # Total line price: (price + fee) * quantity
    sector_id: Optional[int] = None     # Crowder sector ID
    ticket_id: Optional[int] = None     # Crowder ticket ID
    currency: str = "ARS"

    def __post_init__(self):
        if math.isnan(self.price) or math.isinf(self.price) or self.price < 0:
            raise ValueError(f"HoldItem 'price' must be non-negative and finite, got {self.price}.")
        if math.isnan(self.fee) or math.isinf(self.fee) or self.fee < 0:
            raise ValueError(f"HoldItem 'fee' must be non-negative and finite, got {self.fee}.")
        if self.quantity < 1:
            raise ValueError(f"HoldItem 'quantity' must be >= 1, got {self.quantity}.")
        expected_subtotal = (self.price + self.fee) * self.quantity
        if self.subtotal == 0.0 or abs(self.subtotal - expected_subtotal) > 0.05:
            object.__setattr__(self, "subtotal", expected_subtotal)

    @property
    def unit_total(self) -> float:
        """Combined unit price plus service charge."""
        return self.price + self.fee

    @property
    def formatted_unit_price(self) -> str:
        """Human-readable formatted unit price."""
        unit = self.unit_total
        p_str = f"${unit:,.0f}" if unit.is_integer() else f"${unit:,.2f}"
        return f"{p_str} {self.currency}"

    @property
    def formatted_subtotal(self) -> str:
        """Human-readable subtotal with breakdown."""
        tot_str = f"${self.subtotal:,.0f}" if self.subtotal.is_integer() else f"${self.subtotal:,.2f}"
        if self.quantity > 1:
            return f"{tot_str} {self.currency} ({self.quantity}x {self.formatted_unit_price})"
        return f"{tot_str} {self.currency}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sector_name": self.sector_name,
            "rate_id": self.rate_id,
            "rate_name": self.rate_name,
            "quantity": self.quantity,
            "price": self.price,
            "fee": self.fee,
            "subtotal": self.subtotal,
            "sector_id": self.sector_id,
            "ticket_id": self.ticket_id,
            "currency": self.currency,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HoldItem":
        price = float(data.get("price") or data.get("unitPrice") or 0.0)
        fee = float(data.get("fee") or data.get("serviceCharge") or 0.0)
        qty = int(data.get("quantity") or 1)
        subtotal = float(data.get("subtotal") or ((price + fee) * qty))
        return cls(
            sector_name=str(data.get("sector_name") or data.get("sectorName") or ""),
            rate_id=int(data["rate_id"]) if data.get("rate_id") is not None else (int(data["rateId"]) if data.get("rateId") is not None else None),
            rate_name=str(data.get("rate_name") or data.get("rateName") or ""),
            quantity=qty,
            price=price,
            fee=fee,
            subtotal=subtotal,
            sector_id=int(data["sector_id"]) if data.get("sector_id") is not None else (int(data["sectorId"]) if data.get("sectorId") is not None else None),
            ticket_id=int(data["ticket_id"]) if data.get("ticket_id") is not None else (int(data["ticketId"]) if data.get("ticketId") is not None else None),
            currency=str(data.get("currency") or "ARS"),
        )


@dataclass(frozen=True)
class ReservationRequest:
    """Request parameters for acquiring a temporary inventory hold in AllAccess cart."""
    event_url: str = ""
    show_id: int = 239575
    sector_name: str = ""
    rate_id: int = 0
    sector_id: Optional[int] = None
    quantity: int = 2
    max_price: Optional[float] = None
    max_budget_ars: Optional[float] = None
    user_token: Optional[str] = None
    event_name: Optional[str] = None
    rate_name: Optional[str] = None
    passcode: Optional[str] = None
    session_id: Optional[str] = None
    csrf_token: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        # 0. Budget Ceiling Validation (for both max_budget_ars and max_price)
        for budget_attr, budget_val in [("max_budget_ars", self.max_budget_ars), ("max_price", self.max_price)]:
            if budget_val is not None:
                if math.isnan(budget_val) or math.isinf(budget_val) or budget_val <= 0:
                    raise ValueError(f"ReservationRequest '{budget_attr}' must be a positive number, got {budget_val}.")

        # Synchronize max_price and max_budget_ars
        budget = self.max_budget_ars if self.max_budget_ars is not None else self.max_price
        if budget is not None:
            object.__setattr__(self, "max_price", budget)
            object.__setattr__(self, "max_budget_ars", budget)

        # 1. Non-Transactional Safety Boundary Check
        self._assert_non_transactional_safety()

        # 2. Strict ID validations
        if not isinstance(self.show_id, int) or self.show_id <= 0:
            raise ValueError(f"ReservationRequest 'show_id' must be a positive int, got {self.show_id}.")
        if self.sector_id is not None and (not isinstance(self.sector_id, int) or self.sector_id <= 0):
            raise ValueError(f"ReservationRequest 'sector_id' must be a positive int, got {self.sector_id}.")
        if self.rate_id is not None and (not isinstance(self.rate_id, int) or self.rate_id < 0):
            raise ValueError(f"ReservationRequest 'rate_id' must be a non-negative int, got {self.rate_id}.")

        # 3. Anti-Scalping Quantity Ceiling (1 to 4 tickets per hold)
        if not (1 <= self.quantity <= 4):
            raise ReservationConstraintError(
                f"Reservation quantity must be between 1 and 4 tickets per hold, requested {self.quantity}."
            )

    def _assert_non_transactional_safety(self) -> None:
        """Scan request attributes for any prohibited financial/payment fields."""
        for field_name, value in self.__dict__.items():
            field_lower = field_name.lower()
            for prohibited in PROHIBITED_PAYMENT_KEYWORDS:
                if prohibited in field_lower:
                    raise NonTransactionalSafetyViolation(
                        f"Non-transactional safety boundary violation: Attribute '{field_name}' "
                        f"matches prohibited payment keyword '{prohibited}'."
                    )
            if isinstance(value, str):
                val_lower = value.lower()
                # Check for standard 13-19 digit PAN card numbers
                if re.search(r'\b(?:\d[ -]*?){13,19}\b', val_lower):
                    raise NonTransactionalSafetyViolation(
                        f"Non-transactional safety boundary violation: Attribute '{field_name}' "
                        f"contains detected card number pattern."
                    )

    def to_api_payload(self) -> Dict[str, Any]:
        """Convert to Crowder /api/cart JSON request payload."""
        payload: Dict[str, Any] = {
            "showId": self.show_id,
            "rateId": self.rate_id,
            "quantity": self.quantity,
        }
        if self.sector_id is not None:
            payload["sectorId"] = self.sector_id
        if self.sector_name:
            payload["sectorName"] = self.sector_name
        if self.passcode:
            payload["passcode"] = self.passcode
        return payload

    @classmethod
    def from_section_info(
        cls,
        show_id: int,
        section_info: SectionInfo,
        quantity: int = 2,
        max_price: Optional[float] = None,
        max_budget_ars: Optional[float] = None,
        user_token: Optional[str] = None,
        event_url: str = "",
        event_name: Optional[str] = None,
        passcode: Optional[str] = None,
    ) -> "ReservationRequest":
        """
        Factory to construct a ReservationRequest by extracting sectorId and rateId
        from SectionInfo.raw_data (parsed Crowder sector dict).
        """
        raw_data = section_info.raw_data or {}
        sector_id = raw_data.get("id")
        rates = [r for r in raw_data.get("rates", []) if isinstance(r, dict)]

        # Select best available rate
        selected_rate = None
        for r in rates:
            if r.get("available") and (r.get("max", 0) > 0) and not r.get("showAsDisabled"):
                selected_rate = r
                break
        if not selected_rate and rates:
            selected_rate = rates[0]

        rate_id = selected_rate.get("id") if selected_rate else None
        rate_name = selected_rate.get("name") if selected_rate else section_info.name

        sec_id_int = int(sector_id) if sector_id is not None else None
        rate_id_int = int(rate_id) if rate_id is not None else 0

        budget = max_budget_ars if max_budget_ars is not None else max_price

        return cls(
            event_url=event_url or (section_info.direct_checkout_url or ""),
            show_id=show_id,
            sector_name=section_info.name,
            rate_id=rate_id_int,
            sector_id=sec_id_int,
            quantity=quantity,
            max_price=budget,
            max_budget_ars=budget,
            user_token=user_token,
            event_name=event_name,
            rate_name=rate_name,
            passcode=passcode,
        )


@dataclass(frozen=True)
class CartReservation:
    """Represents an inventory reservation hold in an AllAccess shopping cart."""
    cart_id: str = ""
    status: ReservationStatus = ReservationStatus.RESERVED
    items: List[HoldItem] = field(default_factory=list)
    total_amount: float = 0.0
    currency: str = "ARS"
    hold_expires_at: Optional[datetime] = None
    _seconds_remaining: Optional[float] = field(default=None, repr=False)
    hold_duration_seconds: int = 600
    checkout_url: str = ""
    raw_response: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    show_id: Optional[int] = None
    event_name: Optional[str] = None
    error_message: Optional[str] = None
    retry_after: Optional[int] = None
    reservation_id: str = ""

    def __post_init__(self):
        if math.isnan(self.total_amount) or math.isinf(self.total_amount) or self.total_amount < 0:
            raise ValueError(f"CartReservation 'total_amount' must be non-negative and finite, got {self.total_amount}.")
        # Keep cart_id and reservation_id synchronized
        if self.cart_id and not self.reservation_id:
            object.__setattr__(self, "reservation_id", self.cart_id)
        elif self.reservation_id and not self.cart_id:
            object.__setattr__(self, "cart_id", self.reservation_id)

    @property
    def seconds_remaining(self) -> int:
        """Dynamically calculated integer seconds remaining until hold expiration."""
        if self.hold_expires_at is not None:
            now = datetime.now(timezone.utc)
            delta = (self.hold_expires_at - now).total_seconds()
            return max(0, int(delta))
        if self._seconds_remaining is not None:
            return max(0, int(self._seconds_remaining))
        return 0

    @property
    def computed_seconds_remaining(self) -> float:
        """Dynamically calculated float seconds remaining until hold expiration."""
        if self.hold_expires_at is not None:
            now = datetime.now(timezone.utc)
            delta = (self.hold_expires_at - now).total_seconds()
            return max(0.0, delta)
        if self._seconds_remaining is not None:
            return max(0.0, float(self._seconds_remaining))
        return 0.0

    @property
    def is_expired(self) -> bool:
        """True if the hold expiration window has passed."""
        if self.status == ReservationStatus.EXPIRED:
            return True
        if self.hold_expires_at is not None:
            return datetime.now(timezone.utc) >= self.hold_expires_at
        return False

    @property
    def is_active(self) -> bool:
        """True if the reservation hold is active, valid, and not expired."""
        return self.status == ReservationStatus.RESERVED and not self.is_expired

    @property
    def formatted_total(self) -> str:
        """Human-readable formatted total amount."""
        tot = self.total_amount
        t_str = f"${tot:,.0f}" if tot.is_integer() else f"${tot:,.2f}"
        return f"{t_str} {self.currency}"

    @property
    def total_tickets(self) -> int:
        """Total number of tickets held across all line items."""
        return sum(item.quantity for item in self.items)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cartId": self.cart_id,
            "reservation_id": self.reservation_id,
            "status": self.status.value,
            "items": [item.to_dict() for item in self.items],
            "total_amount": self.total_amount,
            "currency": self.currency,
            "hold_expires_at": self.hold_expires_at.isoformat() if self.hold_expires_at else None,
            "seconds_remaining": self.seconds_remaining,
            "hold_duration_seconds": self.hold_duration_seconds,
            "checkout_url": self.checkout_url,
            "created_at": self.created_at.isoformat(),
            "show_id": self.show_id,
            "event_name": self.event_name,
            "error_message": self.error_message,
            "retry_after": self.retry_after,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CartReservation":
        status_raw = data.get("status") or "PENDING"
        status = ReservationStatus(status_raw) if isinstance(status_raw, str) else status_raw
        items = [HoldItem.from_dict(it) for it in data.get("items", [])]
        expires_at_raw = data.get("hold_expires_at") or data.get("expiresAt")
        expires_at = None
        if expires_at_raw:
            try:
                expires_at = datetime.fromisoformat(str(expires_at_raw).replace("Z", "+00:00"))
            except ValueError:
                pass

        created_at_raw = data.get("created_at") or data.get("createdAt")
        created_at = None
        if created_at_raw:
            try:
                created_at = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
            except ValueError:
                pass
        if not created_at:
            created_at = datetime.now(timezone.utc)

        cart_id = str(data.get("cartId") or data.get("cart_id") or data.get("reservation_id") or "")
        hold_duration = int(data.get("hold_duration_seconds") or data.get("holdDurationSeconds") or 600)
        sec_rem = float(data["secondsRemaining"]) if data.get("secondsRemaining") is not None else (
            float(data["seconds_remaining"]) if data.get("seconds_remaining") is not None else None
        )

        return cls(
            cart_id=cart_id,
            reservation_id=cart_id,
            status=status,
            items=items,
            total_amount=float(data.get("total_amount") or data.get("totalAmount") or data.get("totalPrice") or 0.0),
            currency=str(data.get("currency") or "ARS"),
            hold_expires_at=expires_at,
            _seconds_remaining=sec_rem,
            hold_duration_seconds=hold_duration,
            checkout_url=str(data.get("checkout_url") or data.get("checkoutUrl") or ""),
            raw_response=dict(data.get("raw_response") or {}),
            created_at=created_at,
            show_id=int(data["show_id"]) if data.get("show_id") is not None else (int(data["showId"]) if data.get("showId") is not None else None),
            event_name=str(data["event_name"]) if data.get("event_name") else None,
            error_message=str(data["error_message"]) if data.get("error_message") else None,
            retry_after=int(data["retry_after"]) if data.get("retry_after") is not None else None,
        )
