"""
Deterministic in-memory mock checkout service and HTTP adapter for Crowder / AllAccess ticketing backend.
Provides offline simulation of inventory holds, rate limits, sold out states, and WAF challenges.
"""
from datetime import datetime, timedelta, timezone
from enum import Enum
import io
import json
import logging
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from requests.adapters import BaseAdapter
from requests.models import Request, Response

logger = logging.getLogger(__name__)


class MockScenario(str, Enum):
    """Configurable simulation scenarios for Crowder checkout responses."""
    SUCCESS = "success"
    SOLD_OUT = "sold_out"
    RATE_LIMIT = "rate_limit"
    WAF_CHALLENGE = "waf_challenge"
    EXPIRED = "expired"
    SERVER_ERROR = "server_error"


class MockHoldRecord:
    """Internal state record representing an active or released ticket reservation hold."""
    def __init__(
        self,
        cart_id: str,
        show_id: int,
        sector_name: str,
        rate_id: int,
        rate_name: str,
        quantity: int,
        unit_price: float,
        service_charge: float,
        hold_duration_seconds: int = 600,
        currency: str = "ARS",
        pre_expired: bool = False,
    ):
        self.cart_id = cart_id
        self.show_id = show_id
        self.sector_name = sector_name
        self.rate_id = rate_id
        self.rate_name = rate_name
        self.quantity = quantity
        self.unit_price = unit_price
        self.service_charge = service_charge
        self.currency = currency
        self.hold_duration_seconds = hold_duration_seconds
        self.created_at = datetime.now(timezone.utc)
        if pre_expired:
            self.expires_at = self.created_at - timedelta(seconds=10)
        else:
            self.expires_at = self.created_at + timedelta(seconds=hold_duration_seconds)
        self.released = False

    @property
    def total_price(self) -> float:
        return (self.unit_price + self.service_charge) * self.quantity

    @property
    def seconds_remaining(self) -> int:
        if self.released:
            return 0
        now = datetime.now(timezone.utc)
        diff = int((self.expires_at - now).total_seconds())
        return max(0, diff)

    @property
    def is_expired(self) -> bool:
        return not self.released and self.seconds_remaining <= 0

    def to_dict(self, base_checkout_url: str = "https://www.allaccess.com.ar/checkout") -> Dict[str, Any]:
        return {
            "success": True,
            "cartId": self.cart_id,
            "showId": self.show_id,
            "expiresAt": self.expires_at.isoformat(),
            "holdDurationSeconds": self.hold_duration_seconds,
            "secondsRemaining": self.seconds_remaining,
            "items": [
                {
                    "sectorName": self.sector_name,
                    "rateId": self.rate_id,
                    "rateName": self.rate_name,
                    "quantity": self.quantity,
                    "unitPrice": self.unit_price,
                    "serviceCharge": self.service_charge,
                    "subtotal": self.total_price,
                }
            ],
            "totalAmount": self.total_price,
            "currency": self.currency,
            "checkoutUrl": f"{base_checkout_url}?cart={self.cart_id}&token=mock_session_tok_{self.cart_id[:8]}",
        }


class MockAllAccessCheckoutService:
    """
    Deterministic in-memory ticketing and cart reservation service.
    Simulates Crowder API quotas, holds, rate limiting, and defensive edge challenges.
    """

    def __init__(
        self,
        default_stock: int = 50,
        default_hold_duration_seconds: int = 600,
        scenario: Union[str, MockScenario] = MockScenario.SUCCESS,
        unit_price: float = 125000.0,
        service_charge: float = 18750.0,
        sector_name: str = "Sivori media",
        rate_name: str = "Sivori media",
        rate_id: int = 4486883,
        show_id: int = 239575,
        retry_after_seconds: int = 30,
    ):
        self._lock = threading.Lock()
        self.scenario = self._normalize_scenario(scenario)
        self.default_stock = default_stock
        self.available_stock = default_stock
        self.default_hold_duration_seconds = default_hold_duration_seconds
        self.unit_price = unit_price
        self.service_charge = service_charge
        self.sector_name = sector_name
        self.rate_name = rate_name
        self.rate_id = rate_id
        self.show_id = show_id
        self.retry_after_seconds = retry_after_seconds

        # Active reservations keyed by cart_id
        self.holds: Dict[str, MockHoldRecord] = {}

        # Telemetry / Call inspection
        self.call_history: List[Dict[str, Any]] = []

    def _normalize_scenario(self, scenario: Union[str, MockScenario]) -> MockScenario:
        if isinstance(scenario, MockScenario):
            return scenario
        val = str(scenario).strip().lower()
        for sc in MockScenario:
            if sc.value == val:
                return sc
        raise ValueError(f"Unknown mock checkout scenario: {scenario}")

    def set_scenario(self, scenario: Union[str, MockScenario]) -> None:
        """Dynamically switch response scenario for upcoming requests."""
        with self._lock:
            self.scenario = self._normalize_scenario(scenario)

    def set_stock(self, count: int) -> None:
        """Update available inventory pool."""
        with self._lock:
            self.available_stock = max(0, int(count))

    def reset(self) -> None:
        """Clear all active holds, reset call telemetry, and restore default stock."""
        with self._lock:
            self.holds.clear()
            self.call_history.clear()
            self.available_stock = self.default_stock
            self.scenario = MockScenario.SUCCESS

    def reserve(
        self,
        show_id: int,
        sector_id: Optional[int] = None,
        rate_id: Optional[int] = None,
        quantity: int = 2,
        passcode: Optional[str] = None,
        sector_name: Optional[str] = None,
    ) -> Tuple[int, Dict[str, str], Union[Dict[str, Any], str]]:
        """
        Simulate cart reservation request (equivalent to POST /api/cart).

        Returns:
            Tuple of (status_code, headers_dict, response_body_json_or_str)
        """
        with self._lock:
            self.call_history.append({
                "action": "reserve",
                "show_id": show_id,
                "sector_id": sector_id,
                "rate_id": rate_id or self.rate_id,
                "quantity": quantity,
                "passcode": passcode,
                "timestamp": datetime.now(timezone.utc),
            })

            # Check scenario overrides
            if self.scenario == MockScenario.SOLD_OUT:
                return (
                    409,
                    {"Content-Type": "application/json"},
                    {
                        "success": False,
                        "error": "INSUFFICIENT_INVENTORY",
                        "message": "No tickets available for requested sector/rate",
                    },
                )

            if self.scenario == MockScenario.RATE_LIMIT:
                return (
                    429,
                    {
                        "Content-Type": "application/json",
                        "Retry-After": str(self.retry_after_seconds),
                    },
                    {
                        "success": False,
                        "error": "RATE_LIMIT_EXCEEDED",
                        "message": "Too many requests. Please slow down.",
                        "retryAfter": self.retry_after_seconds,
                    },
                )

            if self.scenario == MockScenario.WAF_CHALLENGE:
                challenge_html = (
                    "<html><head><title>AWS WAF Challenge</title></head><body>"
                    "<div id=\"request-captcha-container\">detectamos actividad sospechosa</div>"
                    "<script src=\"https://challenge.allaccess.com.ar/challenge.js\"></script>"
                    "</body></html>"
                )
                return (
                    403,
                    {
                        "Content-Type": "text/html; charset=utf-8",
                        "x-amzn-waf-action": "challenge",
                    },
                    challenge_html,
                )

            if self.scenario == MockScenario.SERVER_ERROR:
                return (
                    500,
                    {"Content-Type": "application/json"},
                    {
                        "success": False,
                        "error": "INTERNAL_SERVER_ERROR",
                        "message": "Crowder ticketing engine timeout",
                    },
                )

            # Check stock availability
            if self.available_stock < quantity:
                return (
                    409,
                    {"Content-Type": "application/json"},
                    {
                        "success": False,
                        "error": "INSUFFICIENT_INVENTORY",
                        "message": f"Requested {quantity} tickets, but only {self.available_stock} available",
                    },
                )

            # Decrement inventory and create hold
            self.available_stock -= quantity
            cart_id = f"cart_{uuid.uuid4().hex[:12]}"
            pre_expired = (self.scenario == MockScenario.EXPIRED)
            hold = MockHoldRecord(
                cart_id=cart_id,
                show_id=show_id,
                sector_name=sector_name or self.sector_name,
                rate_id=rate_id or self.rate_id,
                rate_name=self.rate_name,
                quantity=quantity,
                unit_price=self.unit_price,
                service_charge=self.service_charge,
                hold_duration_seconds=self.default_hold_duration_seconds,
                pre_expired=pre_expired,
            )
            self.holds[cart_id] = hold

            return 200, {"Content-Type": "application/json"}, hold.to_dict()

    def get_cart(self, cart_id: str) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        """Simulate inspecting cart hold status (GET /api/cart/{cart_id})."""
        with self._lock:
            hold = self.holds.get(cart_id)
            if not hold:
                return 404, {"Content-Type": "application/json"}, {"success": False, "error": "CART_NOT_FOUND"}

            if hold.released:
                return 410, {"Content-Type": "application/json"}, {"success": False, "error": "HOLD_RELEASED"}

            if hold.is_expired:
                # Return inventory to pool
                self.available_stock += hold.quantity
                hold.released = True
                return 410, {"Content-Type": "application/json"}, {"success": False, "error": "HOLD_EXPIRED"}

            return 200, {"Content-Type": "application/json"}, hold.to_dict()

    def release_cart(self, cart_id: str) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        """Simulate releasing active inventory hold (DELETE /api/cart/{cart_id})."""
        with self._lock:
            self.call_history.append({
                "action": "release",
                "cart_id": cart_id,
                "timestamp": datetime.now(timezone.utc),
            })
            hold = self.holds.get(cart_id)
            if not hold or hold.released:
                return 200, {"Content-Type": "application/json"}, {"success": True, "released": False, "alreadyReleased": True}

            hold.released = True
            self.available_stock += hold.quantity
            return 200, {"Content-Type": "application/json"}, {"success": True, "released": True, "cartId": cart_id}

    def create_http_adapter(self) -> BaseAdapter:
        """Create a requests.adapters.BaseAdapter backed by this mock service."""
        return MockCheckoutHTTPAdapter(self)

    def create_mock_session(self) -> requests.Session:
        """
        Create a fully configured requests.Session with this mock mounted for AllAccess endpoints.
        Zero external network requests will be issued.
        """
        session = requests.Session()
        adapter = self.create_http_adapter()
        session.mount("https://www.allaccess.com.ar", adapter)
        session.mount("http://www.allaccess.com.ar", adapter)
        session.mount("https://allaccess.com.ar", adapter)
        session.mount("http://allaccess.com.ar", adapter)
        session.mount("https://api.getcrowder.com", adapter)
        return session


class MockCheckoutHTTPAdapter(BaseAdapter):
    """
    Standard requests transport adapter intercepting AllAccess and Crowder cart endpoints.
    Dispatches directly to MockAllAccessCheckoutService in-memory.
    """

    def __init__(self, mock_service: MockAllAccessCheckoutService):
        super().__init__()
        self.mock_service = mock_service

    def send(
        self,
        request: Request,
        stream: bool = False,
        timeout: Optional[Union[float, Tuple[float, float]]] = None,
        verify: bool = True,
        cert: Optional[Any] = None,
        proxies: Optional[Any] = None,
    ) -> Response:
        """Intercept and synthesize Response without any network traffic."""
        url = request.url or ""
        method = (request.method or "GET").upper()

        resp = Response()
        resp.url = url
        resp.request = request

        # Parse request body if JSON
        body_data: Dict[str, Any] = {}
        if request.body:
            try:
                if isinstance(request.body, bytes):
                    body_data = json.loads(request.body.decode("utf-8"))
                elif isinstance(request.body, str):
                    body_data = json.loads(request.body)
            except Exception:
                pass

        # Route matching: POST /api/cart or /api/shows/{id}/reserve
        if method == "POST" and ("/api/cart" in url or "/reserve" in url) and not url.rstrip("/").endswith("/release"):
            show_id = body_data.get("showId", self.mock_service.show_id)
            sector_id = body_data.get("sectorId")
            rate_id = body_data.get("rateId", self.mock_service.rate_id)
            quantity = body_data.get("quantity", 2)
            passcode = body_data.get("passcode")
            sector_name = body_data.get("sectorName")

            status_code, headers, body = self.mock_service.reserve(
                show_id=show_id,
                sector_id=sector_id,
                rate_id=rate_id,
                quantity=quantity,
                passcode=passcode,
                sector_name=sector_name,
            )

        # Route matching: DELETE /api/cart/{id} or POST /api/cart/{id}/release
        elif (method == "DELETE" and "/api/cart/" in url) or (method == "POST" and "/release" in url):
            parts = url.rstrip("/").split("/")
            cart_id = parts[-1].replace("release", "").strip("/")
            if not cart_id and len(parts) >= 2:
                cart_id = parts[-2]
            status_code, headers, body = self.mock_service.release_cart(cart_id)

        # Route matching: GET /api/cart/{id}
        elif method == "GET" and "/api/cart/" in url:
            cart_id = url.rstrip("/").split("/")[-1]
            status_code, headers, body = self.mock_service.get_cart(cart_id)

        else:
            # Fallback 404 for unhandled mock routes
            status_code = 404
            headers = {"Content-Type": "application/json"}
            body = {"error": "ROUTE_NOT_FOUND", "url": url}

        # Populate requests.Response object
        resp.status_code = status_code
        for k, v in headers.items():
            resp.headers[k] = v

        if isinstance(body, dict):
            raw_bytes = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            raw_bytes = body.encode("utf-8")
        else:
            raw_bytes = b""

        resp.raw = io.BytesIO(raw_bytes)
        return resp

    def close(self) -> None:
        pass
