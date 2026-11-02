"""
Comprehensive unit tests for Target-Partitioned State & Multi-Target Scheduler (Milestone 1).
Verifies:
1. Complete elimination of the State Flapping Bug across alternating check cycles.
2. Multi-event cooldown isolation (independent monotonic timers per target).
3. Deterministic canonical target key normalization (diacritics, whitespace, URL slashes).
4. Backward-compatible property delegation and dual-layer v2/v1 JSON persistence.
5. Scheduler multi-target execution, budget ceiling alert suppression, and rate-limiting backoff.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from allaccess_monitor.checker.mock import MockTicketChecker
from allaccess_monitor.models import AvailabilityStatus, CheckResult, MonitoredTarget, SectionInfo
from allaccess_monitor.notifier.base import BaseNotifier
from allaccess_monitor.scheduler import PollingScheduler
from allaccess_monitor.state import (
    DEFAULT_TARGET_KEY,
    AlertStateManager,
    make_target_key,
    normalize_section_name,
)


class TestTargetPartitionedState(unittest.TestCase):
    """Unit tests for target-partitioned AlertStateManager state tracking and FSM rules."""

    def test_state_flapping_eliminated(self):
        """
        Verify elimination of the State Flapping Bug.
        Alternating checks between an AVAILABLE section and an UNAVAILABLE section
        must NOT reset availability or cooldown. Exactly 1 alert is sent.
        """
        manager = AlertStateManager(cooldown_seconds=1800)
        target_avail = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            target_section="Sivori media",
        )
        target_unavail = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            target_section="Platea alta",
        )

        res_avail = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            section_name="Sivori media",
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
        )
        res_unavail = CheckResult(
            status=AvailabilityStatus.UNAVAILABLE,
            section_name="Platea alta",
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
        )

        avail_alerts = 0
        unavail_alerts = 0

        # Simulate 20 alternating polling cycles (10 cycles per target)
        for cycle in range(20):
            current_time = 1000.0 + (cycle * 30.0)  # every 30s
            if cycle % 2 == 0:
                d = manager.process_check_result(
                    result=res_avail,
                    target=target_avail,
                    current_time=current_time,
                )
                if d.should_alert:
                    avail_alerts += 1
            else:
                d = manager.process_check_result(
                    result=res_unavail,
                    target=target_unavail,
                    current_time=current_time,
                )
                if d.should_alert:
                    unavail_alerts += 1

        # Exactly 1 alert sent for the available section on initial detection
        self.assertEqual(avail_alerts, 1, "State flapping occurred! More than 1 alert sent for available section.")
        self.assertEqual(unavail_alerts, 0, "Unavailable section should never alert.")
        self.assertTrue(manager.is_target_available(target_avail))
        self.assertFalse(manager.is_target_available(target_unavail))

    def test_multi_event_cooldown_isolation(self):
        """
        Verify independent cooldown timers across distinct events.
        An alert for Event A must not suppress an immediate alert for Event B.
        """
        manager = AlertStateManager(cooldown_seconds=1800)

        target_a = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            target_section="Sivori media",
        )
        target_b = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/paul-mccartney",
            target_section="Campo delantero",
        )

        res_a = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            section_name="Sivori media",
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
        )
        res_b = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            section_name="Campo delantero",
            event_url="https://www.allaccess.com.ar/event/paul-mccartney",
        )

        # t=1000: Target A becomes available -> Initial alert
        d_a1 = manager.process_check_result(result=res_a, target=target_a, current_time=1000.0)
        self.assertTrue(d_a1.should_alert)
        self.assertFalse(d_a1.is_reminder)

        # t=1010: Target B becomes available -> Initial alert (isolated from Target A!)
        d_b1 = manager.process_check_result(result=res_b, target=target_b, current_time=1010.0)
        self.assertTrue(d_b1.should_alert, "Target B must alert immediately despite Target A's cooldown!")
        self.assertFalse(d_b1.is_reminder)

        # t=1030: Target A checked again -> Suppressed by Target A cooldown
        d_a2 = manager.process_check_result(result=res_a, target=target_a, current_time=1030.0)
        self.assertFalse(d_a2.should_alert)

        # t=1040: Target B checked again -> Suppressed by Target B cooldown
        d_b2 = manager.process_check_result(result=res_b, target=target_b, current_time=1040.0)
        self.assertFalse(d_b2.should_alert)

        # t=2801 (1801s after A): Target A triggers reminder alert
        d_a3 = manager.process_check_result(result=res_a, target=target_a, current_time=2801.0)
        self.assertTrue(d_a3.should_alert)
        self.assertTrue(d_a3.is_reminder)

        # Target B (only 1791s after B) is STILL suppressed
        d_b3 = manager.process_check_result(result=res_b, target=target_b, current_time=2801.0)
        self.assertFalse(d_b3.should_alert)

        # t=2811 (1801s after B): Target B triggers reminder alert
        d_b4 = manager.process_check_result(result=res_b, target=target_b, current_time=2811.0)
        self.assertTrue(d_b4.should_alert)
        self.assertTrue(d_b4.is_reminder)

    def test_target_key_normalization_robustness(self):
        """
        Verify canonical target key normalization strips accents, diacritics,
        trailing slashes, and collapses irregular whitespace.
        """
        # Diacritics: SÍVORI MEDIA vs sivori media
        key1 = make_target_key(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            section_name="SÍVORI MEDIA",
        )
        key2 = make_target_key(
            event_url="https://www.allaccess.com.ar/event/foo-fighters/",
            section_name="sivori media",
        )
        self.assertEqual(key1, key2)

        # Whitespace and casing
        key3 = make_target_key(
            event_url="  HTTPS://WWW.ALLACCESS.COM.AR/EVENT/FOO-FIGHTERS/  ",
            section_name="  Campo   Delantero  ",
        )
        key4 = make_target_key(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            section_name="campo delantero",
        )
        self.assertEqual(key3, key4)

    def test_backward_compatible_properties(self):
        """
        Verify is_currently_available, last_alert_time, consecutive_errors,
        and total_alerts_sent reflect primary/current target state.
        """
        manager = AlertStateManager(cooldown_seconds=600)
        self.assertFalse(manager.is_currently_available)
        self.assertIsNone(manager.last_alert_time)
        self.assertEqual(manager.consecutive_errors, 0)
        self.assertEqual(manager.total_alerts_sent, 0)

        # Single target check
        manager.process_check_result(is_available=True, current_time=5000.0)
        self.assertTrue(manager.is_currently_available)
        self.assertEqual(manager.last_alert_time, 5000.0)
        self.assertEqual(manager.total_alerts_sent, 1)

    def test_state_persistence_v2_and_v1_migration(self):
        """
        Verify state persistence saves dual-layer JSON with 'version': 2
        and backwards-compatible top-level keys.
        """
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            temp_path = Path(f.name)

        try:
            # 1. Save multi-target state
            manager = AlertStateManager(state_file=temp_path, cooldown_seconds=1800)
            target1 = MonitoredTarget("https://allaccess.com.ar/event/foo", "Sivori")
            target2 = MonitoredTarget("https://allaccess.com.ar/event/foo", "Campo")

            manager.process_check_result(result=CheckResult(AvailabilityStatus.AVAILABLE, "Sivori"), target=target1, current_time=1000.0)
            manager.process_check_result(result=CheckResult(AvailabilityStatus.UNAVAILABLE, "Campo"), target=target2, current_time=1000.0)

            raw_json = json.loads(temp_path.read_text(encoding="utf-8"))
            self.assertEqual(raw_json.get("version"), 2)
            self.assertIn("targets", raw_json)
            self.assertIn("is_currently_available", raw_json)

            # 2. Reload into fresh manager instance
            manager_reloaded = AlertStateManager(state_file=temp_path)
            self.assertTrue(manager_reloaded.is_target_available(target1))
            self.assertFalse(manager_reloaded.is_target_available(target2))
            self.assertEqual(manager_reloaded.get_last_alert_time(target1), 1000.0)
            self.assertIsNone(manager_reloaded.get_last_alert_time(target2))

            # 3. Simulate legacy v1 file loading
            v1_payload = {
                "is_currently_available": True,
                "last_alert_time": 8888.0,
                "consecutive_errors": 0,
                "total_alerts_sent": 5,
            }
            temp_path.write_text(json.dumps(v1_payload), encoding="utf-8")
            manager_v1 = AlertStateManager(state_file=temp_path)
            self.assertTrue(manager_v1.is_currently_available)
            self.assertEqual(manager_v1.last_alert_time, 8888.0)
        finally:
            temp_path.unlink(missing_ok=True)


class TestSchedulerMultiTargetIntegration(unittest.TestCase):
    """Unit tests for PollingScheduler multi-target polling and budget ceiling guards."""

    def test_scheduler_budget_limit_alert_suppression(self):
        """
        Verify that PollingScheduler suppresses alerts when a section's price
        exceeds the MonitoredTarget.max_price budget ceiling.
        """
        target_expensive = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            target_section="Platea Preferencial",
            max_price=100000.0,
        )
        target_affordable = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            target_section="Campo General",
            max_price=100000.0,
        )

        checker = MockTicketChecker(
            section_statuses={
                "Platea Preferencial": AvailabilityStatus.AVAILABLE,
                "Campo General": AvailabilityStatus.AVAILABLE,
            },
            section_prices={
                "Platea Preferencial": 180000.0,
                "Campo General": 75000.0,
            },
        )

        notifier = MagicMock(spec=BaseNotifier)
        notifier.send.return_value = True

        scheduler = PollingScheduler(
            checker=checker,
            state_manager=AlertStateManager(),
            notifier=notifier,
            targets=[target_expensive, target_affordable],
            interval_seconds=10.0,
        )

        scheduler.run_single_tick()

        # Only Campo General ($75k <= $100k) should have triggered an alert
        self.assertEqual(notifier.send.call_count, 1)
        dispatched_alert = notifier.send.call_args[0][0]
        self.assertEqual(dispatched_alert.section_name, "Campo General")
        self.assertEqual(scheduler.metrics["alerts_dispatched"], 1)
        self.assertEqual(scheduler.metrics["total_checks"], 2)

    def test_scheduler_rate_limit_multi_target_backoff(self):
        """
        Verify compute_delay_for_tick honors HTTP 429 Retry-After across any target.
        """
        checker = MagicMock()
        scheduler = PollingScheduler(checker=checker, interval_seconds=10.0, max_backoff=300.0)

        res_ok = CheckResult(status=AvailabilityStatus.AVAILABLE, section_name="Sivori")
        res_429 = CheckResult(
            status=AvailabilityStatus.ERROR,
            section_name="Campo",
            status_code=429,
            error_message="Retry-After: 90",
        )

        scheduler._last_tick_results = [res_ok, res_429]
        delay = scheduler.compute_delay_for_tick()
        self.assertEqual(delay, 90)


if __name__ == "__main__":
    unittest.main()
