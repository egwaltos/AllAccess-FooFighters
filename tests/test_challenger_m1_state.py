"""
Adversarial Stress Test Suite for Milestone 1:
1. Target-partitioned state machine and zero state-flapping under high-frequency transitions.
2. Cooldown isolation across distinct events and targets.
3. State persistence corruption recovery (partial writes, invalid JSON, legacy v1).
4. Budget ceiling guards (suppression during price spike, resuming alerts when price falls below ceiling).
"""
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import MagicMock

from allaccess_monitor.checker.mock import MockTicketChecker
from allaccess_monitor.models import (
    AvailabilityStatus,
    CheckResult,
    MonitoredTarget,
    SectionInfo,
)
from allaccess_monitor.notifier.base import BaseNotifier
from allaccess_monitor.scheduler import PollingScheduler
from allaccess_monitor.state import (
    DEFAULT_TARGET_KEY,
    AlertStateManager,
    make_target_key,
    normalize_section_name,
)


class TestTargetPartitionedStateAdversarial(unittest.TestCase):
    """Stress tests for target-partitioned state machine and zero state-flapping."""

    def test_high_frequency_randomized_transitions_against_oracle(self):
        """
        Stress-test 15 distinct targets across 3 events over 100 high-frequency cycles
        (1,500 state evaluations). Compares decisions against a strict reference oracle
        to prove zero state-flapping, zero duplicate alerts during cooldown, and timely reminders.
        """
        random.seed(1337)
        events = [
            "https://www.allaccess.com.ar/event/event-alpha",
            "https://www.allaccess.com.ar/event/event-beta",
            "https://www.allaccess.com.ar/event/event-gamma",
        ]
        sections = [
            "Campo General",
            "Campo Delantero",
            "Platea Alta",
            "Platea Baja",
            "Sivori Media",
        ]

        targets = [
            MonitoredTarget(event_url=e, target_section=s)
            for e in events
            for s in sections
        ]
        self.assertEqual(len(targets), 15)

        cooldown_sec = 300
        manager = AlertStateManager(cooldown_seconds=cooldown_sec)

        # Oracle state tracking per target_id
        oracle = {
            t.target_id: {"avail": False, "last_alert": None, "alerts_count": 0}
            for t in targets
        }

        simulated_time = 1000.0
        total_evaluations = 0

        for cycle in range(100):
            # Advance time by 10 to 60 seconds
            simulated_time += random.uniform(10.0, 60.0)

            for t in targets:
                total_evaluations += 1
                roll = random.random()
                if roll < 0.40:
                    status = AvailabilityStatus.UNAVAILABLE
                elif roll < 0.75:
                    status = AvailabilityStatus.AVAILABLE
                else:
                    status = AvailabilityStatus.ERROR

                result = CheckResult(
                    status=status,
                    section_name=t.target_section,
                    event_url=t.event_url,
                )

                decision = manager.process_check_result(
                    result=result,
                    target=t,
                    current_time=simulated_time,
                )

                # Reference Oracle Logic
                ostate = oracle[t.target_id]
                expected_alert = False
                expected_reminder = False

                if status == AvailabilityStatus.ERROR:
                    # Transient error immunity: availability state preserved, no alert
                    pass
                elif status == AvailabilityStatus.AVAILABLE:
                    if not ostate["avail"]:
                        # Transition UNAVAILABLE -> AVAILABLE: must alert immediately
                        ostate["avail"] = True
                        ostate["last_alert"] = simulated_time
                        ostate["alerts_count"] += 1
                        expected_alert = True
                        expected_reminder = False
                    else:
                        # Steady state: AVAILABLE -> AVAILABLE
                        elapsed = simulated_time - ostate["last_alert"]
                        if elapsed >= cooldown_sec:
                            # Cooldown expired: reminder alert
                            ostate["last_alert"] = simulated_time
                            ostate["alerts_count"] += 1
                            expected_alert = True
                            expected_reminder = True
                        else:
                            # Within cooldown: throttled duplicate
                            expected_alert = False
                else:  # UNAVAILABLE
                    if ostate["avail"]:
                        ostate["avail"] = False
                        ostate["last_alert"] = None
                    expected_alert = False

                # Assert that AlertStateManager perfectly matches the authoritative oracle
                self.assertEqual(
                    decision.should_alert,
                    expected_alert,
                    f"Cycle {cycle}, Target {t.target_id}: Expected should_alert={expected_alert}, "
                    f"got {decision.should_alert}. Status={status.value}, reason='{decision.reason}'",
                )
                self.assertEqual(
                    decision.is_reminder,
                    expected_reminder,
                    f"Cycle {cycle}, Target {t.target_id}: Expected is_reminder={expected_reminder}, "
                    f"got {decision.is_reminder}. Status={status.value}",
                )

        self.assertEqual(total_evaluations, 1500)
        # Check that total alerts recorded matches sum of oracle alerts
        expected_total_alerts = sum(o["alerts_count"] for o in oracle.values())
        self.assertEqual(manager.total_alerts_sent, expected_total_alerts)

    def test_interleaved_flapping_resistance_across_targets(self):
        """
        Interleave rapid status changes between Target A (stable available)
        and Target B (flapping unavailable/error) in the same polling loop.
        Confirms Target A never flaps or sends extra alerts.
        """
        manager = AlertStateManager(cooldown_seconds=1800)
        target_a = MonitoredTarget("https://event/1", "Section A")
        target_b = MonitoredTarget("https://event/1", "Section B")

        res_a = CheckResult(AvailabilityStatus.AVAILABLE, "Section A", event_url="https://event/1")
        res_b_unavail = CheckResult(AvailabilityStatus.UNAVAILABLE, "Section B", event_url="https://event/1")
        res_b_err = CheckResult(AvailabilityStatus.ERROR, "Section B", event_url="https://event/1")

        # Initial detection of Target A
        d_a = manager.process_check_result(res_a, target=target_a, current_time=100.0)
        self.assertTrue(d_a.should_alert)

        # 50 interleaved ticks where Target B alternates between UNAVAILABLE and ERROR
        for i in range(50):
            t_now = 100.0 + (i * 10.0)
            res_b = res_b_unavail if i % 2 == 0 else res_b_err
            manager.process_check_result(res_b, target=target_b, current_time=t_now)

            # Re-check Target A: must remain throttled under cooldown
            d_a_recheck = manager.process_check_result(res_a, target=target_a, current_time=t_now)
            self.assertFalse(d_a_recheck.should_alert, f"Target A flapped at tick {i}!")


class TestCooldownIsolationAdversarial(unittest.TestCase):
    """Stress tests for cooldown isolation across distinct events and sectors."""

    def test_cooldown_isolation_t0_and_t5(self):
        """
        Verify that firing an alert for Target 1 at t=0 does NOT suppress an alert
        for Target 2 at t=5.
        """
        manager = AlertStateManager(cooldown_seconds=1800)
        target1 = MonitoredTarget("https://event/1", "Section 1")
        target2 = MonitoredTarget("https://event/2", "Section 2")

        res1 = CheckResult(AvailabilityStatus.AVAILABLE, "Section 1", event_url="https://event/1")
        res2 = CheckResult(AvailabilityStatus.AVAILABLE, "Section 2", event_url="https://event/2")

        # t=0: Target 1 alerts
        d1 = manager.process_check_result(res1, target=target1, current_time=0.0)
        self.assertTrue(d1.should_alert)
        self.assertFalse(d1.is_reminder)

        # t=5: Target 2 becomes available -> MUST alert immediately!
        d2 = manager.process_check_result(res2, target=target2, current_time=5.0)
        self.assertTrue(d2.should_alert, "Target 2 alert was suppressed by Target 1 cooldown!")
        self.assertFalse(d2.is_reminder)

        # t=5: Target 1 re-checked -> throttled
        d1_re = manager.process_check_result(res1, target=target1, current_time=5.0)
        self.assertFalse(d1_re.should_alert)

        # t=1800: Target 1 reminder fires (1800 - 0 = 1800 >= 1800)
        d1_rem = manager.process_check_result(res1, target=target1, current_time=1800.0)
        self.assertTrue(d1_rem.should_alert)
        self.assertTrue(d1_rem.is_reminder)

        # t=1800: Target 2 should NOT trigger reminder yet (1800 - 5 = 1795 < 1800)
        d2_early = manager.process_check_result(res2, target=target2, current_time=1800.0)
        self.assertFalse(d2_early.should_alert, "Target 2 triggered reminder prematurely!")

        # t=1805: Target 2 reminder fires (1805 - 5 = 1800 >= 1800)
        d2_rem = manager.process_check_result(res2, target=target2, current_time=1805.0)
        self.assertTrue(d2_rem.should_alert)
        self.assertTrue(d2_rem.is_reminder)

    def test_same_section_different_events_isolation(self):
        """
        Verify that identical section names (e.g. 'Campo') across different event URLs
        have completely isolated partitions.
        """
        manager = AlertStateManager(cooldown_seconds=600)
        t_event_a = MonitoredTarget("https://allaccess.com.ar/event/band-a", "Campo")
        t_event_b = MonitoredTarget("https://allaccess.com.ar/event/band-b", "Campo")

        res_a = CheckResult(AvailabilityStatus.AVAILABLE, "Campo", event_url=t_event_a.event_url)
        res_b = CheckResult(AvailabilityStatus.AVAILABLE, "Campo", event_url=t_event_b.event_url)

        # Event A Campo alerts at t=100
        d_a = manager.process_check_result(res_a, target=t_event_a, current_time=100.0)
        self.assertTrue(d_a.should_alert)

        # Event B Campo alerts at t=105 (must NOT be throttled by Event A's Campo!)
        d_b = manager.process_check_result(res_b, target=t_event_b, current_time=105.0)
        self.assertTrue(d_b.should_alert, "Event B's 'Campo' was improperly throttled by Event A's 'Campo'!")

        # Event B transitions to UNAVAILABLE at t=120
        res_b_soldout = CheckResult(AvailabilityStatus.UNAVAILABLE, "Campo", event_url=t_event_b.event_url)
        manager.process_check_result(res_b_soldout, target=t_event_b, current_time=120.0)

        # Event A Campo must remain AVAILABLE and in cooldown
        self.assertTrue(manager.is_target_available(t_event_a))
        self.assertFalse(manager.is_target_available(t_event_b))


class TestStateCorruptionResistanceAdversarial(unittest.TestCase):
    """Stress tests for atomic state persistence and corruption recovery."""

    def test_corruption_scenarios_graceful_recovery(self):
        """
        Test that AlertStateManager gracefully recovers from partial writes,
        malformed JSON, empty files, non-dict payloads, and corrupted fields
        without raising uncaught exceptions, and cleanly overwrites valid state.
        """
        corrupted_payloads = [
            ("partial_truncated_json", '{"version": 2, "targets": {"key": '),
            ("binary_garbage", "\x00\x01\xfe\xff<garbage_header>non-json"),
            ("empty_zero_byte_file", ""),
            ("json_array", '[{"version": 2}]'),
            ("json_string", '"not a dict"'),
            ("json_number", "4294967296"),
            ("json_null", "null"),
            ("targets_as_list", '{"version": 2, "targets": ["a", "b"]}'),
            ("corrupted_last_alert_time", '{"version": 2, "targets": {"k": {"last_alert_time": "invalid_float"}}}'),
            ("corrupted_consecutive_errors", '{"version": 2, "targets": {"k": {"consecutive_errors": "not_an_int"}}}'),
        ]

        for name, payload in corrupted_payloads:
            with self.subTest(scenario=name):
                with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
                    f.write(payload)
                    temp_path = Path(f.name)

                try:
                    # Instantiation should never raise an exception despite corrupt file
                    manager = AlertStateManager(state_file=temp_path)
                    self.assertFalse(manager.is_currently_available)
                    self.assertIsNone(manager.last_alert_time)
                    self.assertEqual(manager.consecutive_errors, 0)

                    # State machine must be fully functional after recovery
                    target = MonitoredTarget("https://event/1", "Section 1")
                    res = CheckResult(AvailabilityStatus.AVAILABLE, "Section 1", event_url="https://event/1")
                    decision = manager.process_check_result(res, target=target, current_time=500.0)
                    self.assertTrue(decision.should_alert)

                    # Verify that subsequent state save atomically creates valid v2 JSON
                    manager_reloaded = AlertStateManager(state_file=temp_path)
                    self.assertTrue(manager_reloaded.is_target_available(target))
                    self.assertEqual(manager_reloaded.get_last_alert_time(target), 500.0)
                finally:
                    temp_path.unlink(missing_ok=True)

    def test_legacy_v1_format_migration(self):
        """
        Verify that legacy v1 state files without version or targets dict
        are cleanly migrated to v2 without data loss.
        """
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            v1_data = {
                "is_currently_available": True,
                "last_alert_time": 9999.0,
                "consecutive_errors": 3,
                "total_alerts_sent": 7,
            }
            f.write(json.dumps(v1_data))
            temp_path = Path(f.name)

        try:
            manager = AlertStateManager(state_file=temp_path)
            self.assertTrue(manager.is_currently_available)
            self.assertEqual(manager.last_alert_time, 9999.0)
            self.assertEqual(manager.consecutive_errors, 3)
            self.assertEqual(manager.total_alerts_sent, 7)

            # Trigger a save and inspect output format
            manager._save_state()
            saved_content = json.loads(temp_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_content.get("version"), 2)
            self.assertIn("targets", saved_content)
            self.assertIn(DEFAULT_TARGET_KEY, saved_content["targets"])
        finally:
            temp_path.unlink(missing_ok=True)


class TestBudgetCeilingAdversarial(unittest.TestCase):
    """Stress tests for MonitoredTarget.max_price budget ceiling guards."""

    def test_budget_ceiling_suppression_on_price_spike(self):
        """
        Verify that when ticket price spikes above max_price, alert is suppressed.
        """
        target = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/foo",
            target_section="Campo",
            max_price=100000.0,
        )
        checker = MockTicketChecker(event_url=target.event_url)
        checker.set_section_status("Campo", AvailabilityStatus.AVAILABLE, price=150000.0, service_charge=0.0)

        notifier = MagicMock(spec=BaseNotifier)
        notifier.send.return_value = True

        scheduler = PollingScheduler(
            checker=checker,
            notifier=notifier,
            targets=[target],
            interval_seconds=10.0,
        )

        scheduler.run_single_tick()
        self.assertEqual(notifier.send.call_count, 0, "Alert was sent despite price exceeding max_price!")

    def test_budget_ceiling_service_charge_calculation(self):
        """
        Verify that price + service_charge is checked against max_price.
        Base price 90k <= 100k, but total 90k + 20k = 110k > 100k -> suppressed.
        """
        target = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/foo",
            target_section="Campo",
            max_price=100000.0,
        )
        checker = MockTicketChecker(event_url=target.event_url)
        checker.set_section_status("Campo", AvailabilityStatus.AVAILABLE, price=90000.0, service_charge=20000.0)

        notifier = MagicMock(spec=BaseNotifier)
        notifier.send.return_value = True

        scheduler = PollingScheduler(
            checker=checker,
            notifier=notifier,
            targets=[target],
            interval_seconds=10.0,
        )

        scheduler.run_single_tick()
        self.assertEqual(notifier.send.call_count, 0, "Alert was sent when total price with service charge exceeded max_price!")

    def test_budget_ceiling_resumes_alert_when_price_falls_below_ceiling(self):
        """
        Adversarial test for dispatch requirement:
        'verify that targets with max_price correctly suppress alerts when price spikes,
        and resume alerts when price falls below ceiling.'

        Scenario:
        1. Cycle 0: Section is UNAVAILABLE.
        2. Cycle 1: Price spikes to $150,000 (> $100,000 ceiling). Alert must be suppressed.
        3. Cycle 2: Within cooldown, price falls to $80,000 (<= $100,000 ceiling).
           Tickets are now affordable! The scheduler MUST resume and dispatch an alert.
        4. Cycle 3: Price remains $80,000 (within cooldown). Alert must be throttled.
        """
        target = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/foo",
            target_section="Campo",
            max_price=100000.0,
        )
        checker = MockTicketChecker(event_url=target.event_url)
        notifier = MagicMock(spec=BaseNotifier)
        notifier.send.return_value = True

        scheduler = PollingScheduler(
            checker=checker,
            notifier=notifier,
            targets=[target],
            interval_seconds=10.0,
        )

        # Cycle 0: UNAVAILABLE
        checker.set_section_status("Campo", AvailabilityStatus.UNAVAILABLE)
        scheduler.run_single_tick()
        self.assertEqual(notifier.send.call_count, 0)

        # Cycle 1: Price spikes to 150,000 (> 100,000 ceiling) -> Alert suppressed
        checker.set_section_status("Campo", AvailabilityStatus.AVAILABLE, price=150000.0, service_charge=0.0)
        scheduler.run_single_tick()
        self.assertEqual(notifier.send.call_count, 0, "Price spike above max_price was not suppressed!")

        # Cycle 2: Price drops to 80,000 (<= 100,000 ceiling) -> MUST alert affordable tickets!
        checker.set_section_status("Campo", AvailabilityStatus.AVAILABLE, price=80000.0, service_charge=0.0)
        scheduler.run_single_tick()
        self.assertEqual(
            notifier.send.call_count,
            1,
            "Failed to resume alert when ticket price fell below ceiling! "
            "Tickets became affordable within budget but user was never notified.",
        )
        alert = notifier.send.call_args[0][0]
        self.assertEqual(alert.price, 80000.0)

        # Cycle 3: Still available at 80,000 within cooldown -> throttled (no duplicate)
        scheduler.run_single_tick()
        self.assertEqual(notifier.send.call_count, 1, "Duplicate alert was sent during active cooldown!")


if __name__ == "__main__":
    unittest.main()
