"""
Comprehensive unit tests for AllAccess HTML parser, Unicode normalization,
false-positive prevention, pricing extraction, WAF detection, and network mocking.
Zero third-party test dependencies; uses Python standard library unittest.
"""
import unittest
from unittest.mock import MagicMock, patch

from allaccess_monitor.checker.allaccess import (
    AllAccessTicketChecker,
    normalize_section_name,
    parse_allaccess_html,
)
from allaccess_monitor.checker.mock import MockTicketChecker
from allaccess_monitor.models import AvailabilityStatus
from tests.fixtures import load_fixture


class TestNormalization(unittest.TestCase):
    """Test suite for Unicode NFKD normalization of section names."""

    def test_lowercase(self):
        self.assertEqual(normalize_section_name("sivori media"), "sivori media")

    def test_titlecase(self):
        self.assertEqual(normalize_section_name("Sivori Media"), "sivori media")

    def test_uppercase(self):
        self.assertEqual(normalize_section_name("SIVORI MEDIA"), "sivori media")

    def test_accent_acute_i(self):
        self.assertEqual(normalize_section_name("Sívori media"), "sivori media")
        self.assertEqual(normalize_section_name("Sívori Media"), "sivori media")

    def test_accent_uppercase_i(self):
        self.assertEqual(normalize_section_name("SÍVORI MEDIA"), "sivori media")

    def test_extra_whitespace(self):
        self.assertEqual(normalize_section_name("   Sivori    media  \n\t "), "sivori media")

    def test_empty_and_none(self):
        self.assertEqual(normalize_section_name(""), "")
        self.assertEqual(normalize_section_name(None), "")


class TestCanonicalFixtures(unittest.TestCase):
    """Test suite executing parser against all 5 canonical HTML test fixtures."""

    def test_available_fixture(self):
        html = load_fixture("available_response.html")
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)
        self.assertTrue(res.status.is_available)
        self.assertIsNotNone(res.section_info)
        self.assertTrue(res.section_info.available)
        self.assertEqual(res.section_info.price, 125000.0)
        self.assertEqual(res.section_info.service_charge, 18750.0)
        self.assertEqual(res.section_info.total_price, 143750.0)
        self.assertEqual(res.section_info.currency, "ARS")

    def test_sold_out_fixture_rejects_false_positive(self):
        html = load_fixture("sold_out_response.html")
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)
        self.assertFalse(res.status.is_available)
        self.assertIsNotNone(res.section_info)
        self.assertFalse(res.section_info.available)
        self.assertEqual(res.section_info.price, 125000.0)

    def test_section_missing_fixture(self):
        html = load_fixture("section_missing_response.html")
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.SECTION_NOT_FOUND)
        self.assertIsNone(res.section_info)
        self.assertIn("not found in event layout", res.error_message)

    def test_malformed_fixture(self):
        html = load_fixture("malformed_response.html")
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.ERROR)
        self.assertIsNone(res.section_info)
        self.assertIn("marker not found", res.error_message)

    def test_waf_challenge_fixture(self):
        html = load_fixture("waf_challenge_response.html")
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.ERROR)
        self.assertEqual(res.status_code, 403)
        self.assertIn("AWS WAF Captcha challenge detected", res.error_message)


class TestFalsePositiveEdgeCases(unittest.TestCase):
    """Test suite ensuring strict multi-tier availability validation."""

    def setUp(self):
        self.base_html = load_fixture("available_response.html")

    def test_rate_available_false_blocks_availability(self):
        html = self.base_html.replace(
            '"available": true,\n                                                "max": 4',
            '"available": false,\n                                                "max": 4'
        )
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_rate_max_zero_blocks_availability(self):
        html = self.base_html.replace('"max": 4', '"max": 0')
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_rate_availability_count_zero_blocks_availability(self):
        html = self.base_html.replace('"availability": 8', '"availability": 0')
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_section_availability_false_blocks_availability(self):
        html = self.base_html.replace(
            '"name": "Sivori Media",\n                                        "uuid": "509aabf4-24be-4c44-a86f-de570742c1e4",\n                                        "available": true',
            '"name": "Sivori Media",\n                                        "uuid": "509aabf4-24be-4c44-a86f-de570742c1e4",\n                                        "available": false'
        )
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)


class TestPricingExtraction(unittest.TestCase):
    """Test suite for ticket pricing and service fee parsing."""

    def test_pricing_and_fees(self):
        html = load_fixture("available_response.html")
        res = parse_allaccess_html(html, "Sivori media")
        self.assertIsNotNone(res.section_info)
        self.assertEqual(res.section_info.price, 125000.0)
        self.assertEqual(res.section_info.service_charge, 18750.0)
        self.assertEqual(res.section_info.total_price, 143750.0)
        self.assertEqual(res.section_info.currency, "ARS")

    def test_missing_service_charge(self):
        html = load_fixture("available_response.html").replace(
            '"serviceChargeAmount": 18750',
            '"serviceChargeAmount": null'
        )
        res = parse_allaccess_html(html, "Sivori media")
        self.assertIsNotNone(res.section_info)
        self.assertEqual(res.section_info.price, 125000.0)
        self.assertIsNone(res.section_info.service_charge)
        self.assertEqual(res.section_info.total_price, 125000.0)


class TestContextManagerAndLifecycle(unittest.TestCase):
    """Test suite for resource cleanup and context manager lifecycle."""

    def test_context_manager(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = load_fixture("available_response.html")
        session.get.return_value = resp

        with AllAccessTicketChecker(session=session) as checker:
            res = checker.check_availability("Sivori media")
            self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)


class TestNetworkAndSessionMocking(unittest.TestCase):
    """Test suite verifying HTTP error handling and transport resilience."""

    def test_allaccess_checker_available_flow(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = load_fixture("available_response.html")
        session.get.return_value = resp

        checker = AllAccessTicketChecker(session=session)
        res = checker.check_availability("Sívori Media")
        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)
        self.assertEqual(res.section_info.price, 125000.0)

    def test_allaccess_checker_http_403(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 403
        session.get.return_value = resp

        checker = AllAccessTicketChecker(session=session)
        res = checker.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.ERROR)
        self.assertEqual(res.status_code, 403)

    def test_allaccess_checker_http_429(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 429
        session.get.return_value = resp

        checker = AllAccessTicketChecker(session=session)
        res = checker.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.ERROR)
        self.assertEqual(res.status_code, 429)

    def test_allaccess_checker_http_500(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 500
        session.get.return_value = resp

        checker = AllAccessTicketChecker(session=session)
        res = checker.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.ERROR)
        self.assertEqual(res.status_code, 500)

    def test_allaccess_checker_connection_error(self):
        session = MagicMock()
        session.get.side_effect = ConnectionError("Connection refused")

        checker = AllAccessTicketChecker(session=session)
        res = checker.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.ERROR)
        self.assertIn("ConnectionError", res.error_message)

    def test_allaccess_checker_timeout(self):
        session = MagicMock()
        session.get.side_effect = TimeoutError("Read timeout")

        checker = AllAccessTicketChecker(session=session)
        res = checker.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.ERROR)
        self.assertIn("TimeoutError", res.error_message)


class TestMockTicketChecker(unittest.TestCase):
    """Test suite for MockTicketChecker offline simulation engine."""

    def test_mock_available(self):
        checker = MockTicketChecker(initial_status="available")
        res = checker.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)
        self.assertEqual(res.section_info.price, 125000.0)

    def test_mock_sold_out(self):
        checker = MockTicketChecker(initial_status="sold_out")
        res = checker.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_mock_section_missing(self):
        checker = MockTicketChecker(initial_status="section_missing")
        res = checker.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.SECTION_NOT_FOUND)

    def test_mock_sequence(self):
        checker = MockTicketChecker()
        checker.set_sequence(["sold_out", "available", "sold_out"])
        self.assertEqual(checker.check_availability("Sivori media").status, AvailabilityStatus.UNAVAILABLE)
        self.assertEqual(checker.check_availability("Sivori media").status, AvailabilityStatus.AVAILABLE)
        self.assertEqual(checker.check_availability("Sivori media").status, AvailabilityStatus.UNAVAILABLE)
        self.assertEqual(checker.call_count, 3)

    def test_mock_latency_simulation(self):
        """Verify MockTicketChecker simulates injected latency accurately."""
        import time
        checker = MockTicketChecker(status="available", latency_ms=50)
        self.assertEqual(checker.latency_ms, 50.0)

        start = time.time()
        res = checker.check_availability("Sivori media")
        elapsed = time.time() - start

        self.assertGreaterEqual(elapsed, 0.04)
        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)

        # Test dynamic latency adjustment with set_latency
        checker.set_latency(0)
        self.assertEqual(checker.latency_ms, 0.0)
        start = time.time()
        checker.check_availability("Sivori media")
        self.assertLess(time.time() - start, 0.03)


if __name__ == "__main__":
    unittest.main()
