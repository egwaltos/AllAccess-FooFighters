"""
Empirical Adversarial Test Suite for Milestone 3 (Challenger M3-1).

Target Deliverables:
- Reservation Domain Entities (models.py): ReservationRequest, CartReservation, HoldItem,
  ReservationStatus, NonTransactionalSafetyViolation, ReservationConstraintError.
- Non-Transactional Safety Boundary Assertions (client.py, models.py).
- HTTP Cart Client Error Handling & Payload Sanitization (client.py).
- In-Memory Deterministic Mock Checkout Service (mock_service.py).
- Reservation Orchestration Engine Safety Guards (engine.py).

Adversarial Stress Test Tiers:
1. Payment Credential & Token Injection (PAN patterns, CVV, prohibited keywords, nested payloads).
2. Pathological Quantities (<=0, >4, NaN, Inf, non-integer, type mismatches).
3. Pathological Prices & Budgets (negative, zero, NaN, Inf, float formatting).
4. Time Drift, Expiration Edge Cases, and Naive Datetime Boundaries.
5. High-Concurrency & Multi-Threaded State Invariant Verification.
"""
from datetime import datetime, timedelta, timezone
import math
import re
import threading
import time
from typing import Any, Dict, List
import unittest

from allaccess_monitor.reservation.client import AllAccessCartClient, assert_non_transactional
from allaccess_monitor.reservation.engine import ReservationEngine, ReservationEngineConfig
from allaccess_monitor.reservation.mock_service import (
    MockAllAccessCheckoutService,
    MockScenario,
)
from allaccess_monitor.reservation.models import (
    CartReservation,
    HoldItem,
    NonTransactionalSafetyViolation,
    PROHIBITED_PAYMENT_KEYWORDS,
    ReservationConstraintError,
    ReservationRequest,
    ReservationStatus,
)


class TestPaymentCredentialInjection(unittest.TestCase):
    """
    Tier 1: Adversarial testing of non-transactional safety boundaries.
    Verifies that payment credentials, PAN card numbers, and financial tokens
    are rejected unconditionally across all domain models and client boundaries.
    """

    def test_pan_injection_in_reservation_request_fields(self):
        """Card PAN numbers injected into any ReservationRequest string field must raise NonTransactionalSafetyViolation."""
        pan_samples = [
            ("Visa standard", "4111 2222 3333 4444"),
            ("Visa unspaced", "4111222233334444"),
            ("Visa hyphenated", "4111-2222-3333-4444"),
            ("Visa multi-space", "4111   2222   3333   4444"),
            ("Mastercard standard", "5105 1051 0510 5100"),
            ("Mastercard unspaced", "5105105105105100"),
            ("Amex 15-digit", "3782 822463 10005"),
            ("Amex unspaced", "378282246310005"),
            ("Diners Club 14-digit", "3056 930902 5904"),
            ("Diners Club unspaced", "30569309025904"),
        ]

        target_fields = [
            "passcode",
            "user_token",
            "session_id",
            "csrf_token",
            "event_url",
            "sector_name",
            "event_name",
            "rate_name",
        ]

        for label, pan in pan_samples:
            for field_name in target_fields:
                kwargs = {field_name: pan}
                with self.assertRaises(
                    NonTransactionalSafetyViolation,
                    msg=f"Failed to reject {label} in field '{field_name}'",
                ):
                    ReservationRequest(**kwargs)

    def test_assert_non_transactional_all_prohibited_keywords(self):
        """Every keyword in PROHIBITED_PAYMENT_KEYWORDS must trigger NonTransactionalSafetyViolation."""
        for keyword in PROHIBITED_PAYMENT_KEYWORDS:
            payload = {keyword: "malicious_value"}
            with self.assertRaises(
                NonTransactionalSafetyViolation,
                msg=f"assert_non_transactional failed to reject prohibited keyword '{keyword}'",
            ):
                assert_non_transactional(payload)

    def test_assert_non_transactional_nested_structures(self):
        """Prohibited keywords and PAN patterns deeply nested in dicts, lists, tuples, or sets must be detected."""
        pan = "4111-2222-3333-4444"

        # Nested in dict hierarchy
        with self.assertRaises(NonTransactionalSafetyViolation):
            assert_non_transactional({"level1": {"level2": {"cvv": "123"}}})

        # Nested in list inside dict
        with self.assertRaises(NonTransactionalSafetyViolation):
            assert_non_transactional({"items": [1, 2, {"stripe_token": "tok_visa"}]})

        # Deeply nested string PAN
        with self.assertRaises(NonTransactionalSafetyViolation):
            assert_non_transactional([[[{"auth": f"Bearer {pan}"}]]])

        # Tuple structure
        with self.assertRaises(NonTransactionalSafetyViolation):
            assert_non_transactional(("data", (1, {"payment_method": "credit"})))

        # Set structure containing PAN
        with self.assertRaises(NonTransactionalSafetyViolation):
            assert_non_transactional({pan, "clean_string"})

    def test_assert_non_transactional_custom_object_inspection(self):
        """Custom objects with __dict__ containing prohibited fields or PANs must be intercepted."""
        class MaliciousPayload:
            def __init__(self):
                self.custom_card_token = "4111222233334444"

        with self.assertRaises(NonTransactionalSafetyViolation):
            assert_non_transactional(MaliciousPayload())

    def test_subclass_reservation_request_with_prohibited_attribute(self):
        """Subclassing ReservationRequest to inject prohibited attributes must be caught at initialization."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class MaliciousRequest(ReservationRequest):
            card_number: str = "12345"

        with self.assertRaises(NonTransactionalSafetyViolation):
            MaliciousRequest()

    def test_client_reserve_tickets_intercepts_malicious_to_api_payload(self):
        """AllAccessCartClient must reject requests whose to_api_payload() produces prohibited keys."""
        class MaliciousOverrideRequest(ReservationRequest):
            def to_api_payload(self) -> Dict[str, Any]:
                return {"showId": 239575, "card_number": "4111222233334444"}

        service = MockAllAccessCheckoutService()
        client = AllAccessCartClient(session=service.create_mock_session())
        req = MaliciousOverrideRequest()

        with self.assertRaises(NonTransactionalSafetyViolation):
            client.reserve_tickets(req)

    def test_client_interface_has_zero_payment_methods(self):
        """AllAccessCartClient must strictly lack any payment execution methods."""
        client = AllAccessCartClient()
        prohibited_methods = [
            "pay",
            "charge",
            "process_payment",
            "submit_payment",
            "checkout_and_pay",
            "enter_card",
            "submit_credit_card",
        ]
        for method in prohibited_methods:
            self.assertFalse(
                hasattr(client, method),
                f"AllAccessCartClient illegally exposes payment method '{method}'",
            )


class TestQuantityBoundsAndPathologicalValues(unittest.TestCase):
    """
    Tier 2: Adversarial stress testing of quantity limits and type invariants.
    Anti-scalping rules mandate 1 <= quantity <= 4.
    """

    def test_quantity_lower_boundary_violations(self):
        """Quantities <= 0 must be rejected with ReservationConstraintError (or ValueError)."""
        lower_bounds = [0, -1, -4, -100, -999999]
        for q in lower_bounds:
            with self.assertRaises(
                ReservationConstraintError,
                msg=f"Expected ReservationConstraintError for quantity={q}",
            ):
                ReservationRequest(quantity=q)

    def test_quantity_upper_boundary_violations(self):
        """Quantities > 4 must be rejected with ReservationConstraintError (or ValueError)."""
        upper_bounds = [5, 6, 10, 50, 100, 10000]
        for q in upper_bounds:
            with self.assertRaises(
                ReservationConstraintError,
                msg=f"Expected ReservationConstraintError for quantity={q}",
            ):
                ReservationRequest(quantity=q)

    def test_quantity_floating_point_specials(self):
        """NaN and infinite quantities must be rejected unconditionally."""
        for val in [float("nan"), float("inf"), -float("inf")]:
            with self.assertRaises(
                ReservationConstraintError,
                msg=f"Expected ReservationConstraintError for non-finite quantity {val}",
            ):
                ReservationRequest(quantity=val)

    def test_quantity_non_numeric_types_raise(self):
        """String, None, and collection types passed as quantity must raise TypeError or ValueError."""
        invalid_types = ["two", "4", None, [2], {"qty": 2}]
        for val in invalid_types:
            with self.assertRaises(
                (TypeError, ValueError),
                msg=f"Expected TypeError/ValueError for quantity={val!r}",
            ):
                ReservationRequest(quantity=val)

    def test_engine_execute_reservation_with_out_of_bounds_quantity(self):
        """ReservationEngine must guard against out-of-bounds quantity requests."""
        service = MockAllAccessCheckoutService()
        client = AllAccessCartClient(session=service.create_mock_session())
        engine = ReservationEngine(client=client)

        # Build request with valid quantity then tamper
        req = ReservationRequest(quantity=2)
        object.__setattr__(req, "quantity", 5)

        res = engine.execute_reservation(req)
        self.assertFalse(res.status.is_success)
        self.assertEqual(len(service.call_history), 0)

    def test_engine_config_quantity_limits(self):
        """ReservationEngineConfig must reject max_hold_quantity outside 1..4."""
        with self.assertRaises(ReservationConstraintError):
            ReservationEngineConfig(max_hold_quantity=0)
        with self.assertRaises(ReservationConstraintError):
            ReservationEngineConfig(max_hold_quantity=5)


class TestPriceAndBudgetConstraints(unittest.TestCase):
    """
    Tier 3: Adversarial stress testing of budget ceilings and price validation.
    """

    def test_reservation_request_negative_and_zero_budgets(self):
        """max_budget_ars and max_price <= 0 must raise ValueError."""
        for bad_budget in [0.0, -0.01, -100.0, -50000.0]:
            with self.assertRaises(ValueError):
                ReservationRequest(max_budget_ars=bad_budget)
            with self.assertRaises(ValueError):
                ReservationRequest(max_price=bad_budget)

    def test_reservation_request_non_finite_budgets(self):
        """NaN and Inf budgets must raise ValueError."""
        for non_finite in [float("nan"), float("inf"), -float("inf")]:
            with self.assertRaises(ValueError):
                ReservationRequest(max_budget_ars=non_finite)
            with self.assertRaises(ValueError):
                ReservationRequest(max_price=non_finite)

    def test_hold_item_negative_price_and_fee(self):
        """HoldItem with negative price or fee must raise ValueError."""
        with self.assertRaises(ValueError):
            HoldItem(price=-1.0)
        with self.assertRaises(ValueError):
            HoldItem(fee=-0.5)

    def test_hold_item_quantity_less_than_one(self):
        """HoldItem with quantity < 1 must raise ValueError."""
        with self.assertRaises(ValueError):
            HoldItem(quantity=0)
        with self.assertRaises(ValueError):
            HoldItem(quantity=-5)

    def test_engine_config_negative_and_zero_budget(self):
        """ReservationEngineConfig must reject max_budget <= 0."""
        with self.assertRaises(ValueError):
            ReservationEngineConfig(max_budget=0.0)
        with self.assertRaises(ValueError):
            ReservationEngineConfig(max_budget=-1000.0)

    def test_engine_rejects_when_estimated_cost_exceeds_budget(self):
        """ReservationEngine aborts before HTTP call when estimated price exceeds budget."""
        service = MockAllAccessCheckoutService(unit_price=100000.0, service_charge=15000.0)
        client = AllAccessCartClient(session=service.create_mock_session())
        engine = ReservationEngine(client=client)

        # 2 tickets * 115,000 ARS = 230,000 ARS > 200,000 ARS
        req = ReservationRequest(quantity=2, max_budget_ars=200000.0)
        res = engine.execute_reservation(req)
        self.assertEqual(res.status, ReservationStatus.FAILED_BUDGET_EXCEEDED)
        self.assertEqual(len(service.call_history), 0)


class TestTimeDriftAndCountdownMechanisms(unittest.TestCase):
    """
    Tier 4: Adversarial stress testing of hold expiration, time drift, and countdowns.
    """

    def test_dynamic_seconds_remaining_decrement(self):
        """seconds_remaining must dynamically decrement as time passes."""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=10)

        res = CartReservation(
            cart_id="cart_countdown_test",
            status=ReservationStatus.RESERVED,
            hold_expires_at=expires_at,
            hold_duration_seconds=10,
        )

        rem1 = res.seconds_remaining
        time.sleep(0.05)
        rem2 = res.computed_seconds_remaining
        self.assertLessEqual(rem2, rem1)
        self.assertFalse(res.is_expired)

    def test_past_expiry_clamped_to_zero(self):
        """Past expiration date must clamp seconds_remaining to 0 and report is_expired == True."""
        now = datetime.now(timezone.utc)
        past_expires_at = now - timedelta(seconds=120)

        res = CartReservation(
            cart_id="cart_past_test",
            status=ReservationStatus.RESERVED,
            hold_expires_at=past_expires_at,
            hold_duration_seconds=600,
        )

        self.assertEqual(res.seconds_remaining, 0)
        self.assertEqual(res.computed_seconds_remaining, 0.0)
        self.assertTrue(res.is_expired)
        self.assertFalse(res.is_active)

    def test_terminal_expired_status_is_expired(self):
        """ReservationStatus.EXPIRED is unconditionally expired regardless of hold_expires_at."""
        future_dt = datetime.now(timezone.utc) + timedelta(hours=1)
        res = CartReservation(
            cart_id="cart_expired_flag",
            status=ReservationStatus.EXPIRED,
            hold_expires_at=future_dt,
        )
        self.assertTrue(res.is_expired)
        self.assertFalse(res.is_active)

    def test_extreme_expiration_dates(self):
        """Extreme dates (year 9999 or year 1970) must not crash seconds_remaining or is_expired."""
        far_future = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        res_future = CartReservation(
            cart_id="cart_future",
            status=ReservationStatus.RESERVED,
            hold_expires_at=far_future,
        )
        self.assertGreater(res_future.seconds_remaining, 1000000)
        self.assertFalse(res_future.is_expired)

        unix_epoch = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        res_past = CartReservation(
            cart_id="cart_past",
            status=ReservationStatus.RESERVED,
            hold_expires_at=unix_epoch,
        )
        self.assertEqual(res_past.seconds_remaining, 0)
        self.assertTrue(res_past.is_expired)

    def test_naive_datetime_comparison_behavior(self):
        """
        Document empirical behavior when an offset-naive datetime is supplied.
        In Python, subtracting naive from aware raises TypeError.
        """
        naive_dt = datetime(2026, 12, 1, 0, 0, 0)
        res = CartReservation(cart_id="cart_naive", hold_expires_at=naive_dt)

        # Verifies empirical finding that naive datetime raises TypeError on calculation
        with self.assertRaises(TypeError):
            _ = res.seconds_remaining
        with self.assertRaises(TypeError):
            _ = res.is_expired

    def test_engine_check_and_expire_holds_pruning(self):
        """check_and_expire_holds correctly identifies expired holds and marks them EXPIRED."""
        now = datetime.now(timezone.utc)
        service = MockAllAccessCheckoutService()
        client = AllAccessCartClient(session=service.create_mock_session())
        engine = ReservationEngine(client=client)

        active_res = CartReservation(
            cart_id="cart_active",
            status=ReservationStatus.RESERVED,
            hold_expires_at=now + timedelta(seconds=600),
        )
        expired_res = CartReservation(
            cart_id="cart_expired",
            status=ReservationStatus.RESERVED,
            hold_expires_at=now - timedelta(seconds=10),
        )

        with engine._lock:
            engine.active_holds["cart_active"] = active_res
            engine.active_holds["cart_expired"] = expired_res

        expired_ids = engine.check_and_expire_holds()
        self.assertIn("cart_expired", expired_ids)
        self.assertNotIn("cart_active", expired_ids)

        active_remaining = engine.get_active_holds()
        self.assertEqual(len(active_remaining), 1)
        self.assertEqual(active_remaining[0].cart_id, "cart_active")


class TestConcurrencyAndThreadSafety(unittest.TestCase):
    """
    Tier 5: High-concurrency stress testing and thread-safety invariant verification.
    """

    def test_concurrent_reservation_request_instantiation(self):
        """Creating 50 ReservationRequest instances concurrently across threads is thread-safe."""
        errors: List[Exception] = []

        def worker(thread_idx: int):
            try:
                for i in range(20):
                    req = ReservationRequest(
                        show_id=239575 + i,
                        sector_name=f"Sector_{thread_idx}",
                        quantity=(i % 4) + 1,
                    )
                    self.assertEqual(req.quantity, (i % 4) + 1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Encountered thread errors: {errors}")

    def test_mock_service_concurrent_reservation_stock_integrity(self):
        """
        Contention test: 20 threads simultaneously attempt to reserve 2 tickets each.
        With available_stock = 10, exactly 5 threads must succeed, exactly 15 must fail
        with HTTP 409 (Sold Out), and available_stock must reach exactly 0 (no overselling).
        """
        service = MockAllAccessCheckoutService(default_stock=10)
        results: List[int] = []
        lock = threading.Lock()

        def worker():
            code, _, _ = service.reserve(show_id=239575, quantity=2)
            with lock:
                results.append(code)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        success_count = results.count(200)
        sold_out_count = results.count(409)

        self.assertEqual(success_count, 5, f"Expected 5 successful holds, got {success_count}")
        self.assertEqual(sold_out_count, 15, f"Expected 15 sold out rejections, got {sold_out_count}")
        self.assertEqual(service.available_stock, 0, "Inventory pool must be exactly 0")

    def test_mock_service_concurrent_reserve_and_release_invariants(self):
        """
        Under concurrent reserve and release operations, the invariant:
        available_stock + sum(active held tickets) == default_stock
        must hold strictly at all times.
        """
        initial_stock = 30
        service = MockAllAccessCheckoutService(default_stock=initial_stock)
        cart_ids: List[str] = []
        lock = threading.Lock()

        def res_worker():
            code, _, body = service.reserve(show_id=239575, quantity=2)
            if code == 200:
                with lock:
                    cart_ids.append(body["cartId"])

        def rel_worker():
            cid = None
            with lock:
                if cart_ids:
                    cid = cart_ids.pop()
            if cid:
                service.release_cart(cid)

        threads: List[threading.Thread] = []
        for _ in range(20):
            threads.append(threading.Thread(target=res_worker))
        for _ in range(15):
            threads.append(threading.Thread(target=rel_worker))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        active_held_tickets = sum(h.quantity for h in service.holds.values() if not h.released)
        total_accounted = service.available_stock + active_held_tickets
        self.assertEqual(
            total_accounted,
            initial_stock,
            f"Stock invariant violated: {service.available_stock} + {active_held_tickets} != {initial_stock}",
        )

    def test_reservation_engine_concurrent_execution(self):
        """ReservationEngine must coordinate concurrent execute_reservation safely without race conditions."""
        service = MockAllAccessCheckoutService(default_stock=20)
        client = AllAccessCartClient(session=service.create_mock_session())
        engine = ReservationEngine(client=client)

        reservations: List[CartReservation] = []
        lock = threading.Lock()

        def worker():
            req = ReservationRequest(quantity=2)
            res = engine.execute_reservation(req)
            with lock:
                reservations.append(res)

        threads = [threading.Thread(target=worker) for _ in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        reserved = [r for r in reservations if r.status == ReservationStatus.RESERVED]
        sold_out = [r for r in reservations if r.status == ReservationStatus.FAILED_SOLD_OUT]

        self.assertEqual(len(reserved), 10)
        self.assertEqual(len(sold_out), 5)
        self.assertEqual(len(engine.active_holds), 10)


if __name__ == "__main__":
    unittest.main()
