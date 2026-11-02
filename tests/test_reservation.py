"""
Comprehensive automated test suite for AllAccess Reservation Subsystem (Milestone 3).
Verifies ReservationRequest, CartReservation, AllAccessCartClient, ReservationEngine,
MockAllAccessCheckoutService, and reserve_prototype CLI entrypoint.
"""
from datetime import datetime, timedelta, timezone
import io
import json
import math
import sys
import unittest
from unittest.mock import MagicMock, patch

from allaccess_monitor.models import AvailabilityStatus, CheckResult, SectionInfo
from allaccess_monitor.reservation.client import AllAccessCartClient, assert_non_transactional
from allaccess_monitor.reservation.engine import ReservationEngine, ReservationEngineConfig
from allaccess_monitor.reservation.mock_service import (
    MockAllAccessCheckoutService,
    MockCheckoutHTTPAdapter,
    MockScenario,
)
from allaccess_monitor.reservation.models import (
    CartReservation,
    HoldItem,
    NonTransactionalSafetyViolation,
    ReservationConstraintError,
    ReservationRequest,
    ReservationStatus,
)
from allaccess_monitor.scripts import reserve_prototype


class TestReservationModels(unittest.TestCase):
    """Tier 1: Unit tests for reservation domain entities and invariants."""

    def test_reservation_request_valid_defaults(self):
        """ReservationRequest instantiates with valid parameters and defaults."""
        req = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
        )
        self.assertEqual(req.quantity, 2)
        self.assertIsNone(req.max_budget_ars)
        self.assertEqual(req.show_id, 239575)

    def test_reservation_request_quantity_limits(self):
        """Quantity must be strictly between 1 and 4; out-of-range raises ValueError."""
        with self.assertRaises(ValueError):
            ReservationRequest(
                event_url="https://www.allaccess.com.ar/event/foo-fighters",
                show_id=239575,
                sector_name="Sivori media",
                rate_id=4486883,
                quantity=0,
            )
        with self.assertRaises(ValueError):
            ReservationRequest(
                event_url="https://www.allaccess.com.ar/event/foo-fighters",
                show_id=239575,
                sector_name="Sivori media",
                rate_id=4486883,
                quantity=5,
            )

    def test_reservation_request_negative_budget_raises(self):
        """Negative or zero budget raises ValueError."""
        with self.assertRaises(ValueError):
            ReservationRequest(
                event_url="https://www.allaccess.com.ar/event/foo-fighters",
                show_id=239575,
                sector_name="Sivori media",
                rate_id=4486883,
                max_budget_ars=-100.0,
            )
        with self.assertRaises(ValueError):
            ReservationRequest(
                event_url="https://www.allaccess.com.ar/event/foo-fighters",
                show_id=239575,
                sector_name="Sivori media",
                rate_id=4486883,
                max_budget_ars=0.0,
            )

    def test_reservation_request_non_transactional_safety_violation(self):
        """Attempting to inject credit card numbers or payment tokens raises NonTransactionalSafetyViolation."""
        with self.assertRaises(NonTransactionalSafetyViolation):
            ReservationRequest(
                event_url="https://www.allaccess.com.ar/event/foo-fighters",
                show_id=239575,
                sector_name="Sivori media",
                rate_id=4486883,
                passcode="4532-1234-5678-9012",  # Card PAN pattern
            )

    def test_hold_item_subtotal_calculation(self):
        """HoldItem subtotal is calculated as (unit_price + fee) * quantity."""
        item = HoldItem(
            sector_name="Sivori media",
            rate_id=4486883,
            rate_name="Sivori media",
            quantity=2,
            price=125000.0,
            fee=18750.0,
        )
        self.assertEqual(item.subtotal, 287500.0)
        self.assertEqual(item.unit_total, 143750.0)
        self.assertIn("$143,750 ARS", item.formatted_unit_price)

    def test_cart_reservation_countdown_and_expiration(self):
        """CartReservation computes seconds_remaining and handles expiration dynamically."""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=600)

        res = CartReservation(
            cart_id="cart_abc123",
            status=ReservationStatus.RESERVED,
            items=[],
            total_amount=287500.0,
            currency="ARS",
            hold_expires_at=expires_at,
            hold_duration_seconds=600,
            checkout_url="https://www.allaccess.com.ar/checkout?cart=cart_abc123",
            created_at=now,
        )
        self.assertFalse(res.is_expired)
        self.assertGreater(res.seconds_remaining, 590)

        # Pre-expired reservation
        expired_res = CartReservation(
            cart_id="cart_expired",
            status=ReservationStatus.RESERVED,
            items=[],
            total_amount=287500.0,
            currency="ARS",
            hold_expires_at=now - timedelta(seconds=10),
            hold_duration_seconds=600,
            checkout_url="https://www.allaccess.com.ar/checkout?cart=cart_expired",
            created_at=now - timedelta(seconds=610),
        )
        self.assertTrue(expired_res.is_expired)
        self.assertEqual(expired_res.seconds_remaining, 0)

    def test_hold_item_nan_inf_rejected(self):
        """HoldItem must reject NaN and Inf in price and fee."""
        for bad_val in [float("nan"), float("inf"), -float("inf")]:
            with self.assertRaises(ValueError):
                HoldItem(price=bad_val)
            with self.assertRaises(ValueError):
                HoldItem(fee=bad_val)

    def test_cart_reservation_nan_inf_rejected(self):
        """CartReservation must reject NaN, Inf, and negative total_amount."""
        for bad_val in [float("nan"), float("inf"), -float("inf"), -1.0]:
            with self.assertRaises(ValueError):
                CartReservation(total_amount=bad_val)

    def test_reservation_request_nan_inf_rejected(self):
        """ReservationRequest must reject NaN and Inf budgets."""
        for bad_val in [float("nan"), float("inf"), -float("inf")]:
            with self.assertRaises(ValueError):
                ReservationRequest(max_budget_ars=bad_val)
            with self.assertRaises(ValueError):
                ReservationRequest(max_price=bad_val)


class TestMockAllAccessCheckoutService(unittest.TestCase):
    """Tier 2: Unit tests for deterministic in-memory mock checkout service."""

    def setUp(self):
        self.service = MockAllAccessCheckoutService(default_stock=10, default_hold_duration_seconds=600)

    def test_reserve_success_decrements_stock(self):
        """Successful reservation decrements available stock and returns hold payload."""
        code, headers, body = self.service.reserve(show_id=239575, sector_id=1, rate_id=4486883, quantity=2)
        self.assertEqual(code, 200)
        self.assertTrue(body["success"])
        self.assertIn("cartId", body)
        self.assertEqual(self.service.available_stock, 8)
        self.assertIn("checkoutUrl", body)

    def test_reserve_sold_out_scenario(self):
        """SOLD_OUT scenario returns HTTP 409 Conflict."""
        self.service.set_scenario(MockScenario.SOLD_OUT)
        code, headers, body = self.service.reserve(show_id=239575, sector_id=1, rate_id=4486883, quantity=2)
        self.assertEqual(code, 409)
        self.assertFalse(body["success"])
        self.assertEqual(body["error"], "INSUFFICIENT_INVENTORY")

    def test_reserve_rate_limit_scenario(self):
        """RATE_LIMIT scenario returns HTTP 429 with Retry-After header."""
        self.service.set_scenario(MockScenario.RATE_LIMIT)
        code, headers, body = self.service.reserve(show_id=239575, sector_id=1, rate_id=4486883, quantity=2)
        self.assertEqual(code, 429)
        self.assertIn("Retry-After", headers)
        self.assertEqual(headers["Retry-After"], "30")

    def test_reserve_waf_challenge_scenario(self):
        """WAF_CHALLENGE scenario returns HTTP 403 with challenge markup."""
        self.service.set_scenario(MockScenario.WAF_CHALLENGE)
        code, headers, body = self.service.reserve(show_id=239575, sector_id=1, rate_id=4486883, quantity=2)
        self.assertEqual(code, 403)
        self.assertIn("request-captcha-container", body)
        self.assertEqual(headers.get("x-amzn-waf-action"), "challenge")

    def test_reserve_server_error_scenario(self):
        """SERVER_ERROR scenario returns HTTP 500."""
        self.service.set_scenario(MockScenario.SERVER_ERROR)
        code, headers, body = self.service.reserve(show_id=239575, sector_id=1, rate_id=4486883, quantity=2)
        self.assertEqual(code, 500)
        self.assertFalse(body["success"])

    def test_manual_release_restores_inventory(self):
        """Releasing an active cart hold restores inventory to the available pool."""
        code, headers, body = self.service.reserve(show_id=239575, sector_id=1, rate_id=4486883, quantity=4)
        cart_id = body["cartId"]
        self.assertEqual(self.service.available_stock, 6)

        rel_code, rel_headers, rel_body = self.service.release_cart(cart_id)
        self.assertEqual(rel_code, 200)
        self.assertTrue(rel_body["released"])
        self.assertEqual(self.service.available_stock, 10)

    def test_expired_hold_replenishes_stock_on_get(self):
        """Querying an expired hold marks it expired and returns stock to pool."""
        self.service.set_scenario(MockScenario.EXPIRED)
        code, headers, body = self.service.reserve(show_id=239575, sector_id=1, rate_id=4486883, quantity=3)
        cart_id = body["cartId"]
        self.assertEqual(self.service.available_stock, 7)

        # Inspecting expired hold triggers return to pool
        get_code, get_headers, get_body = self.service.get_cart(cart_id)
        self.assertEqual(get_code, 410)
        self.assertEqual(self.service.available_stock, 10)


class TestMockCheckoutHTTPAdapter(unittest.TestCase):
    """Tier 3: Unit tests for requests BaseAdapter routing without network calls."""

    def test_session_mount_intercepts_cart_post(self):
        """Mounted session intercepts POST /api/cart and returns synthesized response."""
        service = MockAllAccessCheckoutService()
        session = service.create_mock_session()

        payload = {"showId": 239575, "rateId": 4486883, "quantity": 2}
        resp = session.post("https://www.allaccess.com.ar/api/cart", json=payload)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["cartId"].startswith("cart_"))

    def test_session_mount_intercepts_cart_delete(self):
        """Mounted session intercepts DELETE /api/cart/{id}."""
        service = MockAllAccessCheckoutService()
        session = service.create_mock_session()

        _, _, body = service.reserve(show_id=239575, sector_id=1, rate_id=4486883, quantity=2)
        cart_id = body["cartId"]

        resp = session.delete(f"https://www.allaccess.com.ar/api/cart/{cart_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["released"])

    def test_session_mount_intercepts_cart_get(self):
        """Mounted session intercepts GET /api/cart/{id}."""
        service = MockAllAccessCheckoutService()
        session = service.create_mock_session()

        _, _, body = service.reserve(show_id=239575, sector_id=1, rate_id=4486883, quantity=2)
        cart_id = body["cartId"]

        resp = session.get(f"https://www.allaccess.com.ar/api/cart/{cart_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["cartId"], cart_id)


class TestAllAccessCartClient(unittest.TestCase):
    """Tier 4: Unit tests for cart client and safety boundaries."""

    def test_non_transactional_safety_boundary(self):
        """Client must reject any payment or credit card fields."""
        client = AllAccessCartClient()
        # Verify no payment methods exist on client
        self.assertFalse(hasattr(client, "pay"))
        self.assertFalse(hasattr(client, "process_payment"))
        self.assertFalse(hasattr(client, "submit_credit_card"))

    def test_assert_non_transactional_assertion(self):
        """assert_non_transactional raises on prohibited payment fields."""
        with self.assertRaises(NonTransactionalSafetyViolation):
            assert_non_transactional({"credit_card": "1234"})
        with self.assertRaises(NonTransactionalSafetyViolation):
            assert_non_transactional({"cvv": "123"})

    def test_reserve_tickets_success_against_mock(self):
        """Client executes reservation request against mock session."""
        service = MockAllAccessCheckoutService()
        client = AllAccessCartClient(session=service.create_mock_session())

        request = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
            quantity=2,
        )
        res = client.reserve_tickets(request)
        self.assertEqual(res.status, ReservationStatus.RESERVED)
        self.assertTrue(res.cart_id.startswith("cart_"))
        self.assertEqual(res.total_amount, 287500.0)

    def test_reserve_tickets_sold_out_mapping(self):
        """Client correctly maps 409 Conflict to FAILED_SOLD_OUT."""
        service = MockAllAccessCheckoutService(scenario=MockScenario.SOLD_OUT)
        client = AllAccessCartClient(session=service.create_mock_session())

        request = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
            quantity=2,
        )
        res = client.reserve_tickets(request)
        self.assertEqual(res.status, ReservationStatus.FAILED_SOLD_OUT)

    def test_reserve_tickets_rate_limited_mapping(self):
        """Client maps 429 Too Many Requests to FAILED_RATE_LIMITED with retry_after."""
        service = MockAllAccessCheckoutService(scenario=MockScenario.RATE_LIMIT)
        client = AllAccessCartClient(session=service.create_mock_session())

        request = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
            quantity=2,
        )
        res = client.reserve_tickets(request)
        self.assertEqual(res.status, ReservationStatus.FAILED_RATE_LIMITED)
        self.assertEqual(res.retry_after, 30)

    def test_reserve_tickets_challenge_mapping(self):
        """Client maps 403 Forbidden to FAILED_CHALLENGE."""
        service = MockAllAccessCheckoutService(scenario=MockScenario.WAF_CHALLENGE)
        client = AllAccessCartClient(session=service.create_mock_session())

        request = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
            quantity=2,
        )
        res = client.reserve_tickets(request)
        self.assertEqual(res.status, ReservationStatus.FAILED_CHALLENGE)


class TestReservationEngine(unittest.TestCase):
    """Tier 5: Unit tests for reservation engine safety rules and triggers."""

    def test_budget_ceiling_rejection(self):
        """Engine aborts reservation if estimated price exceeds budget ceiling."""
        service = MockAllAccessCheckoutService()
        client = AllAccessCartClient(session=service.create_mock_session())
        engine = ReservationEngine(client=client)

        request = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
            quantity=2,
            max_budget_ars=100000.0,  # 2 tickets cost 287,500 ARS
        )
        res = engine.execute_reservation(request)
        self.assertEqual(res.status, ReservationStatus.FAILED_BUDGET_EXCEEDED)
        # Verify no calls were made to mock service
        self.assertEqual(len(service.call_history), 0)

    def test_trigger_from_check_result_available(self):
        """Engine triggers reservation when CheckResult denotes AVAILABLE status."""
        service = MockAllAccessCheckoutService()
        client = AllAccessCartClient(session=service.create_mock_session())
        notifier = MagicMock()
        engine = ReservationEngine(client=client, notifier=notifier)

        sec_info = SectionInfo(
            name="Sivori media",
            available=True,
            price=125000.0,
            service_charge=18750.0,
        )
        check_result = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            section_name="Sivori media",
            section_info=sec_info,
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
        )

        res = engine.on_availability_detected(check_result, quantity=2)
        self.assertIsNotNone(res)
        self.assertEqual(res.status, ReservationStatus.RESERVED)
        notifier.send.assert_called_once()

    def test_trigger_from_check_result_unavailable_returns_none(self):
        """Engine returns None when CheckResult status is not AVAILABLE."""
        service = MockAllAccessCheckoutService()
        client = AllAccessCartClient(session=service.create_mock_session())
        engine = ReservationEngine(client=client)

        check_result = CheckResult(
            status=AvailabilityStatus.UNAVAILABLE,
            section_name="Sivori media",
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
        )
        res = engine.on_availability_detected(check_result, quantity=2)
        self.assertIsNone(res)

    def test_post_reservation_budget_exceeded_auto_releases(self):
        """If reserved cart exceeds budget limit, engine auto-releases and marks failed."""
        service = MockAllAccessCheckoutService(unit_price=200000.0, service_charge=30000.0)
        client = AllAccessCartClient(session=service.create_mock_session())
        config = ReservationEngineConfig(max_budget=300000.0, auto_release_on_budget_exceeded=True)
        engine = ReservationEngine(client=client, config=config)

        # Pre-check estimate is bypassed if budget limit > 287500, but actual total is 460000
        request = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="VIP",
            rate_id=4486883,
            quantity=2,
            max_budget_ars=350000.0,
        )
        # Pre-check cost in mock is 2 * (200k + 30k) = 460k > 350k
        res = engine.execute_reservation(request)
        self.assertEqual(res.status, ReservationStatus.FAILED_BUDGET_EXCEEDED)

    def test_engine_config_nan_inf_rejected(self):
        """ReservationEngineConfig must reject NaN, Inf, and non-positive budgets."""
        for bad_val in [float("nan"), float("inf"), -float("inf"), 0.0, -100.0]:
            with self.assertRaises(ValueError):
                ReservationEngineConfig(max_budget=bad_val)


class TestReservationCLIPrototype(unittest.TestCase):
    """Tier 6: End-to-end integration tests of CLI prototype runner."""

    def test_cli_prototype_mock_success(self):
        """CLI prototype executes in default --mock mode returning code 0."""
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = reserve_prototype.main(["--mock", "--section", "Sivori media", "--quantity", "2"])
        self.assertEqual(code, 0)
        output = out.getvalue()
        self.assertIn("RESERVATION HOLD ACTIVE", output)
        self.assertIn("DIRECT CHECKOUT LINK", output)
        self.assertIn("SAFETY GUARANTEE", output)

    def test_cli_prototype_mock_sold_out(self):
        """CLI prototype with sold_out scenario returns exit code 2."""
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = reserve_prototype.main(["--mock", "--mock-scenario", "sold_out"])
        self.assertEqual(code, 2)
        self.assertIn("SOLD OUT", out.getvalue())

    def test_cli_prototype_budget_exceeded(self):
        """CLI prototype with exceeded budget returns exit code 3."""
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = reserve_prototype.main(["--mock", "--max-budget", "50000"])
        self.assertEqual(code, 3)
        self.assertIn("BUDGET EXCEEDED", out.getvalue())

    def test_cli_prototype_json_mode(self):
        """CLI prototype in --json mode prints valid JSON with reservation keys."""
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = reserve_prototype.main(["--mock", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["status"], "RESERVED")
        self.assertIn("cartId", data)
        self.assertIn("checkoutUrl", data)

    def test_cli_prototype_release_flag(self):
        """CLI prototype with --release acquires and immediately releases the hold."""
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = reserve_prototype.main(["--mock", "--release"])
        self.assertEqual(code, 0)
        output = out.getvalue()
        self.assertIn("Manual Hold Release: SUCCESS", output)

    def test_cli_prototype_invalid_quantity(self):
        """CLI prototype with quantity outside 1-4 returns code 3."""
        err = io.StringIO()
        with patch("sys.stderr", err):
            code = reserve_prototype.main(["--mock", "--quantity", "5"])
        self.assertEqual(code, 3)

    def test_cli_prototype_zero_or_negative_budget_clean_exit_3(self):
        """CLI prototype with 0 or negative budget prints clean error and returns exit code 3."""
        for bad_budget in ["0", "-100", "-1"]:
            err = io.StringIO()
            with patch("sys.stderr", err):
                code = reserve_prototype.main(["--mock", "--max-budget", bad_budget])
            self.assertEqual(code, 3)
            self.assertIn("max_budget must be positive", err.getvalue())

    def test_cli_prototype_nan_or_inf_budget_clean_exit_3(self):
        """CLI prototype with nan or inf budget prints clean error and returns exit code 3."""
        for bad_budget in ["nan", "inf", "-inf"]:
            err = io.StringIO()
            with patch("sys.stderr", err):
                code = reserve_prototype.main(["--mock", f"--max-budget={bad_budget}"])
            self.assertEqual(code, 3)
            self.assertIn("max_budget must be positive", err.getvalue())



if __name__ == "__main__":
    unittest.main()
