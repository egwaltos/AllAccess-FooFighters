"""
CLI entrypoint and argument parser for AllAccess Foo Fighters Ticket Monitor.
Feature F14: Command-Line Interface and Daemon Flags.
Supports one-shot checks (--once), offline dry-runs (--dry-run),
synthetic simulation (--mock-status), and cascading configuration.
"""
import argparse
import logging
from pathlib import Path
import sys
from typing import Optional, List, Union

from allaccess_monitor.checker.allaccess import AllAccessTicketChecker
from allaccess_monitor.checker.mock import MockTicketChecker
from allaccess_monitor.config import MonitorConfig, ConfigurationError
from allaccess_monitor.logging_setup import setup_logging
from allaccess_monitor.models import AvailabilityStatus
from allaccess_monitor.notifier.composite import CompositeNotifier
from allaccess_monitor.notifier.console import ConsoleNotifier
from allaccess_monitor.scheduler import PollingScheduler
from allaccess_monitor.state import AlertStateManager

logger = logging.getLogger("allaccess_monitor.cli")

__version__ = "1.0.0"

VALID_MOCK_STATUSES = [
    "available",
    "sold_out",
    "section_missing",
    "section_not_found",
    "error",
    "network_error",
    "waf_challenge",
    "malformed",
    "rate_limit",
    "unavailable",
]


def _parse_interval(val: str) -> Union[int, float]:
    f = float(val)
    return int(f) if f.is_integer() else f


def build_parser() -> argparse.ArgumentParser:
    """Build and configure argparse CLI parser with all flags."""
    parser = argparse.ArgumentParser(
        prog="allaccess_monitor",
        description="Continuous monitoring daemon for AllAccess Foo Fighters ticket availability.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version="allaccess_monitor 1.0.0",
        help="Show program's version number and exit.",
    )
    parser.add_argument(
        "-i", "--interval",
        type=_parse_interval,
        default=None,
        metavar="SECONDS",
        help="Polling interval in seconds between checks (default: 60.0).",
    )
    parser.add_argument(
        "-s", "--section",
        type=str,
        default=None,
        metavar="NAME",
        help="Target section name to monitor (default: 'Sivori media').",
    )
    parser.add_argument(
        "-u", "--url",
        type=str,
        default=None,
        metavar="URL",
        help="Target event URL on AllAccess.",
    )
    parser.add_argument(
        "--cooldown",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Alert cooldown throttle in seconds (default: 1800).",
    )
    parser.add_argument(
        "-c", "--config",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to custom .env configuration file.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="HTTP request timeout in seconds (default: 10.0).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=None,
        metavar="COUNT",
        help="Maximum HTTP request retries (default: 3).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Execute a single check, dispatch/print alert, and exit immediately.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Perform offline run without dispatching external notifications.",
    )
    parser.add_argument(
        "--mock-status",
        type=str,
        default=None,
        choices=VALID_MOCK_STATUSES,
        metavar="STATUS",
        help=f"Use MockTicketChecker with canned status for simulation: {', '.join(VALID_MOCK_STATUSES)}",
    )
    parser.add_argument(
        "--channels",
        type=str,
        default=None,
        metavar="CHANNELS",
        help="Comma-separated list of enabled channels (e.g. 'telegram,discord,email').",
    )
    parser.add_argument(
        "--state-file",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to JSON state file for cross-process alert persistence.",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to write rotating log file with automated secret redaction.",
    )

    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose debug logging.",
    )
    verbosity_group.add_argument(
        "-q", "--quiet",
        action="store_true",
        default=False,
        help="Suppress informational output, logging only warnings and errors.",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """
    Main application CLI entrypoint.

    Args:
        argv: List of command-line argument strings (defaults to sys.argv[1:]).

    Returns:
        int: OS exit code:
            0 = Available tickets detected (--once) or clean shutdown
            1 = Check error / network error / configuration error
            2 = Unavailable tickets / Section not found / parser error
    """
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    # 1. Structured Logging Initialization
    log_level = "DEBUG" if args.verbose else ("WARNING" if args.quiet else "INFO")
    setup_logging(
        level=log_level,
        log_file=args.log_file,
        verbose=args.verbose,
        quiet=args.quiet,
    )

    # 2. Configuration Loading & Cascading (CLI > .env > defaults)
    try:
        cfg = MonitorConfig.from_args(args)
    except ConfigurationError as err:
        logger.error("Configuration error: %s", err)
        print(f"Configuration error: {err}", file=sys.stderr)
        return 1
    except Exception as err:
        logger.exception("Failed to load configuration: %s", err)
        print(f"Error loading configuration: {err}", file=sys.stderr)
        return 1

    # Apply explicit CLI overrides
    if args.dry_run:
        cfg.dry_run = True

    if args.channels:
        requested = {c.strip().lower() for c in args.channels.split(",") if c.strip()}
        cfg.telegram.enabled = "telegram" in requested
        cfg.discord.enabled = "discord" in requested
        cfg.email.enabled = ("email" in requested or "smtp" in requested)

    if args.state_file:
        cfg.state_file_path = args.state_file

    # 3. Instantiate TicketChecker
    if args.mock_status or cfg.mock_status:
        chosen_status = args.mock_status or cfg.mock_status
        checker = MockTicketChecker(
            status=chosen_status,
            section_name=cfg.target_section,
            event_url=cfg.event_url,
        )
    else:
        checker = AllAccessTicketChecker(
            event_url=cfg.event_url,
            timeout=cfg.request_timeout_seconds,
            max_retries=cfg.max_retries,
        )

    # 4. Instantiate Notifier
    if args.dry_run or cfg.dry_run:
        notifier = ConsoleNotifier()
    else:
        notifier = cfg.build_notifier()

    # 5. Instantiate State Manager
    state_file = Path(cfg.state_file_path) if cfg.state_file_path else None
    state_manager = AlertStateManager(
        cooldown_seconds=cfg.cooldown_seconds,
        state_file=state_file,
    )

    # 6. Instantiate Polling Scheduler
    scheduler = PollingScheduler(
        checker=checker,
        state_manager=state_manager,
        notifier=notifier,
        interval_seconds=cfg.poll_interval,
        section_name=cfg.target_section,
        config=cfg,
    )

    # 7. Execution Mode: One-shot (--once) vs Continuous Daemon
    if args.once or cfg.once:
        is_dry = bool(args.dry_run or cfg.dry_run)
        prefix = "[DRY RUN] " if is_dry else ""
        if is_dry:
            print("[DRY RUN] Running in dry-run mode: external notification dispatch is disabled.")

        result = scheduler.run_single_tick()

        if result.status == AvailabilityStatus.AVAILABLE:
            price_info = result.section_info.formatted_price if result.section_info else "N/A"
            print(f"{prefix}Status: AVAILABLE - Tickets found for section '{cfg.target_section}'! Price: {price_info}")
            return 0
        elif result.status in (AvailabilityStatus.UNAVAILABLE, AvailabilityStatus.SECTION_NOT_FOUND):
            status_desc = "UNAVAILABLE / SOLD OUT" if result.status == AvailabilityStatus.UNAVAILABLE else "SECTION_NOT_FOUND"
            print(f"{prefix}Status: {status_desc} - No tickets available for section '{cfg.target_section}'.")
            return 2
        else:
            # AvailabilityStatus.ERROR
            print(f"{prefix}Status: ERROR - {result.error_message or 'Check error'}")
            return 1

    # Continuous Daemon Mode
    logger.info(
        "Starting ticket monitor daemon for section '%s' (interval: %ds, cooldown: %ds)",
        cfg.target_section,
        cfg.check_interval_seconds,
        cfg.cooldown_seconds,
    )
    try:
        scheduler.run()
        return 0
    except KeyboardInterrupt:
        logger.info("Monitor received keyboard interrupt (Ctrl+C). Shutting down.")
        return 0
    except Exception as exc:
        logger.exception("Fatal error in monitor loop: %s", exc)
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1
