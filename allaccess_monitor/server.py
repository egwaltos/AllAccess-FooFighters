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
from allaccess_monitor.models import CheckResult
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
        self.scheduler = PollingScheduler(
            checker=self.checker,
            state_manager=self.state_manager,
            notifier=self.notifier,
            interval_seconds=config.check_interval_seconds,
            section_name=config.target_section,
            config=config,
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
                        "section": service.config.target_section,
                        "interval_seconds": service.config.check_interval_seconds,
                        "total_checks": service.total_checks,
                        "last_status": last.status.value if last else None,
                        "last_available": last.is_available if last else False,
                        "last_check_utc": (
                            datetime.fromtimestamp(service.last_check_epoch, tz=timezone.utc).isoformat()
                            if service.last_check_epoch > 0
                            else None
                        ),
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
