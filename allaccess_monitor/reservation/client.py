"""
HTTP Cart Client and Non-Transactional Safety Boundary for AllAccess.
Strictly limited to temporary inventory holds; zero credit card or payment logic.
"""
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

from allaccess_monitor.checker.allaccess import DEFAULT_BROWSER_HEADERS
from allaccess_monitor.reservation.models import (
    CartReservation,
    HoldItem,
    NonTransactionalSafetyViolation,
    PROHIBITED_PAYMENT_KEYWORDS,
    ReservationConstraintError,
    ReservationError,
    ReservationRequest,
    ReservationStatus,
)

logger = logging.getLogger(__name__)


def assert_non_transactional(obj: Any) -> None:
    """
    Strict safety boundary assertion. Inspects dictionaries, strings, and objects
    recursively to guarantee that NO payment credentials or transaction fields are passed.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            k_lower = str(k).lower()
            for prohibited in PROHIBITED_PAYMENT_KEYWORDS:
                if prohibited in k_lower:
                    raise NonTransactionalSafetyViolation(
                        f"Non-transactional safety boundary violation: Prohibited key '{k}' found in payload."
                    )
            assert_non_transactional(v)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            assert_non_transactional(item)
    elif isinstance(obj, str):
        val_lower = obj.lower()
        if re.search(r'\b(?:\d[ -]*?){13,19}\b', val_lower):
            raise NonTransactionalSafetyViolation(
                "Non-transactional safety boundary violation: string contains detected card number pattern."
            )
    elif hasattr(obj, "__dict__"):
        assert_non_transactional(vars(obj))


class BaseCartClient(ABC):
    """Abstract interface contract for AllAccess cart reservation clients."""

    @abstractmethod
    def reserve_tickets(self, request: ReservationRequest) -> CartReservation:
        """
        Request a temporary inventory hold in the shopping cart.

        Args:
            request: Validated ReservationRequest specifying show, sector, rate, and quantity.

        Returns:
            CartReservation indicating success (RESERVED) with checkout URL and expiry,
            or failure status with error message.
        """
        pass

    def reserve_cart(self, request: ReservationRequest) -> CartReservation:
        """Alias for reserve_tickets adhering to alternate method naming."""
        return self.reserve_tickets(request)

    @abstractmethod
    def release_cart(self, cart_id: str, show_id: Optional[int] = None) -> bool:
        """
        Explicitly release an active reservation hold.

        Args:
            cart_id: The reservation cart identifier to release.
            show_id: Optional show identifier if required by endpoint.

        Returns:
            bool: True if hold was successfully released, False otherwise.
        """
        pass

    @abstractmethod
    def get_cart(self, cart_id: str) -> Optional[CartReservation]:
        """Query the current state of an active cart hold."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Release underlying HTTP connection pools and session resources."""
        pass


class AllAccessCartClient(BaseCartClient):
    """
    Production-grade HTTP client interacting with AllAccess / Crowder cart endpoints.
    Enforces non-transactional safety, handles browser header spoofing, session cookies,
    timeout management, and HTTP error classification.
    """

    def __init__(
        self,
        base_url: str = "https://www.allaccess.com.ar",
        session: Optional[requests.Session] = None,
        timeout: float = 10.0,
        timeout_seconds: Optional[float] = None,
        connect_timeout: float = 5.0,
        user_agent: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        eff_timeout = timeout_seconds if timeout_seconds is not None else timeout
        self.timeout = (connect_timeout, eff_timeout)
        self._owned_session = session is None
        self.session = session or requests.Session()

        # Configure headers matching Chrome 125 on Windows standard
        ua = user_agent or DEFAULT_BROWSER_HEADERS.get(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        self.headers: Dict[str, str] = {
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
            "Sec-Ch-Ua": DEFAULT_BROWSER_HEADERS.get("Sec-Ch-Ua", '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"'),
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Requested-With": "XMLHttpRequest",
        }
        self.session.headers.update(self.headers)

    def reserve_tickets(self, request: ReservationRequest) -> CartReservation:
        """Executes POST /api/cart to acquire temporary inventory hold."""
        # 1. Enforce strict non-transactional safety
        assert_non_transactional(request)

        # 2. Build URL and Payload
        url = f"{self.base_url}/api/cart"
        payload = request.to_api_payload()
        assert_non_transactional(payload)

        # 3. Inject Request-Specific Headers / Cookies
        req_headers = dict(self.headers)
        req_headers["Origin"] = self.base_url
        req_headers["Referer"] = request.event_url or f"{self.base_url}/event/foo-fighters"
        if request.user_token:
            tok = request.user_token.strip()
            if tok.lower().startswith("bearer "):
                req_headers["Authorization"] = tok
            elif tok.startswith("ey") and "." in tok and ";" not in tok and "=" not in tok:
                req_headers["Authorization"] = f"Bearer {tok}"
            elif "=" in tok and (";" in tok or tok.startswith("_cid=") or tok.startswith("connect.sid=")):
                for part in tok.split(";"):
                    part = part.strip()
                    if "=" in part:
                        ck_name, ck_val = part.split("=", 1)
                        self.session.cookies.set(ck_name.strip(), ck_val.strip())
            else:
                # Raw cookie value: set both connect.sid and _cid for Crowder/AllAccess compatibility
                self.session.cookies.set("connect.sid", tok)
                self.session.cookies.set("_cid", tok)
        if request.session_id:
            self.session.cookies.set("connect.sid", request.session_id)
            self.session.cookies.set("_cid", request.session_id)
        if request.csrf_token:
            req_headers["X-CSRF-Token"] = request.csrf_token

        # 4. Dispatch HTTP Request with Fault Tolerance
        try:
            logger.info(
                "Submitting cart reservation: show=%d, rate=%d, qty=%d",
                request.show_id, request.rate_id, request.quantity
            )
            response = self.session.post(url, json=payload, headers=req_headers, timeout=self.timeout)
            return self._handle_reservation_response(response, request)
        except requests.Timeout as exc:
            logger.warning("Reservation request timed out: %s", exc)
            return CartReservation(
                cart_id="",
                status=ReservationStatus.FAILED_CHALLENGE,
                items=[],
                total_amount=0.0,
                error_message=f"Request timeout connecting to AllAccess: {exc}",
                show_id=request.show_id,
                event_name=request.event_name,
            )
        except requests.ConnectionError as exc:
            logger.warning("Reservation connection error: %s", exc)
            return CartReservation(
                cart_id="",
                status=ReservationStatus.FAILED_CHALLENGE,
                items=[],
                total_amount=0.0,
                error_message=f"Connection failure to AllAccess: {exc}",
                show_id=request.show_id,
                event_name=request.event_name,
            )
        except Exception as exc:
            logger.exception("Unexpected exception in reserve_tickets: %s", exc)
            return CartReservation(
                cart_id="",
                status=ReservationStatus.FAILED_CHALLENGE,
                items=[],
                total_amount=0.0,
                error_message=f"Unexpected client exception: {exc}",
                show_id=request.show_id,
                event_name=request.event_name,
            )

    def reserve_cart(self, request: ReservationRequest) -> CartReservation:
        return self.reserve_tickets(request)

    def _handle_reservation_response(self, response: requests.Response, request: ReservationRequest) -> CartReservation:
        """Classifies Crowder HTTP response status codes into structured CartReservation."""
        code = response.status_code
        now = datetime.now(timezone.utc)

        # Success: 200 OK or 201 Created
        if code in (200, 201):
            try:
                data = response.json()
            except Exception as exc:
                logger.error("Failed to parse JSON reservation response: %s", exc)
                return CartReservation(
                    cart_id="",
                    status=ReservationStatus.FAILED_CHALLENGE,
                    items=[],
                    total_amount=0.0,
                    error_message=f"Invalid JSON returned by server: {exc}",
                    show_id=request.show_id,
                    event_name=request.event_name,
                )

            cart_id = str(data.get("cartId") or data.get("id") or "")
            expires_at_raw = data.get("expiresAt") or data.get("expiration")
            hold_expires_at = None
            if expires_at_raw:
                try:
                    hold_expires_at = datetime.fromisoformat(str(expires_at_raw).replace("Z", "+00:00"))
                except ValueError:
                    pass

            hold_seconds = int(data.get("holdDurationSeconds") or data.get("holdSeconds") or 600)
            if not hold_expires_at:
                hold_expires_at = now + timedelta(seconds=hold_seconds)

            # Parse line items
            raw_items = data.get("items") or []
            items: List[HoldItem] = []
            for item_dict in raw_items:
                items.append(HoldItem.from_dict(item_dict))

            # Fallback if Crowder returned summary without items array
            if not items:
                price = float(data.get("unitPrice") or (request.max_price or 125000.0))
                fee = float(data.get("serviceCharge") or 0.0)
                subtotal = (price + fee) * request.quantity
                items.append(HoldItem(
                    sector_name=request.sector_name or "Standard Sector",
                    rate_id=request.rate_id,
                    rate_name=request.rate_name or "Standard Rate",
                    quantity=request.quantity,
                    price=price,
                    fee=fee,
                    subtotal=subtotal,
                    sector_id=request.sector_id,
                ))

            total_amount = float(data.get("totalAmount") or data.get("totalPrice") or sum(it.subtotal for it in items))
            currency = str(data.get("currency") or "ARS")

            # Construct human checkout URL
            checkout_url = str(data.get("checkoutUrl") or f"{self.base_url}/checkout?cart={cart_id}")

            logger.info("Successfully reserved cart '%s', expires in %ds", cart_id, hold_seconds)
            return CartReservation(
                cart_id=cart_id,
                status=ReservationStatus.RESERVED,
                items=items,
                total_amount=total_amount,
                currency=currency,
                hold_expires_at=hold_expires_at,
                _seconds_remaining=float(hold_seconds),
                hold_duration_seconds=hold_seconds,
                checkout_url=checkout_url,
                raw_response=data,
                created_at=now,
                show_id=request.show_id,
                event_name=request.event_name,
            )

        # 409 Conflict: Sold Out
        if code == 409:
            logger.info("Reservation rejected: tickets sold out (HTTP 409)")
            return CartReservation(
                cart_id="",
                status=ReservationStatus.FAILED_SOLD_OUT,
                items=[],
                total_amount=0.0,
                error_message="Selected tickets are sold out or locked by another hold (HTTP 409)",
                show_id=request.show_id,
                event_name=request.event_name,
            )

        # 429 Too Many Requests: Rate Limited
        if code == 429:
            retry_after_hdr = response.headers.get("Retry-After")
            retry_after = int(retry_after_hdr) if retry_after_hdr and retry_after_hdr.isdigit() else 30
            logger.warning("Reservation rate limited (HTTP 429). Retry after %ds", retry_after)
            return CartReservation(
                cart_id="",
                status=ReservationStatus.FAILED_RATE_LIMITED,
                items=[],
                total_amount=0.0,
                error_message=f"Rate limited by AllAccess/Crowder (HTTP 429). Retry-After: {retry_after}s",
                retry_after=retry_after,
                show_id=request.show_id,
                event_name=request.event_name,
            )

        # 403 Forbidden: AWS WAF Challenge / Captcha
        if code == 403:
            logger.warning("Reservation intercepted by AWS WAF Challenge (HTTP 403)")
            return CartReservation(
                cart_id="",
                status=ReservationStatus.FAILED_CHALLENGE,
                items=[],
                total_amount=0.0,
                error_message="AWS WAF challenge / Captcha intercepted request (HTTP 403)",
                show_id=request.show_id,
                event_name=request.event_name,
            )

        # 503 Service Unavailable: Queue-It Virtual Waiting Room
        if code == 503:
            logger.warning("Reservation deferred: Queue-It waiting room active (HTTP 503)")
            return CartReservation(
                cart_id="",
                status=ReservationStatus.FAILED_CHALLENGE,
                items=[],
                total_amount=0.0,
                error_message="Queue-It virtual waiting room active (HTTP 503)",
                show_id=request.show_id,
                event_name=request.event_name,
            )

        # 401 Unauthorized: Session Expired / Authentication Required
        if code == 401:
            logger.warning("Reservation unauthorized (HTTP 401)")
            return CartReservation(
                cart_id="",
                status=ReservationStatus.FAILED_AUTH,
                items=[],
                total_amount=0.0,
                error_message="Session expired or authentication required (HTTP 401)",
                show_id=request.show_id,
                event_name=request.event_name,
            )

        # Generic failure
        logger.error("Reservation failed with unexpected HTTP status %d: %s", code, response.text[:200])
        return CartReservation(
            cart_id="",
            status=ReservationStatus.FAILED_CHALLENGE,
            items=[],
            total_amount=0.0,
            error_message=f"Server returned HTTP {code}: {response.text[:100]}",
            show_id=request.show_id,
            event_name=request.event_name,
        )

    def release_cart(self, cart_id: str, show_id: Optional[int] = None) -> bool:
        """Explicitly release an active reservation hold via DELETE /api/cart/{cart_id}."""
        if not cart_id:
            return False
        url = f"{self.base_url}/api/cart/{cart_id}"
        try:
            resp = self.session.delete(url, timeout=self.timeout)
            if resp.status_code in (200, 204):
                logger.info("Successfully released cart hold '%s'", cart_id)
                return True
            # Fallback release endpoint
            fallback_url = f"{self.base_url}/api/cart/{cart_id}/release"
            fb_resp = self.session.post(fallback_url, timeout=self.timeout)
            return fb_resp.status_code in (200, 204)
        except Exception as exc:
            logger.warning("Failed to release cart '%s': %s", cart_id, exc)
            return False

    def get_cart(self, cart_id: str) -> Optional[CartReservation]:
        """Fetch current cart status from /api/cart/{cart_id}."""
        if not cart_id:
            return None
        url = f"{self.base_url}/api/cart/{cart_id}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                return CartReservation.from_dict(data)
            return None
        except Exception as exc:
            logger.warning("Error fetching cart '%s': %s", cart_id, exc)
            return None

    def close(self) -> None:
        """Close session if owned."""
        if self._owned_session and self.session:
            self.session.close()
