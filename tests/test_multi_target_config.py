"""
Comprehensive unit tests for Multi-Target Configuration (Milestone 1).
Verifies MonitoredTarget dataclass invariants, immutability, validation,
serialization, and MonitorConfig multi-target parsing across comma-separated
sectors, events.json, environment variables, CLI arguments, and fallback cascades.
"""
from dataclasses import FrozenInstanceError
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from allaccess_monitor.config import (
    ConfigurationError,
    MonitorConfig,
    derive_event_name_from_url,
    load_config,
    parse_targets_from_file,
    parse_targets_from_json,
)
from allaccess_monitor.models import AvailabilityStatus, CheckResult, MonitoredTarget, SectionInfo


class TestMonitoredTargetModel(unittest.TestCase):
    """Unit tests for MonitoredTarget entity invariants and operations."""

    def test_monitored_target_instantiation_valid(self):
        """MonitoredTarget creates cleanly with valid arguments."""
        t = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/paul-mccartney",
            target_section="Campo Delantero",
            event_name="Paul McCartney",
            max_price=250000.0,
        )
        self.assertEqual(t.event_url, "https://www.allaccess.com.ar/event/paul-mccartney")
        self.assertEqual(t.target_section, "Campo Delantero")
        self.assertEqual(t.event_name, "Paul McCartney")
        self.assertEqual(t.max_price, 250000.0)

    def test_monitored_target_immutability(self):
        """MonitoredTarget is frozen; attempting attribute assignment raises FrozenInstanceError."""
        t = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            target_section="Sivori media",
        )
        with self.assertRaises(FrozenInstanceError):
            t.target_section = "Platea Alta"  # type: ignore

    def test_monitored_target_empty_event_url_raises(self):
        """Empty or whitespace-only event_url raises ValueError."""
        with self.assertRaises(ValueError):
            MonitoredTarget(event_url="", target_section="Sivori")
        with self.assertRaises(ValueError):
            MonitoredTarget(event_url="   ", target_section="Sivori")

    def test_monitored_target_empty_section_raises(self):
        """Empty or whitespace-only target_section raises ValueError."""
        with self.assertRaises(ValueError):
            MonitoredTarget(event_url="https://allaccess.com.ar/event/foo", target_section="")
        with self.assertRaises(ValueError):
            MonitoredTarget(event_url="https://allaccess.com.ar/event/foo", target_section=" \t ")

    def test_monitored_target_non_positive_max_price_raises(self):
        """max_price <= 0 raises ValueError."""
        with self.assertRaises(ValueError):
            MonitoredTarget(
                event_url="https://allaccess.com.ar/event/foo",
                target_section="Sivori",
                max_price=0.0,
            )
        with self.assertRaises(ValueError):
            MonitoredTarget(
                event_url="https://allaccess.com.ar/event/foo",
                target_section="Sivori",
                max_price=-500.0,
            )

    def test_monitored_target_target_id_property(self):
        """target_id returns normalized (event_url.strip(), target_section.strip().lower())."""
        t = MonitoredTarget(
            event_url="  https://www.allaccess.com.ar/event/foo-fighters/  ",
            target_section="  Sivori Media  ",
        )
        self.assertEqual(
            t.target_id,
            ("https://www.allaccess.com.ar/event/foo-fighters/", "sivori media"),
        )

    def test_monitored_target_matches_result(self):
        """matches_result correctly matches corresponding CheckResult and rejects mismatches."""
        t = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            target_section="Sivori media",
        )

        matching_res = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            section_name="Sivori Media",
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
        )
        self.assertTrue(t.matches_result(matching_res))

        different_sec = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            section_name="Platea Alta",
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
        )
        self.assertFalse(t.matches_result(different_sec))

        different_url = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            section_name="Sivori Media",
            event_url="https://www.allaccess.com.ar/event/paul-mccartney",
        )
        self.assertFalse(t.matches_result(different_url))

    def test_monitored_target_nan_and_inf_max_price_rejected(self):
        """MonitoredTarget rejects NaN, inf, and -inf max_price values."""
        for bad_p in [float("nan"), float("inf"), float("-inf"), "nan", "NaN", "inf", "-inf"]:
            with self.subTest(bad_p=bad_p):
                with self.assertRaises(ValueError):
                    MonitoredTarget(
                        event_url="https://allaccess.com.ar/event/foo",
                        target_section="Sivori",
                        max_price=bad_p,
                    )

    def test_monitored_target_matches_result_diacritics_normalization(self):
        """matches_result correctly matches accented and unaccented Spanish section names."""
        target = MonitoredTarget(
            event_url="https://allaccess.com.ar/event/river-plate",
            target_section="Sívori Media",
        )
        # Variants: unaccented, lowercase, uppercase, non-breaking space
        variants = [
            "Sivori Media",
            "sivori media",
            "SÍVORI MEDIA",
            "Sívori Media",
            "  Sívori \u00a0 Media\t\n",
        ]
        for var in variants:
            with self.subTest(variant=var):
                res = CheckResult(
                    status=AvailabilityStatus.AVAILABLE,
                    event_url="https://allaccess.com.ar/event/river-plate",
                    section_name=var,
                )
                self.assertTrue(target.matches_result(res))

        # Check trailing slash tolerance in event_url
        res_slash = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            event_url="https://allaccess.com.ar/event/river-plate/",
            section_name="Sivori Media",
        )
        self.assertTrue(target.matches_result(res_slash))

        # Check distinct section does not match
        res_other = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            event_url="https://allaccess.com.ar/event/river-plate",
            section_name="San Martín",
        )
        self.assertFalse(target.matches_result(res_other))

    def test_monitored_target_to_and_from_dict(self):
        """Serialization to_dict and deserialization from_dict perfectly roundtrips."""
        t1 = MonitoredTarget(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            target_section="Platea Media",
            event_name="Foo Fighters Live",
            max_price=180000.0,
        )
        data = t1.to_dict()
        t2 = MonitoredTarget.from_dict(data)
        self.assertEqual(t1, t2)
        self.assertEqual(data["event_name"], "Foo Fighters Live")
        self.assertEqual(data["max_price"], 180000.0)


class TestConfigMultiTargetParsing(unittest.TestCase):
    """Unit tests for multi-target configuration loading and cascading resolution."""

    def test_single_section_backward_compatibility(self):
        """Single target_section creates MonitorConfig with targets list of length 1."""
        cfg = MonitorConfig(target_section="Sivori media")
        self.assertEqual(len(cfg.targets), 1)
        self.assertEqual(cfg.targets[0].target_section, "Sivori media")
        self.assertEqual(cfg.targets[0].event_url, cfg.event_url)
        self.assertFalse(cfg.is_multi_target)
        self.assertEqual(cfg.target_sections, ["Sivori media"])
        self.assertEqual(cfg.event_urls, [cfg.event_url])

    def test_comma_separated_sectors_parsing(self):
        """Comma-separated target_section creates multiple targets sharing event_url."""
        cfg = MonitorConfig(
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            target_section="Sivori media, Campo delantero, Platea Alta",
        )
        self.assertTrue(cfg.is_multi_target)
        self.assertEqual(len(cfg.targets), 3)
        self.assertEqual(cfg.target_sections, ["Sivori media", "Campo delantero", "Platea Alta"])
        for t in cfg.targets:
            self.assertEqual(t.event_url, "https://www.allaccess.com.ar/event/foo-fighters")

    def test_comma_separated_sectors_whitespace_and_trailing_commas(self):
        """Irregular whitespace, empty entries, and trailing commas are gracefully sanitized."""
        cfg = MonitorConfig(target_section="  Sivori media , , Campo delantero,  ")
        self.assertEqual(len(cfg.targets), 2)
        self.assertEqual(cfg.target_sections, ["Sivori media", "Campo delantero"])

    def test_parse_targets_from_json_flat_list(self):
        """parse_targets_from_json parses a JSON array of targets."""
        payload = json.dumps([
            {"event_url": "https://www.allaccess.com.ar/event/foo-fighters", "target_section": "Sivori media"},
            {"event_url": "https://www.allaccess.com.ar/event/paul-mccartney", "target_section": "Campo", "max_price": 200000.0},
        ])
        targets = parse_targets_from_json(payload)
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0].target_section, "Sivori media")
        self.assertEqual(targets[1].event_name, "Paul Mccartney")
        self.assertEqual(targets[1].max_price, 200000.0)

    def test_parse_targets_from_json_nested_sections(self):
        """parse_targets_from_json parses an event with nested sections array."""
        payload = json.dumps([
            {
                "event_url": "https://www.allaccess.com.ar/event/foo-fighters",
                "event_name": "Foo Fighters River",
                "sections": ["Sivori media", "Platea alta", "Campo general"],
                "max_price": 150000.0,
            }
        ])
        targets = parse_targets_from_json(payload)
        self.assertEqual(len(targets), 3)
        self.assertEqual(targets[0].event_name, "Foo Fighters River")
        self.assertEqual(targets[0].target_section, "Sivori media")
        self.assertEqual(targets[1].target_section, "Platea alta")
        self.assertEqual(targets[2].target_section, "Campo general")
        for t in targets:
            self.assertEqual(t.max_price, 150000.0)

    def test_parse_targets_from_json_keyed_dict(self):
        """parse_targets_from_json accepts a dictionary with a 'targets' or 'events' key."""
        payload = json.dumps({
            "targets": [
                {"event_url": "https://www.allaccess.com.ar/event/foo-fighters", "section": "Platea"}
            ]
        })
        targets = parse_targets_from_json(payload)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].target_section, "Platea")

    def test_parse_targets_from_file_valid(self):
        """parse_targets_from_file loads and parses a valid JSON file."""
        data = [
            {"event_url": "https://www.allaccess.com.ar/event/foo-fighters", "target_section": "Sivori"}
        ]
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            json.dump(data, f)
            temp_path = f.name

        try:
            targets = parse_targets_from_file(temp_path)
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0].target_section, "Sivori")
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def test_parse_targets_from_file_missing_raises(self):
        """parse_targets_from_file raises FileNotFoundError or ConfigurationError on missing file."""
        with self.assertRaises((FileNotFoundError, ConfigurationError)):
            parse_targets_from_file("non_existent_events_file_9999.json")

    def test_parse_targets_from_json_malformed_raises(self):
        """Malformed JSON string raises ValueError."""
        with self.assertRaises(ValueError):
            parse_targets_from_json("{broken json")

    def test_env_var_allaccess_events_json(self):
        """load_config parses ALLACCESS_EVENTS_JSON from environment."""
        payload = json.dumps([
            {"event_url": "https://www.allaccess.com.ar/event/foo-fighters", "target_section": "Sivori media"},
            {"event_url": "https://www.allaccess.com.ar/event/foo-fighters", "target_section": "Campo delantero"},
        ])
        with patch.dict(os.environ, {"ALLACCESS_EVENTS_JSON": payload}):
            cfg = load_config()
            self.assertEqual(len(cfg.targets), 2)
            self.assertEqual(cfg.target_sections, ["Sivori media", "Campo delantero"])

    def test_env_var_allaccess_events_file(self):
        """load_config parses ALLACCESS_EVENTS_FILE from environment."""
        data = [
            {"event_url": "https://www.allaccess.com.ar/event/paul-mccartney", "target_section": "Campo Delantero"}
        ]
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            json.dump(data, f)
            temp_path = f.name

        try:
            with patch.dict(os.environ, {"ALLACCESS_EVENTS_FILE": temp_path}):
                cfg = load_config()
                self.assertEqual(len(cfg.targets), 1)
                self.assertEqual(cfg.targets[0].event_url, "https://www.allaccess.com.ar/event/paul-mccartney")
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def test_derive_event_name_from_url(self):
        """derive_event_name_from_url converts slugs into readable event titles."""
        self.assertEqual(
            derive_event_name_from_url("https://www.allaccess.com.ar/event/foo-fighters"),
            "Foo Fighters",
        )
        self.assertEqual(
            derive_event_name_from_url("https://www.allaccess.com.ar/event/paul-mccartney-2027"),
            "Paul Mccartney 2027",
        )
        self.assertIsNone(derive_event_name_from_url(""))

    def test_validation_rejects_empty_targets(self):
        """validate() flags error if config has empty targets list."""
        cfg = MonitorConfig(target_section="Sivori media")
        object.__setattr__(cfg, "targets", [])
        self.assertFalse(cfg.is_valid())
        self.assertIn("At least one monitored target must be configured.", cfg.validate())

    def test_validation_rejects_invalid_url_in_target(self):
        """validate() flags error if any target has an invalid URL scheme."""
        t_bad = MonitoredTarget(event_url="ftp://invalid.com/event", target_section="Sivori")
        cfg = MonitorConfig(targets=[t_bad])
        self.assertFalse(cfg.is_valid())
        self.assertTrue(any("must start with http" in e for e in cfg.validate()))


if __name__ == "__main__":
    unittest.main()
