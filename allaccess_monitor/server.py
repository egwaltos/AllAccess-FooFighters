"""
Lightweight HTTP Web Server + Background Daemon for Cloud Deployment (Render Free + cron-job.org).

Exposes ultra-lightweight JSON endpoints (<120 bytes) designed for cron-job.org (every 1 minute)
and Render health checks:
  - GET/HEAD /          -> {"status":"ok","service":"allaccess-monitor"}
  - GET/HEAD /health    -> {"status":"ok","service":"allaccess-monitor"}
  - GET      /status    -> Current monitor state & last check summary
  - GET/POST /check     -> Triggers an immediate ticket check and returns compact JSON
  - GET/POST /test-alert -> Sends a simulated availability alert to Telegram/Discord/Email
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Optional
from urllib.parse import urlparse

from allaccess_monitor.checker.allaccess import AllAccessTicketChecker
from allaccess_monitor.checker.mock import MockTicketChecker
from allaccess_monitor.config import MonitorConfig, load_config
from allaccess_monitor.logging_setup import setup_logging
from allaccess_monitor.models import CheckResult, MonitoredTarget
from allaccess_monitor.reservation.client import AllAccessCartClient
from allaccess_monitor.reservation.engine import ReservationEngine, ReservationEngineConfig
from allaccess_monitor.reservation.mock_service import (
    MockAllAccessCheckoutService,
    MockCheckoutHTTPAdapter,
)
from allaccess_monitor.scheduler import PollingScheduler
from allaccess_monitor.state import AlertStateManager

logger = logging.getLogger("allaccess_monitor.server")


class MonitorWebService:
    """Manages the background PollingScheduler and on-demand HTTP check triggers."""

    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self.checker = AllAccessTicketChecker(
            event_url=config.event_url,
            timeout=config.request_timeout_seconds,
            max_retries=config.max_retries,
        )
        self.notifier = config.build_notifier()
        state_path = Path(config.state_file_path) if config.state_file_path else None
        self.state_manager = AlertStateManager(
            cooldown_seconds=config.cooldown_seconds,
            state_file=state_path,
        )

        # Auto-Reserve Session Cookie / Token Hook
        self.user_token = (
            os.getenv("ALLACCESS_USER_TOKEN")
            or os.getenv("ALLACCESS_SESSION_COOKIE")
            or ""
        ).strip()
        raw_qty = os.getenv("AUTO_RESERVE_QUANTITY", "2").strip()
        self.reserve_quantity = min(max(1, int(raw_qty) if raw_qty.isdigit() else 2), 4)

        self.reservation_engine: Optional[ReservationEngine] = None
        if self.user_token:
            cart_client = AllAccessCartClient(timeout=config.request_timeout_seconds)
            res_cfg = ReservationEngineConfig(
                max_hold_quantity=self.reserve_quantity,
                user_token=self.user_token,
                notify_on_reservation=True,
            )
            self.reservation_engine = ReservationEngine(
                client=cart_client,
                config=res_cfg,
                notifier=self.notifier,
            )
            logger.info(
                "Auto-reserve hook ENABLED (quantity=%d, session cookie configured)",
                self.reserve_quantity,
            )

        self.scheduler = PollingScheduler(
            checker=self.checker,
            state_manager=self.state_manager,
            notifier=self.notifier,
            interval_seconds=config.check_interval_seconds,
            section_name=config.target_section,
            config=config,
            reservation_engine=self.reservation_engine,
        )
        self._lock = threading.Lock()
        self.last_result: Optional[CheckResult] = None
        self.last_check_epoch: float = 0.0
        self.total_checks: int = 0
        self._bg_thread: Optional[threading.Thread] = None

    def start_background_loop(self) -> None:
        """Start the continuous background polling loop in a daemon thread."""
        def _run() -> None:
            logger.info(
                "Background polling loop started for '%s' every %ds",
                self.config.target_section,
                self.config.check_interval_seconds,
            )
            while not self.scheduler.is_stopped:
                try:
                    self.run_check(min_interval_seconds=5.0)
                except Exception as exc:
                    logger.exception("Error in background check cycle: %s", exc)
                self.scheduler._shutdown.wait(self.config.check_interval_seconds)

        self._bg_thread = threading.Thread(target=_run, name="allaccess-poller", daemon=True)
        self._bg_thread.start()

    def run_check(self, min_interval_seconds: float = 0.0) -> CheckResult:
        """Execute a thread-safe check tick (deduplicated if called within min_interval_seconds)."""
        with self._lock:
            now = time.time()
            if (
                min_interval_seconds > 0
                and self.last_result is not None
                and (now - self.last_check_epoch) < min_interval_seconds
            ):
                return self.last_result

            result = self.scheduler.run_single_tick()
            self.last_result = result
            self.last_check_epoch = time.time()
            self.total_checks += 1
            return result

    def run_test_alert(self) -> bool:
        """Dispatch a simulated availability notification to configured channels."""
        mock_checker = MockTicketChecker(
            status="available",
            section_name=self.config.target_section,
            event_url=self.config.event_url,
        )
        result = mock_checker.check_availability(self.config.target_section)
        alert = result.to_alert(is_reminder=False)
        return self.notifier.send(alert)

    def run_test_reserve(self) -> dict:
        """Simulate a safe cart hold and dispatch the human-in-the-loop reservation alert."""
        import requests as _requests
        mock_checker = MockTicketChecker(
            status="available",
            section_name=self.config.target_section,
            event_url=self.config.event_url,
        )
        result = mock_checker.check_availability(self.config.target_section)
        mock_svc = MockAllAccessCheckoutService(sector_name=self.config.target_section)
        mock_session = _requests.Session()
        mock_session.mount("https://", MockCheckoutHTTPAdapter(mock_svc))
        mock_session.mount("http://", MockCheckoutHTTPAdapter(mock_svc))
        mock_cart_client = AllAccessCartClient(session=mock_session)
        res_cfg = ReservationEngineConfig(
            max_hold_quantity=self.reserve_quantity,
            user_token=self.user_token or "simulated_session_cookie",
            notify_on_reservation=True,
        )
        test_engine = ReservationEngine(
            client=mock_cart_client,
            config=res_cfg,
            notifier=self.notifier,
        )
        target = MonitoredTarget(
            event_url=self.config.event_url,
            target_section=self.config.target_section,
        )
        reservation = test_engine.reserve_for_target(target, result)
        return {
            "status": "ok" if (reservation and reservation.is_active) else "error",
            "cart_id": reservation.cart_id if reservation else None,
            "checkout_url": reservation.checkout_url if reservation else None,
            "hold_seconds": reservation.seconds_remaining if reservation else 0,
        }


def create_handler(service: MonitorWebService):
    """Create HTTP handler class bound to the MonitorWebService instance."""

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status_code: int, data: dict, head_only: bool = False) -> None:
            body = json.dumps(data, separators=(",", ":")).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def do_HEAD(self) -> None:
            self._handle_request(head_only=True)

        def do_GET(self) -> None:
            self._handle_request(head_only=False)

        def do_POST(self) -> None:
            self._handle_request(head_only=False)

        def _handle_request(self, head_only: bool = False) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path in ("/", "/health"):
                # If last check was >25s ago, trigger an async check without blocking the health response
                if (time.time() - service.last_check_epoch) >= 25.0:
                    threading.Thread(
                        target=service.run_check,
                        kwargs={"min_interval_seconds": 20.0},
                        daemon=True,
                    ).start()
                # Ultra-compact response (<70 bytes) for cron-job.org & Render health checks
                self._send_json(
                    200,
                    {"status": "ok", "service": "allaccess-monitor"},
                    head_only=head_only,
                )
                return

            if path == "/check":
                result = service.run_check(min_interval_seconds=10.0)
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "ticket_status": result.status.value,
                        "available": result.is_available,
                    },
                    head_only=head_only,
                )
                return

            if path == "/status":
                last = service.last_result
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "version": "2.1.0",
                        "section": service.config.target_section,
                        "targets_count": len(service.config.targets),
                        "interval_seconds": service.config.check_interval_seconds,
                        "total_checks": service.total_checks,
                        "last_status": last.status.value if last else None,
                        "last_available": last.is_available if last else False,
                        "last_check_utc": (
                            datetime.fromtimestamp(service.last_check_epoch, tz=timezone.utc).isoformat()
                            if service.last_check_epoch > 0
                            else None
                        ),
                        "auto_reserve": {
                            "enabled": bool(service.user_token),
                            "quantity": service.reserve_quantity,
                            "active_holds": len(service.reservation_engine.active_holds) if service.reservation_engine else 0,
                        },
                        "channels": {
                            "telegram": service.config.telegram.enabled,
                            "discord": service.config.discord.enabled,
                            "email": service.config.email.enabled,
                        },
                    },
                    head_only=head_only,
                )
                return

            if path == "/test-alert":
                sent = service.run_test_alert()
                self._send_json(
                    200 if sent else 500,
                    {"status": "ok" if sent else "error", "alert_sent": sent},
                    head_only=head_only,
                )
                return

            if path == "/test-reserve":
                res_info = service.run_test_reserve()
                self._send_json(
                    200 if res_info.get("status") == "ok" else 500,
                    res_info,
                    head_only=head_only,
                )
                return

            self._send_json(404, {"error": "not_found"}, head_only=head_only)

        def log_message(self, fmt: str, *args) -> None:
            logger.debug("HTTP %s - " + fmt, self.address_string(), *args)

    return Handler


def run_server() -> None:
    """Entrypoint for running the HTTP web server + background monitor daemon."""
    cfg = load_config()
    setup_logging(level=cfg.log_level, log_file=cfg.log_file_path)

    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    service = MonitorWebService(cfg)
    service.start_background_loop()

    server = ThreadingHTTPServer((host, port), create_handler(service))
    logger.info("AllAccess Monitor HTTP Server listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down HTTP server...")
    finally:
        service.scheduler.stop()
        server.server_close()


if __name__ == "__main__":
    run_server()
