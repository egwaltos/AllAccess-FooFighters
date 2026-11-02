"""
Empirical Challenge & Adversarial Stress Test Suite for Milestone 1.

Written by Milestone 1 Challenger 1.
Tests the Milestone 1 ticket checker and HTML parser across 5 dimensions:
1. Adversarial Unicode & Diacritic Normalization
2. Deceptive Sector Availability & False-Positive Prevention
3. Sibling Sub-Section Availability Isolation (Anti-False-Positive)
4. WAF Captcha & Waiting Room Detection Resilience
5. Malformed/Corrupted Payloads & Crash Resistance
"""
import copy
import json
import unittest
from unittest.mock import MagicMock

from allaccess_monitor.checker.allaccess import (
    AllAccessTicketChecker,
    normalize_section_name,
    parse_allaccess_html,
)
from allaccess_monitor.models import AvailabilityStatus
from tests.fixtures import load_fixture


class TestAdversarialUnicode(unittest.TestCase):
    """Stress-test normalization and section matching under adversarial Unicode."""

    def test_unicode_combining_characters(self):
        # Decomposed 'i' + combining acute accent (\u0069\u0301)
        decomposed_acute = "S\u0069\u0301vori media"
        self.assertEqual(normalize_section_name(decomposed_acute), "sivori media")

        # Decomposed uppercase 'I' + combining acute accent (\u0049\u0301)
        decomposed_upper = "S\u0049\u0301VORI MEDIA"
        self.assertEqual(normalize_section_name(decomposed_upper), "sivori media")

    def test_unicode_diacritics_variations(self):
        variants = [
            "sivori media",
            "Sivori Media",
            "SIVORI MEDIA",
            "sIvOrI mEdIa",
            "Sívori media",
            "Sívori Media",
            "SÍVORI MEDIA",
            "Sïvori media",
            "Sìvori media",
            "Sîvori media",
            "Sivorí Media",
            "Sívori Médîa",
        ]
        for v in variants:
            with self.subTest(variant=v):
                self.assertEqual(normalize_section_name(v), "sivori media")

    def test_adversarial_whitespace(self):
        whitespace_cases = [
            "   Sivori media   ",
            "Sivori\tmedia",
            "Sivori\nmedia",
            "Sivori\r\nmedia",
            "Sivori\u00A0media",  # Non-breaking space
            "Sivori\u2002media",  # En-space
            "Sivori\u2003media",  # Em-space
            "Sivori\u2009media",  # Thin space
            "Sivori\u3000media",  # Ideographic space
            "  \t\r\n Sivori \u00A0 \u2002 \u2003 media \n\t  ",
        ]
        for ws in whitespace_cases:
            with self.subTest(whitespace_case=repr(ws)):
                self.assertEqual(normalize_section_name(ws), "sivori media")

    def test_zero_and_empty_inputs(self):
        self.assertEqual(normalize_section_name(""), "")
        self.assertEqual(normalize_section_name("   "), "")
        self.assertEqual(normalize_section_name(None), "")

    def test_parser_matches_various_unicode_targets_against_fixture(self):
        html = load_fixture("available_response.html")
        targets = [
            "sivori media",
            "Sivori Media",
            "SIVORI MEDIA",
            "Sívori media",
            "S\u0069\u0301vori media",
            "  Sívori   Media\t",
            "Sivori\u00A0Media",
        ]
        for target in targets:
            with self.subTest(target=target):
                res = parse_allaccess_html(html, target)
                self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)
                self.assertIsNotNone(res.section_info)
                self.assertTrue(res.section_info.available)

    def test_mixed_combining_accents_and_fullwidth(self):
        fullwidth = "\uFF33\uFF49\uFF56\uFF4F\uFF52\uFF49 \uFF4D\uFF45\uFF44\uFF49\uFF41"
        self.assertEqual(normalize_section_name(fullwidth), "sivori media")

        multi_accent = "S\u0301\u0300ivori Media"
        self.assertEqual(normalize_section_name(multi_accent), "sivori media")

        combining_ring = "S\u030Aiv\u00F3ri M\u00E9dia"
        self.assertEqual(normalize_section_name(combining_ring), "sivori media")

        zero_width = "S\u200Biv\u200Cori M\u200Dedia"
        self.assertEqual(normalize_section_name(zero_width), "sivori media")

        soft_hyphen = "Sivo\u00ADri Media"
        self.assertEqual(normalize_section_name(soft_hyphen), "sivori media")

    def test_empty_and_whitespace_target_section_handling(self):
        html = load_fixture("available_response.html")
        empty_targets = ["", "   ", "\t\r\n", None]
        for target in empty_targets:
            with self.subTest(target=repr(target)):
                res = parse_allaccess_html(html, target)
                self.assertEqual(res.status, AvailabilityStatus.SECTION_NOT_FOUND)

    def test_cyrillic_homoglyphs_distinction(self):
        # Cyrillic 'С' (\u0421) looks identical to Latin 'S' (\u0053), but must not match
        cyrillic_target = "\u0421ivori Media"
        self.assertNotEqual(normalize_section_name(cyrillic_target), "sivori media")
        html = load_fixture("available_response.html")
        res = parse_allaccess_html(html, cyrillic_target)
        self.assertEqual(res.status, AvailabilityStatus.SECTION_NOT_FOUND)



class TestDeceptiveSectorAvailabilityTraps(unittest.TestCase):
    """Stress-test false-positive prevention: ensure sector.available is NEVER trusted alone."""

    def setUp(self):
        self.base_html = load_fixture("available_response.html")

    def _replace_rates(self, html: str, rates: list) -> str:
        idx = html.find('"rates": [')
        end_idx = html.find('],', idx) + 1
        return html[:idx] + f'"rates": {json.dumps(rates)}' + html[end_idx:]

    def test_sector_available_true_but_rates_empty(self):
        html = self._replace_rates(self.base_html, [])
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)
        self.assertFalse(res.section_info.available)

    def test_sector_available_true_rates_available_false(self):
        rates = [{
            "id": 1,
            "name": "Sivori Media",
            "price": 125000,
            "available": False,
            "max": 4,
            "availability": 10,
        }]
        html = self._replace_rates(self.base_html, rates)
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_sector_available_true_rates_max_zero(self):
        rates = [{
            "id": 1,
            "name": "Sivori Media",
            "price": 125000,
            "available": True,
            "max": 0,
            "availability": 10,
        }]
        html = self._replace_rates(self.base_html, rates)
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_sector_available_true_rates_max_negative(self):
        rates = [{
            "id": 1,
            "name": "Sivori Media",
            "price": 125000,
            "available": True,
            "max": -1,
            "availability": 10,
        }]
        html = self._replace_rates(self.base_html, rates)
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_sector_available_true_rates_availability_zero(self):
        rates = [{
            "id": 1,
            "name": "Sivori Media",
            "price": 125000,
            "available": True,
            "max": 4,
            "availability": 0,
        }]
        html = self._replace_rates(self.base_html, rates)
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_sector_available_true_rates_availability_negative(self):
        rates = [{
            "id": 1,
            "name": "Sivori Media",
            "price": 125000,
            "available": True,
            "max": 4,
            "availability": -5,
        }]
        html = self._replace_rates(self.base_html, rates)
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_multiple_rates_one_available_one_sold_out(self):
        rates = [
            {
                "id": 1,
                "name": "Sivori Media VIP",
                "price": 200000,
                "available": False,
                "max": 0,
                "availability": 0,
            },
            {
                "id": 2,
                "name": "Sivori Media Regular",
                "price": 125000,
                "available": True,
                "max": 4,
                "availability": 2,
            },
        ]
        html = self._replace_rates(self.base_html, rates)
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)
        self.assertTrue(res.section_info.available)
        self.assertEqual(res.section_info.price, 200000.0)

    def test_section_availability_overrides_rates(self):
        html = self.base_html.replace(
            '"name": "Sivori Media",\n                                        "uuid": "509aabf4-24be-4c44-a86f-de570742c1e4",\n                                        "available": true',
            '"name": "Sivori Media",\n                                        "uuid": "509aabf4-24be-4c44-a86f-de570742c1e4",\n                                        "available": false'
        )
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)
        self.assertFalse(res.section_info.available)

    def test_deceptive_sector_available_false_with_available_rates_and_sections(self):
        # Sector says available: False, but rates and sections are available!
        # Top-level sector["available"] is deceptive and must not suppress true availability.
        html = self.base_html.replace(
            '"name": "Sivori Media",\n                                        "available": true,',
            '"name": "Sivori Media",\n                                        "available": false,'
        )
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)
        self.assertTrue(res.section_info.available)

    def test_sector_available_true_with_null_and_malformed_rates(self):
        malformed_rates = [
            None,
            "not_a_list",
            [None],
            ["string_rate"],
            [{"available": True, "max": "not_a_number"}],
        ]
        for rates in malformed_rates:
            with self.subTest(rates=repr(rates)):
                html = self._replace_rates(self.base_html, rates if isinstance(rates, list) else [])
                res = parse_allaccess_html(html, "Sivori media")
                self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_sector_available_true_rates_availability_none_handled(self):
        rates = [{
            "id": 1,
            "name": "Sivori Media",
            "price": 125000,
            "available": True,
            "max": 4,
            "availability": None,
        }]
        html = self._replace_rates(self.base_html, rates)
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)
        self.assertTrue(res.section_info.available)



class TestSubSectionAvailabilityIsolation(unittest.TestCase):
    """
    CRITICAL CHALLENGE:
    Verify that when a sector contains multiple sub-sections, an available sibling
    sub-section NEVER causes an unavailable target sub-section to be reported as AVAILABLE.
    """

    def test_sibling_subsection_available_does_not_leak_to_unavailable_target(self):
        payload = {
            "model": {
                "data": {
                    "name": "Foo Fighters",
                    "shows": [
                        {
                            "id": 1,
                            "sectionAvailability": [],
                            "sectors": [
                                {
                                    "name": "Platea Sivori",
                                    "available": True,
                                    "rates": [
                                        {
                                            "name": "Platea Sivori General",
                                            "price": 125000,
                                            "available": True,
                                            "max": 4,
                                            "availability": 10,
                                        }
                                    ],
                                    "sections": [
                                        {
                                            "name": "Sivori Media",
                                            "available": False,  # Target is SOLD OUT!
                                        },
                                        {
                                            "name": "Sivori Baja",
                                            "available": True,   # Sibling is available
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            }
        }
        html = f"<html><script>App.bootstrapData({json.dumps(payload)});</script></html>"
        res = parse_allaccess_html(html, "Sivori Media")
        # Must be UNAVAILABLE! An available sibling must NOT leak to Sivori Media.
        self.assertEqual(
            res.status,
            AvailabilityStatus.UNAVAILABLE,
            "False-positive vulnerability: Sivori Media was reported AVAILABLE because sibling Sivori Baja was available!"
        )

    def test_multiple_sibling_subsections_available_target_unavailable(self):
        payload = {
            "model": {
                "data": {
                    "name": "Foo Fighters",
                    "shows": [
                        {
                            "id": 1,
                            "sectors": [
                                {
                                    "name": "Platea Sivori",
                                    "available": True,
                                    "rates": [
                                        {
                                            "name": "General",
                                            "price": 125000,
                                            "available": True,
                                            "max": 4,
                                            "availability": 10,
                                        }
                                    ],
                                    "sections": [
                                        {"name": "Sivori Media", "available": False},
                                        {"name": "Sivori Baja", "available": True},
                                        {"name": "Sivori Alta", "available": True},
                                        {"name": "Sivori VIP", "available": True},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            }
        }
        html = f"<html><script>App.bootstrapData({json.dumps(payload)});</script></html>"
        res = parse_allaccess_html(html, "Sivori Media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)
        self.assertFalse(res.section_info.available)

    def test_target_available_while_all_sibling_subsections_sold_out(self):
        payload = {
            "model": {
                "data": {
                    "name": "Foo Fighters",
                    "shows": [
                        {
                            "id": 1,
                            "sectors": [
                                {
                                    "name": "Platea Sivori",
                                    "available": True,
                                    "rates": [
                                        {
                                            "name": "General",
                                            "price": 125000,
                                            "available": True,
                                            "max": 4,
                                            "availability": 10,
                                        }
                                    ],
                                    "sections": [
                                        {"name": "Sivori Media", "available": True},
                                        {"name": "Sivori Baja", "available": False},
                                        {"name": "Sivori Alta", "available": False},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            }
        }
        html = f"<html><script>App.bootstrapData({json.dumps(payload)});</script></html>"
        res = parse_allaccess_html(html, "Sivori Media")
        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)
        self.assertTrue(res.section_info.available)

    def test_similar_prefix_subsections_do_not_leak(self):
        payload = {
            "model": {
                "data": {
                    "name": "Foo Fighters",
                    "shows": [
                        {
                            "id": 1,
                            "sectors": [
                                {
                                    "name": "Platea Sivori",
                                    "available": True,
                                    "rates": [
                                        {
                                            "name": "General",
                                            "price": 125000,
                                            "available": True,
                                            "max": 4,
                                            "availability": 10,
                                        }
                                    ],
                                    "sections": [
                                        {"name": "Sivori Media", "available": False},
                                        {"name": "Sivori Media VIP", "available": True},
                                        {"name": "Platea Sivori Media", "available": True},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            }
        }
        html = f"<html><script>App.bootstrapData({json.dumps(payload)});</script></html>"
        res = parse_allaccess_html(html, "Sivori Media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_parent_sector_queried_returns_available_if_any_subsection_available(self):
        payload = {
            "model": {
                "data": {
                    "name": "Foo Fighters",
                    "shows": [
                        {
                            "id": 1,
                            "sectors": [
                                {
                                    "name": "Platea Sivori",
                                    "available": True,
                                    "rates": [
                                        {
                                            "name": "General",
                                            "price": 125000,
                                            "available": True,
                                            "max": 4,
                                            "availability": 10,
                                        }
                                    ],
                                    "sections": [
                                        {"name": "Sivori Media", "available": False},
                                        {"name": "Sivori Baja", "available": True},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            }
        }
        html = f"<html><script>App.bootstrapData({json.dumps(payload)});</script></html>"
        res = parse_allaccess_html(html, "Platea Sivori")
        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)

    def test_parent_sector_queried_returns_unavailable_if_all_subsections_sold_out(self):
        payload = {
            "model": {
                "data": {
                    "name": "Foo Fighters",
                    "shows": [
                        {
                            "id": 1,
                            "sectors": [
                                {
                                    "name": "Platea Sivori",
                                    "available": True,
                                    "rates": [
                                        {
                                            "name": "General",
                                            "price": 125000,
                                            "available": True,
                                            "max": 4,
                                            "availability": 10,
                                        }
                                    ],
                                    "sections": [
                                        {"name": "Sivori Media", "available": False},
                                        {"name": "Sivori Baja", "available": False},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            }
        }
        html = f"<html><script>App.bootstrapData({json.dumps(payload)});</script></html>"
        res = parse_allaccess_html(html, "Platea Sivori")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_section_availability_map_overrides_subsection_available_flag(self):
        payload = {
            "model": {
                "data": {
                    "name": "Foo Fighters",
                    "shows": [
                        {
                            "id": 1,
                            "sectionAvailability": [
                                {"name": "Sivori Media", "available": False}
                            ],
                            "sectors": [
                                {
                                    "name": "Platea Sivori",
                                    "available": True,
                                    "rates": [
                                        {
                                            "name": "General",
                                            "price": 125000,
                                            "available": True,
                                            "max": 4,
                                            "availability": 10,
                                        }
                                    ],
                                    "sections": [
                                        {"name": "Sivori Media", "available": True},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            }
        }
        html = f"<html><script>App.bootstrapData({json.dumps(payload)});</script></html>"
        res = parse_allaccess_html(html, "Sivori Media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    def test_multiple_shows_finds_section_in_subsequent_show(self):
        payload = {
            "model": {
                "data": {
                    "name": "Foo Fighters",
                    "shows": [
                        {
                            "id": 1,
                            "name": "25/02/2027",
                            "sectors": [
                                {"name": "Campo", "available": True, "rates": []}
                            ],
                        },
                        {
                            "id": 2,
                            "name": "26/02/2027",
                            "sectors": [
                                {
                                    "name": "Platea Sivori",
                                    "available": True,
                                    "rates": [
                                        {
                                            "name": "General",
                                            "price": 125000,
                                            "available": True,
                                            "max": 4,
                                            "availability": 10,
                                        }
                                    ],
                                    "sections": [
                                        {"name": "Sivori Media", "available": True}
                                    ],
                                }
                            ],
                        },
                    ],
                }
            }
        }
        html = f"<html><script>App.bootstrapData({json.dumps(payload)});</script></html>"
        res = parse_allaccess_html(html, "Sivori Media")
        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)
        self.assertEqual(res.section_info.name, "Sivori Media")



class TestWAFAndEdgeCases(unittest.TestCase):
    """Stress-test WAF challenge detection and tricky HTML edge cases."""

    def test_waf_challenge_variations(self):
        waf_payloads = [
            "<html><body><div id='request-captcha-container'></div></body></html>",
            "<html><body><div>detectamos actividad sospechosa en su conexion</div></body></html>",
            "<html><head><script src='https://cdn.waf.com/challenge.js'></script></head></html>",
        ]
        for payload in waf_payloads:
            with self.subTest(payload=payload):
                res = parse_allaccess_html(payload, "Sivori media")
                self.assertEqual(res.status, AvailabilityStatus.ERROR)
                self.assertEqual(res.status_code, 403)
                self.assertIn("AWS WAF Captcha challenge detected", res.error_message)

    def test_queue_it_variations(self):
        queue_payloads = [
            "<html><body><div id=\"queue-view\">Please wait in line</div></body></html>",
            "<html><head><script>var featureQueueitV2 = true;</script></head></html>",
        ]
        for payload in queue_payloads:
            with self.subTest(payload=payload):
                res = parse_allaccess_html(payload, "Sivori media")
                self.assertEqual(res.status, AvailabilityStatus.ERROR)
                self.assertEqual(res.status_code, 503)
                self.assertIn("Queue-It", res.error_message)

    def test_hidden_captcha_div_with_valid_bootstrap_does_not_trigger_waf(self):
        html = load_fixture("available_response.html")
        self.assertIn("request-captcha-container", html)
        res = parse_allaccess_html(html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)


class TestMalformedAndCorruptedPayloads(unittest.TestCase):
    """
    CRITICAL CHALLENGE:
    Stress-test parser and checker against corrupted, malformed, or hostile inputs.
    The checker and parser MUST NOT crash with unhandled exceptions.
    """

    def test_completely_empty_and_whitespace_html(self):
        for empty_val in ["", "   ", "\n\t\r"]:
            res = parse_allaccess_html(empty_val, "Sivori media")
            self.assertEqual(res.status, AvailabilityStatus.ERROR)
            self.assertIn("marker not found", res.error_message.lower())

    def test_truncated_json_in_bootstrap_data(self):
        truncated_html = "<html><body><script>App.bootstrapData({\"model\": {\"data\":"
        res = parse_allaccess_html(truncated_html, "Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.ERROR)
        self.assertIn("json decoding error", res.error_message.lower())

    def test_bootstrap_data_not_a_dict(self):
        non_dict_cases = [
            "<html><script>App.bootstrapData([]);</script></html>",
            "<html><script>App.bootstrapData(null);</script></html>",
            "<html><script>App.bootstrapData(\"string\");</script></html>",
            "<html><script>App.bootstrapData(12345);</script></html>",
        ]
        for case in non_dict_cases:
            with self.subTest(case=case):
                try:
                    res = parse_allaccess_html(case, "Sivori media")
                    self.assertEqual(res.status, AvailabilityStatus.ERROR)
                except Exception as exc:
                    self.fail(f"Crash on non-dict bootstrap data {case}: {type(exc).__name__}: {exc}")

    def test_bootstrap_data_nested_null_values(self):
        null_structures = [
            "<html><script>App.bootstrapData({\"model\": null});</script></html>",
            "<html><script>App.bootstrapData({\"model\": {\"data\": null}});</script></html>",
            "<html><script>App.bootstrapData({\"model\": {\"data\": {\"shows\": null}}});</script></html>",
            "<html><script>App.bootstrapData({\"model\": {\"data\": {\"shows\": [null]}}});</script></html>",
            "<html><script>App.bootstrapData({\"model\": {\"data\": {\"shows\": [{\"sectors\": null}]}}});</script></html>",
            "<html><script>App.bootstrapData({\"model\": {\"data\": {\"shows\": [{\"sectors\": [null]}]}}});</script></html>",
            "<html><script>App.bootstrapData({\"model\": {\"data\": {\"shows\": [{\"sectors\": [{\"name\": \"Sivori Media\", \"rates\": null}]}]}}});</script></html>",
            "<html><script>App.bootstrapData({\"model\": {\"data\": {\"shows\": [{\"sectors\": [{\"name\": \"Sivori Media\", \"rates\": [null]}]}]}}});</script></html>",
            "<html><script>App.bootstrapData({\"model\": {\"data\": {\"shows\": [{\"sectors\": [{\"name\": \"Sivori Media\", \"rates\": [{\"available\": true, \"max\": null}]}]}]}}});</script></html>",
        ]
        for case in null_structures:
            with self.subTest(case=case):
                try:
                    res = parse_allaccess_html(case, "Sivori media")
                    self.assertIn(res.status, [AvailabilityStatus.ERROR, AvailabilityStatus.UNAVAILABLE, AvailabilityStatus.SECTION_NOT_FOUND])
                except Exception as exc:
                    self.fail(f"Crash on null structure {case}: {type(exc).__name__}: {exc}")

    def test_pricing_type_safety(self):
        base_html = load_fixture("available_response.html")
        invalid_price_html = base_html.replace('"price": 125000', '"price": "not_a_number"')
        try:
            res = parse_allaccess_html(invalid_price_html, "Sivori media")
            self.assertIn(res.status, [AvailabilityStatus.ERROR, AvailabilityStatus.AVAILABLE, AvailabilityStatus.UNAVAILABLE])
        except Exception as exc:
            self.fail(f"Crash on non-numeric price: {type(exc).__name__}: {exc}")

    def test_checker_check_availability_catches_all_parser_exceptions(self):
        # Even if HTML is completely corrupted, checker.check_availability must NOT crash!
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "<html><script>App.bootstrapData(null);</script></html>"
        session.get.return_value = resp

        checker = AllAccessTicketChecker(session=session)
        try:
            res = checker.check_availability("Sivori media")
            self.assertEqual(res.status, AvailabilityStatus.ERROR)
        except Exception as exc:
            self.fail(f"AllAccessTicketChecker.check_availability crashed unhandled: {type(exc).__name__}: {exc}")

    def test_deeply_nested_json_structures_do_not_crash(self):
        nested = {"name": "Sivori Media", "rates": [], "sections": []}
        for _ in range(50):
            nested = {"sub": nested}
        payload = {"model": {"data": {"name": "Foo", "shows": [{"sectors": [nested]}]}}}
        html = f"<html><script>App.bootstrapData({json.dumps(payload)});</script></html>"
        try:
            res = parse_allaccess_html(html, "Sivori Media")
            self.assertIn(res.status, [AvailabilityStatus.SECTION_NOT_FOUND, AvailabilityStatus.ERROR])
        except Exception as exc:
            self.fail(f"Crash on deeply nested JSON: {type(exc).__name__}: {exc}")

    def test_non_string_hostile_types_in_section_names_do_not_crash(self):
        hostile_payloads = [
            {"model": {"data": {"name": "Foo", "shows": [{"sectors": [{"name": 12345, "rates": []}]}]}}},
            {"model": {"data": {"name": "Foo", "shows": [{"sectors": [{"name": "Sivori Media", "sections": [{"name": 99999, "available": True}]}]}]}}},
            {"model": {"data": {"name": "Foo", "shows": [{"sectionAvailability": [{"name": 123, "available": True}], "sectors": []}]}}},
        ]
        for p in hostile_payloads:
            with self.subTest(payload=p):
                html = f"<html><script>App.bootstrapData({json.dumps(p)});</script></html>"
                try:
                    res = parse_allaccess_html(html, "Sivori Media")
                    self.assertIn(res.status, [AvailabilityStatus.SECTION_NOT_FOUND, AvailabilityStatus.ERROR, AvailabilityStatus.UNAVAILABLE])
                except Exception as exc:
                    self.fail(f"Crash on hostile section name types: {type(exc).__name__}: {exc}")

    def test_hostile_pricing_and_rate_structures_do_not_crash(self):
        hostile_rates = [
            {"price": {"complex": 100}, "serviceChargeAmount": [10], "available": True, "max": 4},
            {"price": float("nan"), "serviceChargeAmount": float("inf"), "available": True, "max": 4},
            {"price": -50000, "serviceChargeAmount": -1000, "available": True, "max": 4},
            {"price": "125.000,00", "serviceChargeAmount": "free", "available": True, "max": 4},
        ]
        payload = {
            "model": {
                "data": {
                    "name": "Foo",
                    "shows": [
                        {
                            "sectors": [
                                {
                                    "name": "Sivori Media",
                                    "rates": hostile_rates,
                                    "sections": [{"name": "Sivori Media", "available": True}],
                                }
                            ]
                        }
                    ],
                }
            }
        }
        html = f"<html><script>App.bootstrapData({json.dumps(payload)});</script></html>"
        try:
            res = parse_allaccess_html(html, "Sivori Media")
            self.assertIn(res.status, [AvailabilityStatus.AVAILABLE, AvailabilityStatus.UNAVAILABLE, AvailabilityStatus.ERROR])
        except Exception as exc:
            self.fail(f"Crash on hostile pricing types: {type(exc).__name__}: {exc}")



class TestAllAccessCheckerTransportHarness(unittest.TestCase):
    """Stress-test AllAccessTicketChecker transport layer against abnormal HTTP conditions."""

    def test_http_429_with_and_without_retry_after(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "120"}
        session.get.return_value = resp

        checker = AllAccessTicketChecker(session=session)
        res1 = checker.check_availability("Sivori media")
        self.assertEqual(res1.status, AvailabilityStatus.ERROR)
        self.assertEqual(res1.status_code, 429)
        self.assertIn("120", res1.error_message)

        resp.headers = {}
        res2 = checker.check_availability("Sivori media")
        self.assertEqual(res2.status, AvailabilityStatus.ERROR)
        self.assertEqual(res2.status_code, 429)
        self.assertIn("unknown", res2.error_message)

    def test_http_status_codes_5xx(self):
        session = MagicMock()
        checker = AllAccessTicketChecker(session=session)
        for code in [500, 502, 503, 504]:
            with self.subTest(code=code):
                resp = MagicMock()
                resp.status_code = code
                session.get.return_value = resp
                res = checker.check_availability("Sivori media")
                self.assertEqual(res.status, AvailabilityStatus.ERROR)
                self.assertEqual(res.status_code, code)

    def test_http_exceptions_gracefully_handled(self):
        import requests
        session = MagicMock()
        checker = AllAccessTicketChecker(session=session)

        exceptions = [
            requests.ConnectTimeout("Connection timed out"),
            requests.ReadTimeout("Read timed out"),
            requests.ConnectionError("Failed to establish a new connection"),
            requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead"),
            requests.exceptions.ContentDecodingError("Received corrupt gzip data"),
        ]

        for exc in exceptions:
            with self.subTest(exc=exc):
                session.get.side_effect = exc
                res = checker.check_availability("Sivori media")
                self.assertEqual(res.status, AvailabilityStatus.ERROR)
                self.assertIsNotNone(res.error_message)


if __name__ == "__main__":
    unittest.main()
