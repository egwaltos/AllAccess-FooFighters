"""
Empirical Adversarial Test Suite for Milestone 3 (Challenger M3-2).

Target Deliverables:
- MockAllAccessCheckoutService (allaccess_monitor/reservation/mock_service.py)
- MockCheckoutHTTPAdapter (allaccess_monitor/reservation/mock_service.py)
- ReservationEngine (allaccess_monitor/reservation/engine.py)
- AllAccessCartClient (allaccess_monitor/reservation/client.py)
- reserve_prototype.py CLI entrypoint (allaccess_monitor/scripts/reserve_prototype.py)

Adversarial Challenge Areas:
1. High-concurrency inventory depletion races (spawn 20-50 threads competing for limited inventory).
2. Hold timeout auto-replenishment & expiry reaping (verify expired holds return seats to inventory).
3. Simulated rate limiting (429 + Retry-After) and WAF challenge (403) responses.
4. Engine budget spikes and auto-release via release_cart().
5. CLI runner error handling under invalid inputs and OS subprocess isolation.
6. Non-transactional safety boundary penetration testing.

All tests execute with zero external network dependencies and deterministic isolation.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import io
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import MagicMock, patch

from allaccess_monitor.models import AvailabilityStatus, CheckResult, SectionInfo
from allaccess_monitor.reservation.client import AllAccessCartClient, assert_non_transactional
from allaccess_monitor.reservation.engine import ReservationEngine, ReservationEngineConfig
from allaccess_monitor.reservation.mock_service import (
    MockAllAccessCheckoutService,
    MockCheckoutHTTPAdapter,
    MockHoldRecord,
    MockScenario,
)
from allaccess_monitor.reservation.models import (
    CartReservation,
    HoldItem,
    NonTransactionalSafetyViolation,
    ReservationConstraintError,
    ReservationError,
    ReservationRequest,
    ReservationStatus,
)
from allaccess_monitor.scripts import reserve_prototype


# ==============================================================================
# 1. High-Concurrency Inventory Depletion Races
# ==============================================================================

class TestHighConcurrencyInventoryRacesAdversarial(unittest.TestCase):
    """
    Stress-testing race conditions in MockAllAccessCheckoutService and ReservationEngine
    under high concurrent multi-threaded contention.
    """

    def test_high_concurrency_inventory_depletion_30_threads_5_stock(self):
        """
        Adversarial Hypothesis:
        When 30 concurrent threads simultaneously race to reserve 2 tickets from a
        fixed inventory pool of 5 tickets:
        - Thread interleaving could allow overselling (allocating > 5 tickets).
        - Race condition could corrupt internal inventory counters.
        - Exactly 2 requests must succeed (allocating 4 tickets total; 1 remaining).
        - Exactly 28 requests must receive HTTP 409 Conflict (INSUFFICIENT_INVENTORY).
        - Total inventory invariant must be strictly conserved: available_stock + held == initial_stock.
        """
        initial_stock = 5
        service = MockAllAccessCheckoutService(default_stock=initial_stock)
        num_threads = 30
        requested_qty = 2

        results: List[Dict[str, Any]] = []
        barrier = threading.Barrier(num_threads)

        def worker(thread_idx: int):
            barrier.wait()  # Align all threads to hit the service simultaneously
            code, headers, body = service.reserve(
                show_id=239575,
                sector_id=1,
                rate_id=4486883,
                quantity=requested_qty,
                sector_name=f"Thread-{thread_idx}",
            )
            return {"thread_idx": thread_idx, "code": code, "body": body}

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, i) for i in range(num_threads)]
            for f in as_completed(futures):
                results.append(f.result())

        # Collect outcomes
        successes = [r for r in results if r["code"] == 200]
        failures_409 = [r for r in results if r["code"] == 409]

        self.assertEqual(len(results), num_threads)
        self.assertEqual(len(successes), 2, f"Exactly 2 reservations must succeed for 5 stock with qty=2, got {len(successes)}")
        self.assertEqual(len(failures_409), 28, f"Exactly 28 reservations must fail with 409, got {len(failures_409)}")

        # Verify inventory state
        self.assertEqual(service.available_stock, 1, "Remaining stock must be exactly 1")
        total_held = sum(h.quantity for h in service.holds.values())
        self.assertEqual(total_held, 4, "Total held tickets must be exactly 4")
        self.assertEqual(service.available_stock + total_held, initial_stock, "Inventory conservation invariant violated!")

        # Verify all cart IDs are unique
        cart_ids = [r["body"]["cartId"] for r in successes]
        self.assertEqual(len(set(cart_ids)), len(cart_ids), "Duplicate cart IDs generated under concurrency!")

    def test_high_concurrency_depletion_50_threads_exact_pool(self):
        """
        Adversarial Hypothesis:
        50 concurrent threads competing for 20 tickets (quantity=1 each).
        - Exactly 20 must succeed, 30 must fail.
        - Final available stock must be exactly 0, not negative.
        """
        initial_stock = 20
        service = MockAllAccessCheckoutService(default_stock=initial_stock)
        num_threads = 50
        barrier = threading.Barrier(num_threads)

        def worker():
            barrier.wait()
            return service.reserve(show_id=239575, rate_id=4486883, quantity=1)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            outcomes = list(executor.map(lambda _: worker(), range(num_threads)))

        success_count = sum(1 for code, _, _ in outcomes if code == 200)
        fail_count = sum(1 for code, _, _ in outcomes if code == 409)

        self.assertEqual(success_count, 20)
        self.assertEqual(fail_count, 30)
        self.assertEqual(service.available_stock, 0)
        self.assertEqual(len(service.holds), 20)

    def test_concurrent_reservations_and_releases_no_deadlock_or_corruption(self):
        """
        Adversarial Hypothesis:
        20 threads continuously reserving while 10 threads continuously releasing carts.
        - Must never deadlock.
        - Available stock must never drop below 0 or exceed initial stock (10).
        """
        initial_stock = 10
        service = MockAllAccessCheckoutService(default_stock=initial_stock)
        stop_event = threading.Event()
        errors: List[str] = []

        def reserver():
            while not stop_event.is_set():
                code, _, body = service.reserve(show_id=239575, rate_id=4486883, quantity=1)
                if code == 200:
                    time.sleep(0.001)

        def releaser():
            while not stop_event.is_set():
                with service._lock:
                    active = [cid for cid, h in service.holds.items() if not h.released]
                if active:
                    cid = active[0]
                    service.release_cart(cid)
                time.sleep(0.001)

        def auditor():
            while not stop_event.is_set():
                with service._lock:
                    stock = service.available_stock
                    held = sum(h.quantity for h in service.holds.values() if not h.released)
                    if stock < 0:
                        errors.append(f"Negative stock observed: {stock}")
                    if stock + held != initial_stock:
                        errors.append(f"Inventory mismatch: stock={stock}, held={held}, total={stock+held}")
                time.sleep(0.0005)

        threads = [threading.Thread(target=reserver) for _ in range(10)]
        threads += [threading.Thread(target=releaser) for _ in range(5)]
        threads.append(threading.Thread(target=auditor))

        for t in threads:
            t.start()

        time.sleep(0.3)  # Run concurrent chaos for 300ms
        stop_event.set()

        for t in threads:
            t.join(timeout=2.0)

        self.assertEqual(len(errors), 0, f"Concurrency invariants violated: {errors[:5]}")

    def test_concurrent_double_release_race_prevents_duplicate_replenishment(self):
        """
        Adversarial Hypothesis:
        If 20 threads simultaneously attempt to release the SAME cart_id,
        a race condition could cause the stock to be replenished 20 times (20 * 2 = +40 stock).

        Verification:
        - Exactly 1 release succeeds with released=True.
        - 19 releases report alreadyReleased=True.
        - Available stock increments by exactly 2 (the cart quantity).
        """
        initial_stock = 10
        service = MockAllAccessCheckoutService(default_stock=initial_stock)
        code, _, body = service.reserve(show_id=239575, rate_id=4486883, quantity=2)
        cart_id = body["cartId"]
        self.assertEqual(service.available_stock, 8)

        num_threads = 20
        barrier = threading.Barrier(num_threads)

        def release_worker():
            barrier.wait()
            return service.release_cart(cart_id)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            release_outcomes = list(executor.map(lambda _: release_worker(), range(num_threads)))

        newly_released_count = sum(1 for code, _, body in release_outcomes if body.get("released") is True)
        already_released_count = sum(1 for code, _, body in release_outcomes if body.get("alreadyReleased") is True)

        self.assertEqual(newly_released_count, 1, "Exactly one thread must execute the initial release")
        self.assertEqual(already_released_count, 19, "Remaining 19 threads must observe alreadyReleased=True")
        self.assertEqual(service.available_stock, 10, "Stock must return to exactly initial stock (10), no duplicate replenishment")


# ==============================================================================
# 2. Hold Timeout Auto-Replenishment & Expiry Reaping
# ==============================================================================

class TestHoldTimeoutAutoReplenishmentAdversarial(unittest.TestCase):
    """
    Stress-testing inventory replenishment upon hold expiration, manual release,
    and engine hold lifecycle management.
    """

    def test_expired_hold_replenishes_stock_on_get_and_allows_re_reservation(self):
        """
        Adversarial Hypothesis:
        1. Exhaust entire inventory pool (5 tickets held, 0 available).
        2. Verify subsequent reservation is rejected with 409 Conflict.
        3. Fast-forward expiration into the past.
        4. Query GET /api/cart/{cart_id}: returns 410 HOLD_EXPIRED and restores 5 tickets.
        5. A new reservation for 5 tickets immediately succeeds.
        """
        service = MockAllAccessCheckoutService(default_stock=5)
        code, _, body = service.reserve(show_id=239575, rate_id=4486883, quantity=5)
        cart_id = body["cartId"]
        self.assertEqual(service.available_stock, 0)

        # Depleted pool rejects new reservations
        fail_code, _, _ = service.reserve(show_id=239575, rate_id=4486883, quantity=1)
        self.assertEqual(fail_code, 409)

        # Manually backdate expiration timestamp
        hold_rec = service.holds[cart_id]
        hold_rec.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        self.assertTrue(hold_rec.is_expired)

        # GET /api/cart/{cart_id} discovers expiration and reaps hold
        get_code, _, get_body = service.get_cart(cart_id)
        self.assertEqual(get_code, 410)
        self.assertEqual(get_body.get("error"), "HOLD_EXPIRED")
        self.assertEqual(service.available_stock, 5)

        # Re-reserving all 5 tickets now succeeds immediately
        new_code, _, new_body = service.reserve(show_id=239575, rate_id=4486883, quantity=5)
        self.assertEqual(new_code, 200)
        self.assertNotEqual(new_body["cartId"], cart_id)
        self.assertEqual(service.available_stock, 0)

    def test_multiple_expired_holds_replenished_piecemeal(self):
        """
        Adversarial Hypothesis:
        Multiple holds with staggered expirations:
        - Hold 1 (2 tickets) expires and is reaped -> stock becomes 2.
        - Reserving 3 tickets still fails (409).
        - Hold 2 (3 tickets) expires and is reaped -> stock becomes 5.
        - Reserving 3 tickets now succeeds.
        """
        service = MockAllAccessCheckoutService(default_stock=5)
        _, _, b1 = service.reserve(show_id=239575, rate_id=4486883, quantity=2)
        _, _, b2 = service.reserve(show_id=239575, rate_id=4486883, quantity=3)
        cid1, cid2 = b1["cartId"], b2["cartId"]
        self.assertEqual(service.available_stock, 0)

        # Expire only hold 1
        service.holds[cid1].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(service.get_cart(cid1)[0], 410)
        self.assertEqual(service.available_stock, 2)

        # Requesting 3 tickets fails (only 2 available)
        self.assertEqual(service.reserve(show_id=239575, rate_id=4486883, quantity=3)[0], 409)

        # Expire hold 2
        service.holds[cid2].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(service.get_cart(cid2)[0], 410)
        self.assertEqual(service.available_stock, 5)

        # Requesting 3 tickets now succeeds
        self.assertEqual(service.reserve(show_id=239575, rate_id=4486883, quantity=3)[0], 200)
        self.assertEqual(service.available_stock, 2)

    def test_reservation_engine_check_and_expire_holds_lifecycle(self):
        """
        Adversarial Hypothesis:
        ReservationEngine maintains active_holds and hold_history.
        When holds expire, check_and_expire_holds():
        - Identifies expired reservations.
        - Removes them from active_holds.
        - Appends an EXPIRED status copy to hold_history.
        """
        service = MockAllAccessCheckoutService(default_stock=10)
        client = AllAccessCartClient(session=service.create_mock_session())
        engine = ReservationEngine(client=client)

        req = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
            quantity=2,
        )
        res = engine.execute_reservation(req)
        self.assertEqual(res.status, ReservationStatus.RESERVED)
        self.assertIn(res.cart_id, engine.active_holds)

        # Before expiration: check_and_expire_holds returns empty list
        self.assertEqual(engine.check_and_expire_holds(), [])
        self.assertEqual(len(engine.get_active_holds()), 1)

        # Simulate expiration by altering hold_expires_at
        past_time = datetime.now(timezone.utc) - timedelta(seconds=20)
        expired_hold = CartReservation(
            cart_id=res.cart_id,
            status=ReservationStatus.RESERVED,
            items=res.items,
            total_amount=res.total_amount,
            currency=res.currency,
            hold_expires_at=past_time,
            hold_duration_seconds=600,
            checkout_url=res.checkout_url,
            created_at=past_time - timedelta(seconds=600),
        )
        engine.active_holds[res.cart_id] = expired_hold

        # Now check_and_expire_holds reaps it
        expired_ids = engine.check_and_expire_holds()
        self.assertEqual(expired_ids, [res.cart_id])
        self.assertEqual(len(engine.active_holds), 0)
        self.assertEqual(len(engine.get_active_holds()), 0)

        # hold_history contains the EXPIRED record
        expired_records = [h for h in engine.hold_history if h.status == ReservationStatus.EXPIRED]
        self.assertEqual(len(expired_records), 1)
        self.assertEqual(expired_records[0].cart_id, res.cart_id)


# ==============================================================================
# 3. Simulated Rate Limiting & WAF Challenge Responses
# ==============================================================================

class TestSimulatedRateLimitingAndWAFChallengesAdversarial(unittest.TestCase):
    """
    Stress-testing defensive edge responses: HTTP 429 (Rate Limit with Retry-After),
    HTTP 403 (AWS WAF JavaScript Challenge), and HTTP 503 (Queue-It Waiting Room).
    """

    def test_rate_limit_429_custom_retry_after_parsing(self):
        """
        Adversarial Hypothesis:
        When Crowder returns HTTP 429 with 'Retry-After: 45',
        AllAccessCartClient parses the header accurately into res.retry_after,
        sets status to FAILED_RATE_LIMITED, and includes the retry duration in error_message.
        """
        service = MockAllAccessCheckoutService(
            scenario=MockScenario.RATE_LIMIT,
            retry_after_seconds=45,
        )
        client = AllAccessCartClient(session=service.create_mock_session())

        req = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
            quantity=2,
        )
        res = client.reserve_tickets(req)

        self.assertEqual(res.status, ReservationStatus.FAILED_RATE_LIMITED)
        self.assertEqual(res.retry_after, 45)
        self.assertIn("Retry-After: 45s", res.error_message)

    def test_rate_limit_429_missing_or_malformed_retry_after_defaults_safely(self):
        """
        Adversarial Hypothesis:
        If an upstream reverse proxy strips the Retry-After header or returns non-numeric data
        (e.g. 'Wed, 21 Oct 2026 07:28:00 GMT' or 'invalid'), client must fall back safely to 30s
        without raising an unhandled ValueError.
        """
        service = MockAllAccessCheckoutService(scenario=MockScenario.RATE_LIMIT)
        session = service.create_mock_session()

        # Mount custom adapter returning malformed Retry-After header
        class MalformedRateLimitAdapter(MockCheckoutHTTPAdapter):
            def send(self, request, *args, **kwargs):
                resp = super().send(request, *args, **kwargs)
                resp.headers["Retry-After"] = "not_a_valid_number"
                return resp

        adapter = MalformedRateLimitAdapter(service)
        session.mount("https://www.allaccess.com.ar", adapter)
        client = AllAccessCartClient(session=session)

        req = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
            quantity=2,
        )
        res = client.reserve_tickets(req)

        self.assertEqual(res.status, ReservationStatus.FAILED_RATE_LIMITED)
        self.assertEqual(res.retry_after, 30, "Malformed header must safely fall back to 30s")

    def test_waf_challenge_403_html_interception_resilience(self):
        """
        Adversarial Hypothesis:
        AWS WAF intercepts with HTTP 403 and HTML containing <script src='challenge.js'>.
        AllAccessCartClient must catch this gracefully, map to FAILED_CHALLENGE,
        and not raise an unhandled JSONDecodeError.
        """
        service = MockAllAccessCheckoutService(scenario=MockScenario.WAF_CHALLENGE)
        client = AllAccessCartClient(session=service.create_mock_session())

        req = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
            quantity=2,
        )
        res = client.reserve_tickets(req)

        self.assertEqual(res.status, ReservationStatus.FAILED_CHALLENGE)
        self.assertIn("AWS WAF challenge", res.error_message)

    def test_queue_it_503_waiting_room_interception(self):
        """
        Adversarial Hypothesis:
        During massive on-sales, Queue-It returns HTTP 503 Virtual Waiting Room.
        AllAccessCartClient maps this to FAILED_CHALLENGE with informative context.
        """
        service = MockAllAccessCheckoutService()
        session = service.create_mock_session()

        class QueueItAdapter(MockCheckoutHTTPAdapter):
            def send(self, request, *args, **kwargs):
                resp = super().send(request, *args, **kwargs)
                resp.status_code = 503
                resp.headers["Content-Type"] = "text/html"
                resp.raw = io.BytesIO(b"<html><body>Queue-It Waiting Room</body></html>")
                return resp

        session.mount("https://www.allaccess.com.ar", QueueItAdapter(service))
        client = AllAccessCartClient(session=session)

        req = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
            quantity=2,
        )
        res = client.reserve_tickets(req)

        self.assertEqual(res.status, ReservationStatus.FAILED_CHALLENGE)
        self.assertIn("Queue-It virtual waiting room active", res.error_message)

    def test_auth_required_401_session_expired(self):
        """
        Adversarial Hypothesis:
        HTTP 401 Unauthorized maps to FAILED_AUTH.
        """
        service = MockAllAccessCheckoutService()
        session = service.create_mock_session()

        class AuthExpiredAdapter(MockCheckoutHTTPAdapter):
            def send(self, request, *args, **kwargs):
                resp = super().send(request, *args, **kwargs)
                resp.status_code = 401
                resp.headers["Content-Type"] = "application/json"
                resp.raw = io.BytesIO(b'{"success": false, "error": "SESSION_EXPIRED"}')
                return resp

        session.mount("https://www.allaccess.com.ar", AuthExpiredAdapter(service))
        client = AllAccessCartClient(session=session)

        req = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
            quantity=2,
        )
        res = client.reserve_tickets(req)

        self.assertEqual(res.status, ReservationStatus.FAILED_AUTH)
        self.assertIn("Session expired or authentication required", res.error_message)


# ==============================================================================
# 4. Engine Budget Spikes and Auto-Release via release_cart()
# ==============================================================================

class TestEngineBudgetSpikesAndAutoReleaseAdversarial(unittest.TestCase):
    """
    Stress-testing pre-check and post-check budget ceilings, rapid price fluctuations,
    and automatic hold release via release_cart().
    """

    def test_budget_ceiling_pre_check_avoids_api_submission(self):
        """
        Adversarial Hypothesis:
        If estimated cost exceeds max_budget, reservation is immediately rejected
        without dispatching any HTTP POST /api/cart request.
        """
        service = MockAllAccessCheckoutService()
        client = AllAccessCartClient(session=service.create_mock_session())
        engine = ReservationEngine(client=client)

        # 2 tickets at 143,750 ARS = 287,500 ARS. Budget is 100,000 ARS.
        req = ReservationRequest(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            show_id=239575,
            sector_name="Sivori media",
            rate_id=4486883,
            quantity=2,
            max_budget_ars=100000.0,
        )
        res = engine.execute_reservation(req)

        self.assertEqual(res.status, ReservationStatus.FAILED_BUDGET_EXCEEDED)
        self.assertEqual(len(service.call_history), 0, "No network request should have been submitted!")
        self.assertEqual(len(engine.active_holds), 0)

    def test_dynamic_surge_pricing_triggers_post_reservation_auto_release(self):
        """
        Adversarial Hypothesis:
        Mid-request price surge:
        - Pre-check estimate is within budget (e.g. standard estimate = 287,500 ARS, budget = 300,000 ARS).
        - BUT checkout backend returns surge pricing: unit_price = 200,000 + 30,000 fee = 230,000 ARS * 2 = 460,000 ARS!
        - Post-reservation budget check detects 460,000 > 300,000 ARS.
        - Engine auto-releases the hold via client.release_cart(cart_id).
        - MockAllAccessCheckoutService registers the DELETE request and restores inventory!
        - Engine returns FAILED_BUDGET_EXCEEDED.
        """
        # Surge pricing configured on mock service
        surge_unit = 200000.0
        surge_fee = 30000.0
        service = MockAllAccessCheckoutService(
            default_stock=10,
            unit_price=surge_unit,
            service_charge=surge_fee,
        )
        client = AllAccessCartClient(session=service.create_mock_session())
        config = ReservationEngineConfig(
            max_budget=350000.0,
            auto_release_on_budget_exceeded=True,
        )
        engine = ReservationEngine(client=client, config=config)

        # In _estimate_cost, client.session has mock adapter attached with surge pricing (460k).
        # To strictly test the POST-reservation branch, we mock _estimate_cost to simulate pre-check passing:
        with patch.object(engine, "_estimate_cost", return_value=287500.0):
            req = ReservationRequest(
                event_url="https://www.allaccess.com.ar/event/foo-fighters",
                show_id=239575,
                sector_name="Sivori media",
                rate_id=4486883,
                quantity=2,
                max_budget_ars=350000.0,
            )
            res = engine.execute_reservation(req)

        self.assertEqual(res.status, ReservationStatus.FAILED_BUDGET_EXCEEDED)
        self.assertEqual(res.total_amount, 460000.0)
        self.assertNotIn(res.cart_id, engine.active_holds)

        # Verify auto-release was executed on the mock service
        release_calls = [c for c in service.call_history if c["action"] == "release"]
        self.assertEqual(len(release_calls), 1)
        self.assertEqual(release_calls[0]["cart_id"], res.cart_id)

        # Verify inventory was fully restored to 10
        self.assertEqual(service.available_stock, 10)

    def test_surge_pricing_without_auto_release_retains_cart_in_history(self):
        """
        Adversarial Hypothesis:
        When auto_release_on_budget_exceeded=False, hold is marked FAILED_BUDGET_EXCEEDED
        and NOT released on backend.
        """
        service = MockAllAccessCheckoutService(
            default_stock=10,
            unit_price=200000.0,
            service_charge=30000.0,
        )
        client = AllAccessCartClient(session=service.create_mock_session())
        config = ReservationEngineConfig(
            max_budget=350000.0,
            auto_release_on_budget_exceeded=False,
        )
        engine = ReservationEngine(client=client, config=config)

        with patch.object(engine, "_estimate_cost", return_value=287500.0):
            req = ReservationRequest(
                event_url="https://www.allaccess.com.ar/event/foo-fighters",
                show_id=239575,
                sector_name="Sivori media",
                rate_id=4486883,
                quantity=2,
                max_budget_ars=350000.0,
            )
            res = engine.execute_reservation(req)

        self.assertEqual(res.status, ReservationStatus.FAILED_BUDGET_EXCEEDED)
        # Release call was NOT made
        release_calls = [c for c in service.call_history if c["action"] == "release"]
        self.assertEqual(len(release_calls), 0)
        # Stock remains decremented (8)
        self.assertEqual(service.available_stock, 8)

    def test_rapid_fluctuating_budgets_across_sequential_reservations(self):
        """
        Adversarial Hypothesis:
        Sequential requests with fluctuating budgets (100k, 500k, 200k, 600k).
        Only requests with budget >= total price (287,500 ARS) succeed.
        """
        service = MockAllAccessCheckoutService(default_stock=20)
        client = AllAccessCartClient(session=service.create_mock_session())
        engine = ReservationEngine(client=client)

        budgets = [100000.0, 500000.0, 200000.0, 600000.0]
        results = []

        for b in budgets:
            req = ReservationRequest(
                event_url="https://www.allaccess.com.ar/event/foo-fighters",
                show_id=239575,
                sector_name="Sivori media",
                rate_id=4486883,
                quantity=2,
                max_budget_ars=b,
            )
            results.append(engine.execute_reservation(req))

        self.assertEqual(results[0].status, ReservationStatus.FAILED_BUDGET_EXCEEDED)  # 100k < 287.5k
        self.assertEqual(results[1].status, ReservationStatus.RESERVED)                 # 500k >= 287.5k
        self.assertEqual(results[2].status, ReservationStatus.FAILED_BUDGET_EXCEEDED)  # 200k < 287.5k
        self.assertEqual(results[3].status, ReservationStatus.RESERVED)                 # 600k >= 287.5k

        self.assertEqual(len(engine.active_holds), 2)
        self.assertEqual(service.available_stock, 16)  # 20 - 4 tickets = 16


# ==============================================================================
# 5. CLI Runner Error Handling Under Invalid Inputs & Subprocess Invocations
# ==============================================================================

class TestCLIPublicSubprocessInvocationsAdversarial(unittest.TestCase):
    """
    Subprocess isolation verification testing actual CLI entrypoint via
    python -m allaccess_monitor.scripts.reserve_prototype across valid and invalid inputs.
    """

    def _run_prototype(self, args: List[str]) -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-m", "allaccess_monitor.scripts.reserve_prototype"] + args
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_subprocess_help_flag(self):
        """Executing --help exits with code 0 and prints full usage."""
        proc = self._run_prototype(["--help"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage: allaccess-reserve", proc.stdout)
        self.assertIn("--max-budget", proc.stdout)
        self.assertIn("--mock-scenario", proc.stdout)

    def test_subprocess_mock_success_default_exit_0(self):
        """Executing default --mock returns exit code 0 and prints status banner."""
        proc = self._run_prototype(["--mock", "--section", "Sivori media", "--quantity", "2"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("RESERVATION HOLD ACTIVE", proc.stdout)
        self.assertIn("DIRECT CHECKOUT LINK", proc.stdout)
        self.assertIn("SAFETY GUARANTEE", proc.stdout)

    def test_subprocess_all_mock_scenarios_exit_codes(self):
        """
        Verifies exact exit codes across all supported mock scenarios:
        - success -> Exit Code 0
        - sold_out -> Exit Code 2
        - rate_limit -> Exit Code 1
        - waf_challenge -> Exit Code 1
        - expired -> Exit Code 0 (reservation established, hold expires immediately)
        """
        scenario_codes = {
            "success": 0,
            "sold_out": 2,
            "rate_limit": 1,
            "waf_challenge": 1,
            "expired": 0,
        }
        for scenario, expected_code in scenario_codes.items():
            proc = self._run_prototype(["--mock", "--mock-scenario", scenario])
            self.assertEqual(
                proc.returncode,
                expected_code,
                f"Scenario '{scenario}' expected exit code {expected_code}, got {proc.returncode}.\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
            )

    def test_subprocess_invalid_quantity_exit_code_3(self):
        """Quantity outside [1, 4] exits with code 3 and error message."""
        for qty in ["0", "-1", "5", "100"]:
            proc = self._run_prototype(["--mock", "--quantity", qty])
            self.assertEqual(proc.returncode, 3)
            self.assertIn("Quantity must be between 1 and 4", proc.stderr + proc.stdout)

    def test_subprocess_zero_budget_clean_exit_code_3(self):
        """
        Hardening Polish Verification:
        Passing --max-budget 0 or negative is cleanly caught by the try-except block,
        printing a clean error message and returning exit code 3 without an unhandled traceback.
        """
        for budget in ["0", "-500", "-1"]:
            proc = self._run_prototype(["--mock", "--max-budget", budget])
            self.assertEqual(proc.returncode, 3, f"Zero/negative budget exits with code {proc.returncode}")
            self.assertIn("max_budget must be positive", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_subprocess_invalid_show_id_or_rate_id_exit_code_3(self):
        """Negative show_id or rate_id exits with code 3."""
        proc_show = self._run_prototype(["--mock", "--show-id", "-1"])
        self.assertEqual(proc_show.returncode, 3)

        proc_rate = self._run_prototype(["--mock", "--rate-id", "-5"])
        self.assertEqual(proc_rate.returncode, 3)

    def test_subprocess_unrecognized_argument_exit_code_2(self):
        """Unrecognized CLI arguments exit with code 2."""
        proc = self._run_prototype(["--unrecognized-nonexistent-flag"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unrecognized arguments", proc.stderr)

    def test_subprocess_invalid_mock_scenario_choice_exit_code_2(self):
        """Invalid choice for --mock-scenario exits with code 2."""
        proc = self._run_prototype(["--mock-scenario", "bogus_scenario"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid choice", proc.stderr)

    def test_subprocess_json_mode_parses_valid_json_on_stdout(self):
        """--json outputs strictly valid JSON on stdout parseable by json.loads."""
        proc = self._run_prototype(["--mock", "--json", "--quantity", "2"])
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "RESERVED")
        self.assertTrue(payload["cartId"].startswith("cart_"))
        self.assertEqual(payload["totalAmount"], 287500.0)
        self.assertEqual(payload["currency"], "ARS")
        self.assertIn("checkoutUrl", payload)

    def test_subprocess_budget_exceeded_exit_code_3(self):
        """Budget ceiling exceeded exits with code 3 and informative message."""
        proc = self._run_prototype(["--mock", "--max-budget", "50000"])
        self.assertEqual(proc.returncode, 3)
        self.assertIn("BUDGET EXCEEDED", proc.stdout)

    def test_subprocess_release_flag_executes_cleanly(self):
        """Executing with --release establishes hold then immediately releases it."""
        proc = self._run_prototype(["--mock", "--release"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Manual Hold Release: SUCCESS", proc.stdout)


# ==============================================================================
# 6. Non-Transactional Safety Boundary Adversarial Attacks
# ==============================================================================

class TestNonTransactionalSafetyPenetrationAdversarial(unittest.TestCase):
    """
    Adversarial attacks attempting to inject credit card numbers, CVVs,
    or financial transaction tokens into reservation models and payloads.
    """

    def test_card_number_pan_injection_in_passcode_rejected(self):
        """Injecting Visa card PAN into passcode raises NonTransactionalSafetyViolation."""
        with self.assertRaises(NonTransactionalSafetyViolation):
            ReservationRequest(
                event_url="https://www.allaccess.com.ar/event/foo-fighters",
                show_id=239575,
                passcode="4532-1234-5678-9012",
            )

    def test_card_number_pan_injection_in_event_name_rejected(self):
        """Injecting Mastercard PAN into event_name raises NonTransactionalSafetyViolation."""
        with self.assertRaises(NonTransactionalSafetyViolation):
            ReservationRequest(
                event_url="https://www.allaccess.com.ar/event/foo-fighters",
                show_id=239575,
                event_name="Foo Fighters 5412751234567890 Concert",
            )

    def test_card_number_pan_injection_in_user_token_rejected(self):
        """Injecting PAN into user_token raises NonTransactionalSafetyViolation."""
        with self.assertRaises(NonTransactionalSafetyViolation):
            ReservationRequest(
                event_url="https://www.allaccess.com.ar/event/foo-fighters",
                show_id=239575,
                user_token="user-tok-378282246310005",
            )

    def test_assert_non_transactional_nested_dictionary_injection(self):
        """assert_non_transactional detects prohibited keys in deeply nested structures."""
        nested_attack = {
            "showId": 239575,
            "metadata": {
                "user": {
                    "payment_method": "credit_card",
                }
            }
        }
        with self.assertRaises(NonTransactionalSafetyViolation):
            assert_non_transactional(nested_attack)

    def test_assert_non_transactional_cvv_rejection(self):
        """assert_non_transactional detects CVV / CVC fields."""
        with self.assertRaises(NonTransactionalSafetyViolation):
            assert_non_transactional({"cvv": "999"})
        with self.assertRaises(NonTransactionalSafetyViolation):
            assert_non_transactional({"security_code": "123"})

    def test_allaccess_cart_client_has_zero_payment_methods(self):
        """Cart client must have NO payment processing or checkout finalization methods."""
        client = AllAccessCartClient()
        forbidden_method_names = [
            "pay", "process_payment", "checkout_pay", "submit_card",
            "tokenize_card", "charge", "finalize_purchase", "authorize_payment"
        ]
        for name in forbidden_method_names:
            self.assertFalse(hasattr(client, name), f"Prohibited method '{name}' found on AllAccessCartClient!")


if __name__ == "__main__":
    unittest.main()
