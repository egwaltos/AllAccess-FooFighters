"""
Unit tests for CLI interface, argument parser, and module entrypoint (Feature F14).
Tests build_parser flags, validation, main entrypoint execution, exit codes,
cascading configuration precedence, offline dry-run protection, and module execution.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from allaccess_monitor import cli
from allaccess_monitor.models import AvailabilityStatus, CheckResult
from tests.e2e.test_helpers import run_cli_in_process


class TestCLIParser(unittest.TestCase):
    """Unit tests for build_parser flags and validation."""

    def setUp(self):
        self.parser = cli.build_parser()

    def test_parser_defaults(self):
        """Empty arguments list yields expected default values."""
        args = self.parser.parse_args([])
        self.assertIsNone(args.interval)
        self.assertIsNone(args.section)
        self.assertIsNone(args.url)
        self.assertIsNone(args.cooldown)
        self.assertIsNone(args.config)
        self.assertIsNone(args.timeout)
        self.assertIsNone(args.retries)
        self.assertFalse(args.once)
        self.assertFalse(args.dry_run)
        self.assertIsNone(args.mock_status)
        self.assertIsNone(args.channels)
        self.assertIsNone(args.state_file)
        self.assertIsNone(args.log_file)
        self.assertFalse(args.verbose)
        self.assertFalse(args.quiet)

    def test_parser_custom_interval(self):
        """Supports --interval and -i with integer or decimal values."""
        args1 = self.parser.parse_args(["--interval", "45"])
        self.assertEqual(args1.interval, 45)
        args2 = self.parser.parse_args(["-i", "12.5"])
        self.assertEqual(args2.interval, 12.5)

    def test_parser_custom_section(self):
        """Supports --section and -s preserving multi-word section names."""
        args1 = self.parser.parse_args(["--section", "Platea San Martin Inferior"])
        self.assertEqual(args1.section, "Platea San Martin Inferior")
        args2 = self.parser.parse_args(["-s", "Sivori media"])
        self.assertEqual(args2.section, "Sivori media")

    def test_parser_custom_url(self):
        """Supports --url and -u flags."""
        args1 = self.parser.parse_args(["--url", "https://custom.allaccess.com.ar/event/test"])
        self.assertEqual(args1.url, "https://custom.allaccess.com.ar/event/test")
        args2 = self.parser.parse_args(["-u", "https://short.url/event"])
        self.assertEqual(args2.url, "https://short.url/event")

    def test_parser_cooldown(self):
        """Supports --cooldown flag."""
        args = self.parser.parse_args(["--cooldown", "600"])
        self.assertEqual(args.cooldown, 600)

    def test_parser_config(self):
        """Supports --config and -c flags."""
        args1 = self.parser.parse_args(["--config", "/custom/path/.env"])
        self.assertEqual(args1.config, "/custom/path/.env")
        args2 = self.parser.parse_args(["-c", "test.env"])
        self.assertEqual(args2.config, "test.env")

    def test_parser_timeout_and_retries(self):
        """Supports --timeout and --retries flags."""
        args = self.parser.parse_args(["--timeout", "15.5", "--retries", "5"])
        self.assertEqual(args.timeout, 15.5)
        self.assertEqual(args.retries, 5)

    def test_parser_once_flag(self):
        """Supports --once flag."""
        args = self.parser.parse_args(["--once"])
        self.assertTrue(args.once)

    def test_parser_dry_run_flag(self):
        """Supports --dry-run flag."""
        args = self.parser.parse_args(["--dry-run"])
        self.assertTrue(args.dry_run)

    def test_parser_mock_status_valid(self):
        """Accepts canonical mock status choices."""
        for choice in ["available", "sold_out", "section_missing", "network_error", "waf_challenge"]:
            args = self.parser.parse_args(["--mock-status", choice])
            self.assertEqual(args.mock_status, choice)

    def test_parser_mock_status_invalid_raises_exit_2(self):
        """Invalid mock status choice raises parser error and exits with code 2."""
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(["--mock-status", "completely_invalid_choice"])
        self.assertEqual(ctx.exception.code, 2)

    def test_parser_channels(self):
        """Supports --channels comma-separated list."""
        args = self.parser.parse_args(["--channels", "telegram,discord,email"])
        self.assertEqual(args.channels, "telegram,discord,email")

    def test_parser_state_file(self):
        """Supports --state-file flag."""
        args = self.parser.parse_args(["--state-file", "/var/state/monitor.json"])
        self.assertEqual(args.state_file, "/var/state/monitor.json")

    def test_parser_log_file(self):
        """Supports --log-file flag."""
        args = self.parser.parse_args(["--log-file", "/var/log/allaccess.log"])
        self.assertEqual(args.log_file, "/var/log/allaccess.log")

    def test_parser_verbose_and_quiet(self):
        """Supports -v/--verbose and -q/--quiet flags."""
        args_v = self.parser.parse_args(["--verbose"])
        self.assertTrue(args_v.verbose)
        self.assertFalse(args_v.quiet)

        args_q = self.parser.parse_args(["-q"])
        self.assertTrue(args_q.quiet)
        self.assertFalse(args_q.verbose)

    def test_parser_mutually_exclusive_verbose_quiet(self):
        """Specifying both --verbose and --quiet raises parser error."""
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(["--verbose", "--quiet"])
        self.assertEqual(ctx.exception.code, 2)

    def test_parser_unknown_flag_raises_exit_2(self):
        """Unknown flag raises parser error and exits with non-zero code."""
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(["--unknown-random-flag"])
        self.assertEqual(ctx.exception.code, 2)


class TestCLIMainExecution(unittest.TestCase):
    """Unit tests for main() entrypoint execution, exit codes, and workflows."""

    def test_cli_main_help(self):
        """CLI --help prints usage and exits with code 0."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage", stdout.lower())

    def test_cli_main_version(self):
        """CLI --version prints program version and exits with code 0."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--version"])
        self.assertEqual(code, 0)
        self.assertIn("1.0", stdout)

    def test_cli_main_once_available_exit_0(self):
        """CLI --once with available status exits with code 0."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--once", "--mock-status", "available"])
        self.assertEqual(code, 0)
        self.assertIn("AVAILABLE", stdout)

    def test_cli_main_once_sold_out_exit_2(self):
        """CLI --once with sold_out status exits with code 2."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--once", "--mock-status", "sold_out"])
        self.assertEqual(code, 2)
        self.assertTrue("UNAVAILABLE" in stdout or "SOLD OUT" in stdout)

    def test_cli_main_once_section_not_found_exit_2(self):
        """CLI --once with section_not_found status exits with code 2."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--once", "--mock-status", "section_not_found"])
        self.assertEqual(code, 2)
        self.assertIn("SECTION_NOT_FOUND", stdout)

    def test_cli_main_once_network_error_exit_1(self):
        """CLI --once with network error exits with code 1."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--once", "--mock-status", "network_error"])
        self.assertEqual(code, 1)
        self.assertIn("ERROR", stdout)

    def test_cli_main_dry_run_available_stdout(self):
        """CLI --once --dry-run prints DRY RUN header and AVAILABLE status."""
        code, stdout, _ = run_cli_in_process(
            cli.main,
            ["--once", "--dry-run", "--mock-status", "available"]
        )
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", stdout)
        self.assertIn("AVAILABLE", stdout)

    def test_cli_main_dry_run_sold_out_stdout(self):
        """CLI --once --dry-run prints DRY RUN header and UNAVAILABLE status."""
        code, stdout, _ = run_cli_in_process(
            cli.main,
            ["--once", "--dry-run", "--mock-status", "sold_out"]
        )
        self.assertEqual(code, 2)
        self.assertIn("DRY RUN", stdout)
        self.assertTrue("UNAVAILABLE" in stdout or "SOLD OUT" in stdout)

    def test_cli_main_dry_run_prevents_network_calls(self):
        """CLI --dry-run guarantees zero outbound HTTP and SMTP packets."""
        with patch("requests.post") as mock_post, patch("smtplib.SMTP") as mock_smtp:
            code, stdout, _ = run_cli_in_process(
                cli.main,
                ["--once", "--dry-run", "--mock-status", "available", "--channels", "telegram,discord,email"]
            )
            self.assertEqual(code, 0)
            mock_post.assert_not_called()
            mock_smtp.assert_not_called()
            self.assertIn("DRY RUN", stdout)

    def test_cli_main_config_override_precedence(self):
        """CLI arguments take precedence over custom .env file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("POLL_INTERVAL=120\nALLACCESS_TARGET_SECTION=Platea\n", encoding="utf-8")

            parser = cli.build_parser()
            args = parser.parse_args(["--config", str(env_file), "--interval", "15", "--section", "Campo"])
            from allaccess_monitor.config import MonitorConfig
            cfg = MonitorConfig.from_args(args)
            self.assertEqual(cfg.poll_interval, 15)
            self.assertEqual(cfg.target_section, "Campo")

    def test_cli_main_daemon_calls_scheduler_run(self):
        """Continuous mode (without --once) instantiates and runs PollingScheduler."""
        with patch("allaccess_monitor.scheduler.PollingScheduler.run", return_value=1) as mock_run:
            code = cli.main(["--mock-status", "sold_out"])
            self.assertEqual(code, 0)
            mock_run.assert_called_once()

    def test_cli_main_keyboard_interrupt_graceful_exit(self):
        """KeyboardInterrupt (Ctrl+C) in daemon mode exits cleanly with code 0."""
        with patch("allaccess_monitor.scheduler.PollingScheduler.run", side_effect=KeyboardInterrupt):
            code = cli.main(["--mock-status", "sold_out"])
            self.assertEqual(code, 0)

    def test_cli_main_default_sys_argv(self):
        """Calling cli.main() without arguments defaults to sys.argv[1:]."""
        with patch.object(sys, "argv", ["allaccess_monitor", "--once", "--mock-status", "available"]):
            code = cli.main()
            self.assertEqual(code, 0)


class TestModuleEntrypoint(unittest.TestCase):
    """Unit tests for module execution: python -m allaccess_monitor."""

    def test_dunder_main_help(self):
        """Executing python -m allaccess_monitor --help outputs usage and exits 0."""
        proc = subprocess.run(
            [sys.executable, "-m", "allaccess_monitor", "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage", proc.stdout.lower())

    def test_dunder_main_version(self):
        """Executing python -m allaccess_monitor --version outputs version and exits 0."""
        proc = subprocess.run(
            [sys.executable, "-m", "allaccess_monitor", "--version"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("1.0", proc.stdout)


if __name__ == "__main__":
    unittest.main()
