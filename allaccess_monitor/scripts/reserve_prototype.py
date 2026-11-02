"""
Standalone executable prototype demonstrating safe, automated ticket carting and inventory holds on AllAccess.
Defaults to offline mock mode for safe, deterministic verification without real credentials or payment.
"""
import argparse
from datetime import datetime, timezone
import json
import logging
import sys
from typing import List, Optional

from allaccess_monitor.reservation.client import AllAccessCartClient
from allaccess_monitor.reservation.engine import ReservationEngine, ReservationEngineConfig
from allaccess_monitor.reservation.mock_service import MockAllAccessCheckoutService, MockScenario
from allaccess_monitor.reservation.models import ReservationRequest, ReservationStatus

logger = logging.getLogger("allaccess_monitor.reserve")

# ANSI color codes for enhanced terminal output
ANSI_GREEN = "\033[92m"
ANSI_YELLOW = "\033[93m"
ANSI_RED = "\033[91m"
ANSI_CYAN = "\033[96m"
ANSI_BOLD = "\033[1m"
ANSI_RESET = "\033[0m"


def format_currency_ars(amount: Optional[float]) -> str:
    """Format numeric amount into human-readable Argentine Pesos string."""
    if amount is None:
        return "None"
    return f"${amount:,.0f} ARS" if amount.is_integer() else f"${amount:,.2f} ARS"


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for the reservation prototype."""
    parser = argparse.ArgumentParser(
        prog="allaccess-reserve",
        description="Safe Automated Ticket Reservation Prototype for AllAccess Argentina.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-u", "--event-url",
        type=str,
        default="https://www.allaccess.com.ar/event/foo-fighters",
        help="Target event URL on AllAccess.",
    )
    parser.add_argument(
        "-s", "--section",
        type=str,
        default="Sivori media",
        help="Target venue sector name to reserve.",
    )
    parser.add_argument(
        "-q", "--quantity",
        type=int,
        default=2,
        help="Number of tickets to reserve (1 to 4).",
    )
    parser.add_argument(
        "-b", "--max-budget",
        type=float,
        default=None,
        help="Maximum budget ceiling in ARS (aborts if total purchase exceeds this amount).",
    )
    parser.add_argument(
        "--show-id",
        type=int,
        default=239575,
        help="Target show date identifier.",
    )
    parser.add_argument(
        "--rate-id",
        type=int,
        default=4486883,
        help="Target ticketing rate/tier identifier.",
    )
    parser.add_argument(
        "--hold-duration",
        type=int,
        default=600,
        help="Hold duration in seconds for mock reservation (default: 600s/10m).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP request timeout in seconds.",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        default=False,
        help="Immediately release the hold after acquiring and displaying it.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output structured JSON payload of the reservation result.",
    )

    # Mock vs Live execution modes (default is --mock for safety)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--mock",
        action="store_true",
        default=True,
        help="Run against local deterministic MockAllAccessCheckoutService (default).",
    )
    mode_group.add_argument(
        "--live",
        action="store_false",
        dest="mock",
        help="Run against live AllAccess endpoints (WARNING: requires valid session cookies).",
    )

    parser.add_argument(
        "--mock-scenario",
        type=str,
        default="success",
        choices=["success", "sold_out", "rate_limit", "waf_challenge", "expired"],
        help="Mock response scenario to execute.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose debug logging.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="Suppress banner and instructions, printing only essential status.",
    )

    return parser


def render_banner(
    mode: str,
    event_url: str,
    section: str,
    quantity: int,
    unit_price: float,
    service_charge: float,
    cart_id: str,
    status: str,
    hold_duration: int,
    checkout_url: str,
    quiet: bool = False,
) -> None:
    """Render structured ANSI / ASCII terminal card."""
    if quiet:
        print(f"[{status}] CartID: {cart_id} | Checkout: {checkout_url}")
        return

    total = (unit_price + service_charge) * quantity
    total_str = format_currency_ars(total)
    base_str = format_currency_ars(unit_price)
    sc_str = format_currency_ars(service_charge)

    color_status = f"{ANSI_GREEN}{status}{ANSI_RESET}" if status == "RESERVED" else f"{ANSI_RED}{status}{ANSI_RESET}"

    border = "=" * 80
    sep = "-" * 80

    print(f"\n{ANSI_BOLD}{border}{ANSI_RESET}")
    print(f"      {ANSI_BOLD}ALLACCESS TICKET RESERVATION PROTOTYPE (NON-TRANSACTIONAL){ANSI_RESET}")
    print(f"{border}")
    print(f"Mode:         {mode}")
    print(f"Target URL:   {event_url}")
    print(f"Sector:       {section}")
    print(f"Quantity:     {quantity} tickets")
    print(f"Pricing:      Base: {base_str} + Srv: {sc_str} | Total: {total_str}")
    print(f"{sep}")
    print(f"[STATUS]      RESERVATION HOLD ACTIVE - HOLD ACQUIRED ({color_status})")
    print(f"Cart ID:      {cart_id}")
    print(f"Hold Time:    {hold_duration} seconds ({hold_duration // 60}m {hold_duration % 60:02d}s)")
    print(f"Remaining:    {hold_duration}s remaining")
    print()
    print(f"{ANSI_BOLD}DIRECT CHECKOUT LINK (HUMAN-IN-THE-LOOP):{ANSI_RESET}")
    print(f">>> {ANSI_CYAN}{checkout_url}{ANSI_RESET} <<<")
    print()
    print("INSTRUCTIONS FOR USER:")
    print("1. Open the checkout link in your web browser.")
    print("2. Sign in to your AllAccess account and review your reserved seats.")
    print(f"3. Complete payment via bank/credit card before the {hold_duration // 60}-minute hold expires.")
    print()
    print(f"{ANSI_YELLOW}SAFETY GUARANTEE:{ANSI_RESET}")
    print("This prototype executed an inventory hold ONLY.")
    print("No financial transaction was made. No credit card information was handled.")
    print(f"{border}\n")


def main(argv: Optional[List[str]] = None) -> int:
    """
    Main prototype CLI execution entrypoint.

    Returns:
        int: OS exit code:
            0 = Reservation hold successfully established
            1 = Network error, WAF challenge, or timeout
            2 = Tickets sold out / insufficient inventory
            3 = Safety constraint violation (budget exceeded, invalid quantity)
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Safety constraint: quantity enforcement
    if not (1 <= args.quantity <= 4):
        print(f"Error: Quantity must be between 1 and 4 (got {args.quantity}).", file=sys.stderr)
        return 3

    # Initialize Client & Mock Service
    if args.mock:
        mode_label = f"OFFLINE MOCK ({args.mock_scenario.upper()})"
        mock_service = MockAllAccessCheckoutService(
            scenario=args.mock_scenario,
            default_hold_duration_seconds=args.hold_duration,
            sector_name=args.section,
            show_id=args.show_id,
            rate_id=args.rate_id,
        )
        session = mock_service.create_mock_session()
        client = AllAccessCartClient(session=session, timeout_seconds=args.timeout)
    else:
        mode_label = "LIVE ALLACCESS BACKEND"
        client = AllAccessCartClient(timeout_seconds=args.timeout)

    # Initialize Engine and Build Reservation Request
    try:
        engine_config = ReservationEngineConfig(
            max_hold_quantity=args.quantity,
            max_budget=args.max_budget,
        )
        engine = ReservationEngine(client=client, config=engine_config)

        request = ReservationRequest(
            event_url=args.event_url,
            show_id=args.show_id,
            sector_name=args.section,
            rate_id=args.rate_id,
            quantity=args.quantity,
            max_budget_ars=args.max_budget,
        )
    except Exception as exc:
        print(f"Error configuring reservation: {exc}", file=sys.stderr)
        return 3

    # Execute Reservation Hold
    reservation = engine.execute_reservation(request)

    # Handle JSON output mode
    if args.json:
        payload = {
            "cartId": reservation.cart_id,
            "status": reservation.status.value,
            "totalAmount": reservation.total_amount,
            "currency": reservation.currency,
            "expiresAt": reservation.hold_expires_at.isoformat() if reservation.hold_expires_at else None,
            "secondsRemaining": reservation.seconds_remaining,
            "checkoutUrl": reservation.checkout_url,
        }
        print(json.dumps(payload, indent=2))
        return 0 if reservation.status == ReservationStatus.RESERVED else 1

    # Render Terminal Presentation
    if reservation.status == ReservationStatus.RESERVED:
        item = reservation.items[0] if reservation.items else None
        unit_price = item.price if item else 125000.0
        service_charge = item.fee if item else 18750.0

        render_banner(
            mode=mode_label,
            event_url=args.event_url,
            section=args.section,
            quantity=args.quantity,
            unit_price=unit_price,
            service_charge=service_charge,
            cart_id=reservation.cart_id,
            status=reservation.status.value,
            hold_duration=reservation.hold_duration_seconds,
            checkout_url=reservation.checkout_url,
            quiet=args.quiet,
        )

        if args.release:
            released = engine.release_hold(reservation.cart_id)
            status_rel = "SUCCESS" if released else "FAILED"
            print(f"Manual Hold Release: {status_rel} (Cart ID: {reservation.cart_id})")

        return 0

    elif reservation.status == ReservationStatus.FAILED_SOLD_OUT:
        print(f"\n[{ANSI_RED}SOLD OUT{ANSI_RESET}] No tickets available for section '{args.section}'.")
        return 2

    elif reservation.status == ReservationStatus.FAILED_BUDGET_EXCEEDED:
        print(f"\n[{ANSI_RED}BUDGET EXCEEDED{ANSI_RESET}] Total price exceeds maximum budget of {format_currency_ars(args.max_budget)}.")
        return 3

    elif reservation.status == ReservationStatus.FAILED_RATE_LIMITED:
        print(f"\n[{ANSI_YELLOW}RATE LIMITED{ANSI_RESET}] Crowder rate limit encountered. Retry later.")
        return 1

    elif reservation.status == ReservationStatus.FAILED_CHALLENGE:
        print(f"\n[{ANSI_RED}WAF CHALLENGE{ANSI_RESET}] AWS WAF interactive challenge detected.")
        return 1

    else:
        print(f"\n[{ANSI_RED}ERROR{ANSI_RESET}] Reservation failed with status: {reservation.status.value}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
