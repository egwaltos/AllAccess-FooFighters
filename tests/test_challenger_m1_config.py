"""
Adversarial Challenge & Empirical Verification Test Suite for Milestone 1.
Challenger M1-1 (critic, specialist).

Covers:
1. Dirty & edge-case inputs for multi-target configuration:
   - Malformed JSON, non-object/non-list types, empty arrays.
   - Missing fields (event_url, target_section), whitespace-only strings.
   - Invalid URL schemes (ftp, file, javascript, missing protocol).
   - Negative, zero, non-numeric, and NaN max_price values.
   - Special characters in section names (quotes, emojis, HTML tags, slashes).
2. Accented Spanish sector names & HTML matching:
   - Acute accents (Sívori, Martín), tildes (Ñandú), umlauts (Güemes).
   - Case insensitivity and diacritic-insensitive extraction against HTML fixtures.
   - Target key canonicalization in state tracking.
   - MonitoredTarget.matches_result() diacritic behavior.
3. Precedence cascading:
   - CLI overrides vs env vars vs config files vs defaults.
   - Priority of CLI --events-file and --events-json vs ALLACCESS_EVENTS_JSON.
   - Priority of CLI -s / target_section vs ambient multi-target env vars.
4. Backward compatibility:
   - Single-event legacy env setups and defaults.
   - Comma-separated sectors in legacy target_section.
   - CheckResult alias properties and AlertStateManager v1 state persistence.
"""
import io
import json
import logging
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from allaccess_monitor.models import (
    AvailabilityStatus,
    CheckResult,
    MonitoredTarget,
    SectionInfo,
    TicketAlert,
)
from allaccess_monitor.config import (
    ConfigurationError,
    MonitorConfig,
    derive_event_name_from_url,
    load_config,
    parse_targets_from_file,
    parse_targets_from_json,
)
from allaccess_monitor.checker.allaccess import (
    AllAccessTicketChecker,
    normalize_section_name as normalize_checker_section,
    parse_allaccess_sections,
)
from allaccess_monitor.state import (
    AlertDecision,
    AlertStateManager,
    DEFAULT_TARGET_KEY,
    make_target_key,
    normalize_section_name as normalize_state_section,
)
from allaccess_monitor.scheduler import PollingScheduler


FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestAdversarialDirtyInputs(unittest.TestCase):
    """Stress-test configuration parser with dirty, malformed, and boundary inputs."""

    def test_malformed_json_strings(self):
        """Verify parse_targets_from_json raises ConfigurationError on malformed JSON."""
        dirty_json_samples = [
            "",
            "   ",
            "{unquoted_key: 123}",
            "[{'single_quote': 'val'}]",
            "[{event_url: 'https://test.com'}]",
            "{\"targets\": [}",
            "{\"events\": [{\"event_url\": \"https://test.com\"}]",
            "null",
        ]
        for sample in dirty_json_samples:
            with self.subTest(sample=sample):
                if sample == "null":
                    # Null JSON should return empty list
                    self.assertEqual(parse_targets_from_json("null"), [])
                else:
                    with self.assertRaises((ConfigurationError, ValueError)):
                        parse_targets_from_json(sample)

    def test_non_dict_and_primitive_json_types(self):
        """Verify parse_targets_from_json rejects non-dict/non-list top level types."""
        primitives = [
            "123",
            "12.34",
            "\"plain string\"",
            "true",
            "false",
        ]
        for prim in primitives:
            with self.subTest(prim=prim):
                with self.assertRaises(ConfigurationError):
                    parse_targets_from_json(prim)

    def test_non_dict_elements_in_targets_list(self):
        """Verify targets list containing non-dict items raises ConfigurationError."""
        bad_lists = [
            [123],
            ["string_element"],
            [None],
            [True],
            [3.14],
            [[{"event_url": "https://test.com"}]],
        ]
        for bl in bad_lists:
            with self.subTest(bl=bl):
                with self.assertRaises(ConfigurationError):
                    parse_targets_from_json(bl)

    def test_missing_required_fields(self):
        """Verify target entry without event_url or target_section raises error."""
        # Missing event_url
        with self.assertRaises((ConfigurationError, ValueError)):
            parse_targets_from_json([{"target_section": "Campo General"}])

        # Missing target_section
        with self.assertRaises((ConfigurationError, ValueError)):
            parse_targets_from_json([{"event_url": "https://www.allaccess.com.ar/event/test"}])

        # Empty dict
        with self.assertRaises((ConfigurationError, ValueError)):
            parse_targets_from_json([{}])

    def test_whitespace_only_fields_raise_error(self):
        """Verify whitespace-only strings for event_url or section raise error."""
        # Whitespace event_url
        with self.assertRaises((ConfigurationError, ValueError)):
            parse_targets_from_json([{"event_url": "   ", "sections": "Platea"}])

        # Whitespace section string
        with self.assertRaises((ConfigurationError, ValueError)):
            parse_targets_from_json([{"event_url": "https://allaccess.com.ar/event/test", "sections": "   "}])

        # Whitespace section in list
        with self.assertRaises((ConfigurationError, ValueError)):
            parse_targets_from_json([{"event_url": "https://allaccess.com.ar/event/test", "sections": ["   "]}])

        # Whitespace section in dict list
        with self.assertRaises((ConfigurationError, ValueError)):
            parse_targets_from_json([{
                "event_url": "https://allaccess.com.ar/event/test",
                "sections": [{"name": "   "}],
            }])

    def test_invalid_url_schemes(self):
        """Verify non-HTTP/HTTPS URLs are caught during configuration initialization or validation."""
        invalid_urls = [
            "ftp://allaccess.com.ar/event/foo",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,<b>hi</b>",
            "mailto:admin@allaccess.com.ar",
            "allaccess.com.ar/event/foo",  # missing scheme
        ]
        for url in invalid_urls:
            with self.subTest(url=url):
                try:
                    cfg = MonitorConfig(
                        event_url=url if url.startswith(("http", "ftp")) else "https://allaccess.com.ar/event/foo",
                        target_section="Platea",
                        targets=[MonitoredTarget(event_url=url, target_section="Platea")],
                    )
                    errors = cfg.validate()
                    self.assertTrue(
                        any("must start with http:// or https://" in err for err in errors),
                        f"Expected URL scheme error for '{url}', got errors: {errors}",
                    )
                except ValueError as ve:
                    self.assertIn("Invalid URL scheme", str(ve))

    def test_negative_and_zero_max_prices(self):
        """Verify zero, negative, or non-numeric max_price values are rejected."""
        invalid_prices = [0, 0.0, -1, -50.0, -0.001, "0", "-100", "invalid_price", float("nan"), float("inf"), "nan", "NaN"]
        for bad_p in invalid_prices:
            with self.subTest(bad_p=bad_p):
                # At top level of target
                with self.assertRaises((ConfigurationError, ValueError)):
                    parse_targets_from_json([{
                        "event_url": "https://allaccess.com.ar/event/test",
                        "sections": "Platea",
                        "max_price": bad_p,
                    }])

                # At section level
                with self.assertRaises((ConfigurationError, ValueError)):
                    parse_targets_from_json([{
                        "event_url": "https://allaccess.com.ar/event/test",
                        "sections": [{"name": "Platea", "max_price": bad_p}],
                    }])

    def test_target_dictionary_alias_keys(self):
        """Verify alias keys ('url', 'name', 'section', 'target_sections') parse correctly."""
        data = [
            {
                "url": "https://www.allaccess.com.ar/event/oasis-2025",
                "name": "Oasis Live 2025",
                "section": "Campo Delantero",
                "max_price": 150000.0,
            },
            {
                "event_url": "https://www.allaccess.com.ar/event/coldplay",
                "target_sections": ["Platea Alta", "Platea Baja"],
            },
        ]
        targets = parse_targets_from_json(data)
        self.assertEqual(len(targets), 3)
        self.assertEqual(targets[0].event_url, "https://www.allaccess.com.ar/event/oasis-2025")
        self.assertEqual(targets[0].event_name, "Oasis Live 2025")
        self.assertEqual(targets[0].target_section, "Campo Delantero")
        self.assertEqual(targets[0].max_price, 150000.0)
        self.assertEqual(targets[1].target_section, "Platea Alta")
        self.assertEqual(targets[2].target_section, "Platea Baja")

    def test_special_characters_in_section_names(self):
        """Verify section names with quotes, emojis, slashes, and HTML characters parse cleanly."""
        special_names = [
            "Platea \"Preferencial\" Alta",
            "Campo / General",
            "Sector A & B",
            "Rock & Roll Arena",
            "🎸 Campo VIP 🎸",
            "<b>Sivori</b> Media",
            "Sector: Norte::Subsector 1",
        ]
        targets = parse_targets_from_json([{
            "event_url": "https://allaccess.com.ar/event/special",
            "sections": special_names,
        }])
        self.assertEqual(len(targets), len(special_names))
        for t, expected in zip(targets, special_names):
            self.assertEqual(t.target_section, expected)
            self.assertEqual(t.event_url, "https://allaccess.com.ar/event/special")

    def test_whitespace_in_sections_array_raises_configuration_error(self):
        """Verify whitespace in sections array raises ConfigurationError instead of raw ValueError."""
        with self.assertRaises(ConfigurationError):
            parse_targets_from_json([{"event_url": "https://test.com", "sections": ["   "]}])


class TestAdversarialAccentedSpanishSectors(unittest.TestCase):
    """Stress-test Spanish accents, diacritics normalization, and HTML fixture matching."""

    def setUp(self):
        avail_fixture = FIXTURES_DIR / "available_response.html"
        self.assertTrue(avail_fixture.exists(), f"Fixture missing: {avail_fixture}")
        self.html_content = avail_fixture.read_text(encoding="utf-8")

    def test_diacritic_normalization_helpers(self):
        """Verify Unicode NFKD normalization strips accents consistently across checker and state."""
        samples = [
            ("Sívori Media", "sivori media"),
            ("SÍVORI MEDIA", "sivori media"),
            ("sívori media", "sivori media"),
            ("Sivori Media", "sivori media"),
            ("SIVORI MEDIA", "sivori media"),
            ("Platea San Martín Baja", "platea san martin baja"),
            ("PLATEA SAN MARTÍN BAJA", "platea san martin baja"),
            ("Tribuna Belgrano Ñandú", "tribuna belgrano nandu"),
            ("Sección Güemes", "seccion guemes"),
            ("  Sívori \u00a0 Media\t\n", "sivori media"),
        ]
        for raw, expected in samples:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_checker_section(raw), expected)
                self.assertEqual(normalize_state_section(raw), expected)

    def test_html_parsing_matches_accented_and_unaccented_variants(self):
        """Verify parse_allaccess_sections returns AVAILABLE for all accent/casing permutations."""
        queries = [
            "Sivori Media",      # exact match in fixture
            "SÍVORI MEDIA",      # uppercase with accent
            "Sívori Media",      # title case with accent
            "sívori media",      # lowercase with accent
            "SIVORI MEDIA",      # uppercase plain ASCII
            "sivori media",      # lowercase plain ASCII
            "  Sívori   Media  ",# irregular whitespace
        ]
        results = parse_allaccess_sections(self.html_content, target_sections=queries)
        for q in queries:
            with self.subTest(query=q):
                self.assertIn(q, results)
                res = results[q]
                self.assertEqual(
                    res.status,
                    AvailabilityStatus.AVAILABLE,
                    f"Query '{q}' failed to match available sector",
                )
                self.assertIsNotNone(res.section_info)
                self.assertEqual(res.section_info.price, 125000.0)
                self.assertEqual(res.section_info.service_charge, 18750.0)
                self.assertEqual(res.section_info.total_price, 143750.0)

    def test_html_parsing_with_accented_fixture_data(self):
        """Verify parser functions when the underlying HTML data itself contains accents."""
        # Replace 'Sivori Media' with 'Sívori Media' in HTML
        accented_html = self.html_content.replace("Sivori Media", "Sívori Media")
        queries = ["Sivori Media", "Sívori Media", "SÍVORI MEDIA"]
        results = parse_allaccess_sections(accented_html, target_sections=queries)
        for q in queries:
            with self.subTest(query=q):
                self.assertEqual(results[q].status, AvailabilityStatus.AVAILABLE)

    def test_state_make_target_key_canonicalization(self):
        """Verify make_target_key produces identical keys regardless of accents, slashes, or casing."""
        keys = [
            make_target_key(event_url="https://allaccess.com.ar/event/foo", section_name="Sívori Media"),
            make_target_key(event_url="https://allaccess.com.ar/event/foo/", section_name="Sivori Media"),
            make_target_key(event_url="HTTPS://ALLACCESS.COM.AR/event/foo", section_name="SÍVORI MEDIA"),
            make_target_key(event_url="https://allaccess.com.ar/event/foo", section_name="sivori media"),
            make_target_key(event_url="https://allaccess.com.ar/event/foo", section_name="  Sívori \u00a0 Media  "),
        ]
        expected = "https://allaccess.com.ar/event/foo::sivori media"
        for k in keys:
            self.assertEqual(k, expected)

    def test_monitored_target_matches_result_behavior(self):
        """
        Adversarially evaluate MonitoredTarget.matches_result() against accented variations.
        Documents current empirical behavior where matches_result relies solely on .lower().
        """
        t_accent = MonitoredTarget(event_url="https://allaccess.com.ar/event/foo", target_section="Sívori Media")
        t_plain = MonitoredTarget(event_url="https://allaccess.com.ar/event/foo", target_section="Sivori Media")

        r_accent = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            event_url="https://allaccess.com.ar/event/foo",
            section_name="Sívori Media",
        )
        r_plain = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            event_url="https://allaccess.com.ar/event/foo",
            section_name="Sivori Media",
        )

        # Same casing/accent match:
        self.assertTrue(t_accent.matches_result(r_accent))
        self.assertTrue(t_plain.matches_result(r_plain))

        # Case-insensitive ASCII match:
        r_plain_upper = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            event_url="https://allaccess.com.ar/event/foo",
            section_name="SIVORI MEDIA",
        )
        self.assertTrue(t_plain.matches_result(r_plain_upper))

        # Cross-accent comparison:
        # Verified fix: models.py strips diacritics in matches_result(),
        # so 'Sívori Media' correctly matches 'Sivori Media'.
        matches_cross = t_accent.matches_result(r_plain)
        self.assertTrue(
            matches_cross,
            "MonitoredTarget.matches_result() correctly normalizes Spanish diacritics.",
        )
        self.assertTrue(
            t_plain.matches_result(r_accent),
            "Reverse cross-accent match should also succeed.",
        )


class TestAdversarialPrecedenceCascading(unittest.TestCase):
    """Stress-test configuration cascading: CLI args vs env vars vs config file vs defaults."""

    def test_default_configuration_fallback(self):
        """Verify defaults are loaded when no env vars or overrides are provided."""
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config(enforce_validation=False)
            self.assertEqual(cfg.event_url, "https://www.allaccess.com.ar/event/foo-fighters")
            self.assertEqual(cfg.target_section, "Sivori media")
            self.assertEqual(len(cfg.targets), 1)
            self.assertEqual(cfg.targets[0].event_url, "https://www.allaccess.com.ar/event/foo-fighters")
            self.assertEqual(cfg.targets[0].target_section, "Sivori media")
            self.assertFalse(cfg.is_multi_target)

    def test_legacy_env_vars_override_defaults(self):
        """Verify single-event legacy env vars override default configuration."""
        env = {
            "ALLACCESS_EVENT_URL": "https://www.allaccess.com.ar/event/paul-mccartney",
            "ALLACCESS_TARGET_SECTION": "Campo Trasero",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config(enforce_validation=False)
            self.assertEqual(cfg.event_url, "https://www.allaccess.com.ar/event/paul-mccartney")
            self.assertEqual(cfg.target_section, "Campo Trasero")
            self.assertEqual(len(cfg.targets), 1)
            self.assertEqual(cfg.targets[0].target_section, "Campo Trasero")

    def test_comma_separated_sectors_in_legacy_env(self):
        """Verify comma-separated sectors in ALLACCESS_TARGET_SECTION produce multi-target configuration."""
        env = {
            "ALLACCESS_EVENT_URL": "https://www.allaccess.com.ar/event/foo-fighters",
            "ALLACCESS_TARGET_SECTION": "Sivori Media, Platea Alta, Campo General",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config(enforce_validation=False)
            self.assertTrue(cfg.is_multi_target)
            self.assertEqual(len(cfg.targets), 3)
            self.assertEqual(cfg.target_sections, ["Sivori Media", "Platea Alta", "Campo General"])

    def test_allaccess_events_json_env_overrides_single_event_env(self):
        """Verify ALLACCESS_EVENTS_JSON takes precedence over ALLACCESS_TARGET_SECTION."""
        events_json = json.dumps([
            {"event_url": "https://www.allaccess.com.ar/event/event-a", "sections": "Sec A"},
            {"event_url": "https://www.allaccess.com.ar/event/event-b", "sections": "Sec B"},
        ])
        env = {
            "ALLACCESS_EVENT_URL": "https://www.allaccess.com.ar/event/legacy",
            "ALLACCESS_TARGET_SECTION": "Legacy Section",
            "ALLACCESS_EVENTS_JSON": events_json,
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config(enforce_validation=False)
            self.assertEqual(len(cfg.targets), 2)
            self.assertEqual(cfg.event_urls, [
                "https://www.allaccess.com.ar/event/event-a",
                "https://www.allaccess.com.ar/event/event-b",
            ])

    def test_allaccess_events_file_env_overrides_single_event_env(self):
        """Verify ALLACCESS_EVENTS_FILE takes precedence over single event env vars."""
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            json.dump([
                {"event_url": "https://www.allaccess.com.ar/event/file-event", "sections": "File Sec"},
            ], f)
            f_path = f.name

        try:
            env = {
                "ALLACCESS_EVENT_URL": "https://www.allaccess.com.ar/event/legacy",
                "ALLACCESS_TARGET_SECTION": "Legacy Section",
                "ALLACCESS_EVENTS_FILE": f_path,
            }
            with patch.dict(os.environ, env, clear=True):
                cfg = load_config(enforce_validation=False)
                self.assertEqual(len(cfg.targets), 1)
                self.assertEqual(cfg.targets[0].event_url, "https://www.allaccess.com.ar/event/file-event")
                self.assertEqual(cfg.targets[0].target_section, "File Sec")
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)

    def test_cli_events_file_precedence_inversion_empirical(self):
        """
        Adversarial empirical challenge:
        When a user runs CLI with --events-file (overrides['events_file']),
        does it take precedence over ambient ALLACCESS_EVENTS_JSON in the environment?
        Empirically verifies Finding 1.
        """
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            json.dump([
                {"event_url": "https://www.allaccess.com.ar/event/cli-file", "sections": "CLI Sec"},
            ], f)
            f_path = f.name

        try:
            env = {
                "ALLACCESS_EVENTS_JSON": json.dumps([
                    {"event_url": "https://www.allaccess.com.ar/event/env-json", "sections": "Env Sec"},
                ])
            }
            with patch.dict(os.environ, env, clear=True):
                cfg = load_config(overrides={"events_file": f_path}, enforce_validation=False)
                # In standard cascading, CLI overrides MUST take precedence over environment variables.
                self.assertEqual(cfg.targets[0].event_url, "https://www.allaccess.com.ar/event/cli-file")
                self.assertEqual(cfg.targets[0].target_section, "CLI Sec")
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)

    def test_cli_target_section_override_precedence_inversion_empirical(self):
        """
        Adversarial empirical challenge:
        When a user specifies CLI -s / overrides['target_section'],
        does ambient ALLACCESS_EVENTS_JSON override the CLI flag?
        Empirically verifies Finding 1 part 2.
        """
        env = {
            "ALLACCESS_EVENTS_JSON": json.dumps([
                {"event_url": "https://www.allaccess.com.ar/event/env-json", "sections": "Env Sec"},
            ])
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config(overrides={"target_section": "CLI Preferred Section"}, enforce_validation=False)
            self.assertEqual(cfg.targets[0].target_section, "CLI Preferred Section")

    def test_cli_targets_explicit_override_wins(self):
        """Verify explicit overrides['targets'] cleanly overrides env vars and files."""
        explicit_target = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/explicit",
            target_section="Explicit VIP",
        )
        env = {
            "ALLACCESS_EVENTS_JSON": json.dumps([
                {"event_url": "https://www.allaccess.com.ar/event/env-json", "sections": "Env Sec"},
            ])
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config(overrides={"targets": [explicit_target]}, enforce_validation=False)
            self.assertEqual(len(cfg.targets), 1)
            self.assertEqual(cfg.targets[0].event_url, "https://www.allaccess.com.ar/event/explicit")
            self.assertEqual(cfg.targets[0].target_section, "Explicit VIP")


class TestAdversarialBackwardCompatibility(unittest.TestCase):
    """Stress-test backward compatibility across single-event configurations, models, and state."""

    def test_single_event_monitor_config_construction(self):
        """Verify MonitorConfig backward compatible constructor without targets kwarg."""
        cfg = MonitorConfig(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            target_section="Sivori media",
            check_interval_seconds=60,
            cooldown_seconds=1800,
        )
        self.assertEqual(cfg.event_url, "https://www.allaccess.com.ar/event/foo-fighters")
        self.assertEqual(cfg.target_section, "Sivori media")
        self.assertEqual(len(cfg.targets), 1)
        self.assertEqual(cfg.targets[0].event_url, "https://www.allaccess.com.ar/event/foo-fighters")
        self.assertEqual(cfg.targets[0].target_section, "Sivori media")
        self.assertFalse(cfg.is_multi_target)

    def test_check_result_backward_compatibility(self):
        """Verify CheckResult maintains compatibility across section, section_info, and section_name."""
        info = SectionInfo(name="Platea", available=True, price=90000.0)

        # 1. Using legacy section=SectionInfo
        r1 = CheckResult(status=AvailabilityStatus.AVAILABLE, section=info)
        self.assertEqual(r1.section_name, "Platea")
        self.assertEqual(r1.section_info, info)
        self.assertEqual(r1.section, info)
        self.assertTrue(r1.is_available)

        # 2. Using section_info=SectionInfo
        r2 = CheckResult(status=AvailabilityStatus.AVAILABLE, section_info=info)
        self.assertEqual(r2.section_name, "Platea")
        self.assertEqual(r2.section, info)

        # 3. Using section_name="Platea"
        r3 = CheckResult(status=AvailabilityStatus.AVAILABLE, section_name="Platea")
        self.assertEqual(r3.section_name, "Platea")
        self.assertIsNone(r3.section_info)
        self.assertIsNone(r3.section)

    def test_alert_state_manager_backward_compatible_properties(self):
        """Verify AlertStateManager exposes legacy properties and state transitions without target keys."""
        mgr = AlertStateManager(cooldown_seconds=1800)

        # Initial state
        self.assertFalse(mgr.is_currently_available)
        self.assertFalse(mgr.is_available)
        self.assertIsNone(mgr.last_alert_time)
        self.assertEqual(mgr.consecutive_errors, 0)
        self.assertEqual(mgr.total_alerts_sent, 0)

        # Manual setter compatibility
        mgr.is_currently_available = True
        self.assertTrue(mgr.is_currently_available)
        mgr.last_alert_time = 12345.0
        self.assertEqual(mgr.last_alert_time, 12345.0)
        mgr.consecutive_errors = 3
        self.assertEqual(mgr.consecutive_errors, 3)

        # Reset
        mgr.reset()
        self.assertFalse(mgr.is_currently_available)
        self.assertIsNone(mgr.last_alert_time)

    def test_state_persistence_v1_migration(self):
        """Verify legacy v1 state JSON file round-trips cleanly without loss."""
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            v1_data = {
                "is_currently_available": True,
                "last_alert_time": 1700000000.0,
                "consecutive_errors": 4,
                "total_alerts_sent": 8,
            }
            json.dump(v1_data, f)
            f_path = Path(f.name)

        try:
            mgr = AlertStateManager(state_file=f_path)
            self.assertTrue(mgr.is_currently_available)
            self.assertEqual(mgr.last_alert_time, 1700000000.0)
            self.assertEqual(mgr.consecutive_errors, 4)
            self.assertEqual(mgr.total_alerts_sent, 8)
            # Default target should have been populated
            self.assertIn(DEFAULT_TARGET_KEY, mgr._targets)
        finally:
            if f_path.exists():
                f_path.unlink()

    def test_scheduler_budget_ceiling_cooldown_lockout_empirical(self):
        """
        Adversarial empirical challenge:
        When ticket is available but exceeds max_price on tick 1,
        scheduler suppresses notification.
        If price drops within budget on tick 2, state manager suppresses alert
        under cooldown throttling, causing false lockout (Finding 3).
        """
        class PriceDropChecker:
            def __init__(self):
                self.price = 150000.0
            def check_availability(self, sec, event_url=None):
                info = SectionInfo(name=sec, available=True, price=self.price)
                return CheckResult(
                    status=AvailabilityStatus.AVAILABLE,
                    event_url=event_url or "https://allaccess.com.ar/event/test",
                    section_name=sec,
                    section_info=info,
                )

        checker = PriceDropChecker()
        target = MonitoredTarget("https://allaccess.com.ar/event/test", "Platea", max_price=100000.0)
        notifier = MagicMock()
        notifier.send.return_value = True
        state_mgr = AlertStateManager(cooldown_seconds=1800)
        scheduler = PollingScheduler(
            checker=checker,
            state_manager=state_mgr,
            notifier=notifier,
            targets=[target],
        )

        # Tick 1: Price $150k > max_price $100k -> alert suppressed by scheduler
        scheduler.run_single_tick()
        self.assertEqual(notifier.send.call_count, 0)

        # Tick 2: Price drops to $80k <= max_price $100k -> affordable now!
        checker.price = 80000.0
        scheduler.run_single_tick()

        # With budget ceiling lockout fix, tick 2 immediately dispatches an alert!
        self.assertEqual(
            notifier.send.call_count,
            1,
            "Tickets became affordable within budget and alert was immediately dispatched.",
        )


if __name__ == "__main__":
    unittest.main()
