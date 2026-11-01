"""
AllAccess HTTP ticket checker and HTML bootstrap data extraction engine.
"""
from datetime import datetime, timezone
import json
import logging
import unicodedata
from typing import Optional, Dict, Any, Tuple, Union

import requests

from allaccess_monitor.checker.base import BaseTicketChecker
from allaccess_monitor.models import AvailabilityStatus, CheckResult, SectionInfo

logger = logging.getLogger(__name__)

# Standard browser headers matching Chrome on Windows to prevent AWS WAF blocks
DEFAULT_BROWSER_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _safe_float(val: Any) -> Optional[float]:
    """Safely convert a value to float, returning None on failure or if null."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def normalize_section_name(text: Optional[str]) -> str:
    """
    Normalize section names for robust comparison.
    
    Applies NFKD normalization to strip Spanish accents/diacritics (e.g. 'í' -> 'i'),
    converts to lowercase, and collapses extraneous whitespace.

    Examples:
        'Sivori media' -> 'sivori media'
        'Sívori Media' -> 'sivori media'
        '  SÍVORI   MEDIA  ' -> 'sivori media'
    """
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_bytes = nfkd.encode("ASCII", "ignore")
    ascii_str = ascii_bytes.decode("utf-8")
    return " ".join(ascii_str.split()).lower()


def parse_allaccess_html(html_text: str, target_section: str, event_url: str = "") -> CheckResult:
    """
    Pure parser function to extract ticket status from AllAccess HTML without network I/O.

    Args:
        html_text: Full raw HTML content of the event page.
        target_section: Section name to inspect (e.g. 'Sivori media').
        event_url: Associated event URL for population in CheckResult.

    Returns:
        CheckResult reflecting availability, pricing, or detected errors.
    """
    now = datetime.now(timezone.utc)

    try:
        # 1. Locate App.bootstrapData marker
        marker = "App.bootstrapData("
        idx = html_text.find(marker) if isinstance(html_text, str) else -1
        if idx == -1:
            # If marker is absent, inspect whether this is an active WAF challenge or queue
            if isinstance(html_text, str) and (
                "request-captcha-container" in html_text
                or "detectamos actividad sospechosa" in html_text
                or "challenge.js" in html_text
            ):
                logger.warning("AWS WAF Captcha challenge detected in HTML.")
                return CheckResult(
                    status=AvailabilityStatus.ERROR,
                    section_name=target_section,
                    event_url=event_url,
                    timestamp=now,
                    error_message="AWS WAF Captcha challenge detected (request-captcha-container)",
                    status_code=403,
                )

            if isinstance(html_text, str) and (
                '<div id="queue-view"' in html_text
                or "featureQueueitV2" in html_text
            ):
                logger.warning("Queue-It virtual waiting room active.")
                return CheckResult(
                    status=AvailabilityStatus.ERROR,
                    section_name=target_section,
                    event_url=event_url,
                    timestamp=now,
                    error_message="Queue-It virtual waiting room active",
                    status_code=503,
                )

            logger.error("Marker 'App.bootstrapData(' not found in HTML response.")
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=target_section,
                event_url=event_url,
                timestamp=now,
                error_message="App.bootstrapData marker not found in HTML response",
                status_code=200,
            )

        # 2. Decode balanced JSON data starting from marker
        start = idx + len(marker)
        decoder = json.JSONDecoder()
        try:
            bootstrap_data, _ = decoder.raw_decode(html_text[start:])
        except json.JSONDecodeError as exc:
            logger.error("Failed to decode JSON from App.bootstrapData: %s", exc)
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=target_section,
                event_url=event_url,
                timestamp=now,
                error_message=f"JSON decoding error in bootstrapData: {exc}",
                status_code=200,
            )

        # Defensive check: root must be a dict
        if not isinstance(bootstrap_data, dict):
            logger.error("Decoded bootstrapData is not a dict: %s", type(bootstrap_data).__name__)
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=target_section,
                event_url=event_url,
                timestamp=now,
                error_message=f"Decoded bootstrapData is not a JSON object/dict (got {type(bootstrap_data).__name__})",
                status_code=200,
            )

        # 3. Defensive extraction of model and data
        model = bootstrap_data.get("model")
        if not isinstance(model, dict):
            logger.warning("Missing or non-dict 'model' in bootstrapData.")
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=target_section,
                event_url=event_url,
                timestamp=now,
                error_message="Missing or invalid 'model' in bootstrapData",
                status_code=200,
            )

        model_data = model.get("data")
        if not isinstance(model_data, dict):
            logger.warning("Missing or non-dict 'data' in model.")
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=target_section,
                event_url=event_url,
                timestamp=now,
                error_message="Missing or invalid 'data' in model",
                status_code=200,
            )

        event_name = str(model_data.get("name") or "Foo Fighters")
        raw_shows = model_data.get("shows")
        shows = [s for s in raw_shows if isinstance(s, dict)] if isinstance(raw_shows, list) else []
        if not shows:
            logger.warning("No shows found in event data.")
            return CheckResult(
                status=AvailabilityStatus.SECTION_NOT_FOUND,
                section_name=target_section,
                event_name=event_name,
                event_url=event_url,
                timestamp=now,
                error_message="No shows found in event data",
                status_code=200,
            )

        norm_target = normalize_section_name(target_section)

        # 4. Search shows for target section
        for show in shows:
            # Build lookup for sectionAvailability
            sec_avail_map: Dict[str, bool] = {}
            raw_sa = show.get("sectionAvailability")
            if isinstance(raw_sa, list):
                for sa in raw_sa:
                    if isinstance(sa, dict) and sa.get("name"):
                        sec_avail_map[normalize_section_name(sa["name"])] = bool(sa.get("available", False))

            raw_sectors = show.get("sectors")
            sectors = [s for s in raw_sectors if isinstance(s, dict)] if isinstance(raw_sectors, list) else []

            for sector in sectors:
                sector_name = str(sector.get("name") or "")
                norm_sector_name = normalize_section_name(sector_name)

                raw_sections = sector.get("sections")
                sub_sections = [s for s in raw_sections if isinstance(s, dict)] if isinstance(raw_sections, list) else []

                matches = False
                matched_display_name = sector_name
                matched_sub_sec_avail: Optional[bool] = None

                # 1. First check if any sub-section directly matches the target section
                if sub_sections:
                    for sub_sec in sub_sections:
                        sub_name = sub_sec.get("name")
                        if sub_name and normalize_section_name(sub_name) == norm_target:
                            matches = True
                            matched_display_name = sub_name
                            matched_sub_sec_avail = bool(sub_sec.get("available", False))
                            break

                # 2. If no sub-section matched, check if the parent sector matches
                if not matches and norm_sector_name == norm_target:
                    matches = True
                    matched_display_name = sector_name

                if matches:
                    # Target section identified!
                    raw_rates = sector.get("rates")
                    rates = [r for r in raw_rates if isinstance(r, dict)] if isinstance(raw_rates, list) else []
                    rates_available = False
                    price: Optional[float] = None
                    service_charge: Optional[float] = None

                    for r in rates:
                        max_val = r.get("max")
                        max_ok = isinstance(max_val, (int, float)) and max_val > 0
                        avail_val = r.get("availability")
                        avail_ok = avail_val is None or (isinstance(avail_val, (int, float)) and avail_val > 0)

                        passcode_req = bool(r.get("passcodeRequired", False))
                        show_disabled = bool(r.get("showAsDisabled", False))
                        is_r_avail = bool(r.get("available")) and max_ok and avail_ok and not passcode_req and not show_disabled
                        if is_r_avail:
                            rates_available = True
                        if price is None and r.get("price") is not None:
                            price = _safe_float(r.get("price"))
                        if service_charge is None and r.get("serviceChargeAmount") is not None:
                            service_charge = _safe_float(r.get("serviceChargeAmount"))

                    # Fallback to minRate if rates didn't set price
                    min_r = sector.get("minRate")
                    if price is None and isinstance(min_r, dict):
                        if min_r.get("price") is not None:
                            price = _safe_float(min_r.get("price"))
                        if service_charge is None and min_r.get("serviceChargeAmount") is not None:
                            service_charge = _safe_float(min_r.get("serviceChargeAmount"))

                    # Sibling sub-section isolation:
                    # If target matched a specific sub-section, use its availability.
                    # Otherwise, if sub-sections exist, check if any sub-section is available.
                    # If no sub-sections exist, section availability is True.
                    sections_available = True
                    if matched_sub_sec_avail is not None:
                        sections_available = matched_sub_sec_avail
                    elif sub_sections:
                        sections_available = any(bool(s.get("available", False)) for s in sub_sections)

                    # Cross-check sectionAvailability map if present
                    sa_available = sec_avail_map.get(norm_target, None)

                    # STRICT AVAILABILITY PROTECTION:
                    # Must satisfy rate availability and section availability.
                    # If sectionAvailability explicitly says False, it is unavailable.
                    # Top-level sector["available"] is ignored because it is deceptive.
                    is_available = rates_available and sections_available
                    if sa_available is False:
                        is_available = False

                    sec_info = SectionInfo(
                        name=matched_display_name,
                        available=is_available,
                        price=price,
                        currency="ARS",
                        service_charge=service_charge,
                        raw_data=sector,
                    )

                    status = AvailabilityStatus.AVAILABLE if is_available else AvailabilityStatus.UNAVAILABLE
                    logger.info(
                        "Evaluated section '%s': status=%s, price=%s, rates_avail=%s, sec_avail=%s",
                        matched_display_name,
                        status.value,
                        price,
                        rates_available,
                        sections_available,
                    )

                    return CheckResult(
                        status=status,
                        section_name=target_section,
                        section_info=sec_info,
                        event_name=event_name,
                        event_url=event_url,
                        timestamp=now,
                        status_code=200,
                    )

        # Target section not found in layout
        logger.warning("Section '%s' (normalized: '%s') not found in event layout.", target_section, norm_target)
        return CheckResult(
            status=AvailabilityStatus.SECTION_NOT_FOUND,
            section_name=target_section,
            event_name=event_name,
            event_url=event_url,
            timestamp=now,
            error_message=f"Section '{target_section}' not found in event layout (sectors or sections)",
            status_code=200,
        )

    except Exception as exc:
        logger.exception("Unexpected error parsing AllAccess HTML: %s", exc)
        return CheckResult(
            status=AvailabilityStatus.ERROR,
            section_name=target_section,
            event_url=event_url,
            timestamp=now,
            error_message=f"Parser error: {exc}",
            status_code=200,
        )


class AllAccessTicketChecker(BaseTicketChecker):
    """
    Production HTTP ticket checker for AllAccess events.
    """

    def __init__(
        self,
        event_url: str = "https://www.allaccess.com.ar/event/foo-fighters",
        timeout: Union[float, Tuple[float, float]] = (5.0, 15.0),
        session: Optional[requests.Session] = None,
        headers: Optional[Dict[str, str]] = None,
        max_retries: int = 3,
    ):
        self.event_url = event_url
        self.timeout = timeout
        self.max_retries = max_retries
        self._owned_session = session is None
        self.session = session or requests.Session()
        self._session = self.session  # Backwards compatibility alias for E2E tests

        req_headers = dict(DEFAULT_BROWSER_HEADERS)
        if headers:
            req_headers.update(headers)
        self.session.headers.update(req_headers)

    @property
    def _session(self) -> requests.Session:
        """Alias for self.session for backward compatibility with E2E tests."""
        return self.session

    @_session.setter
    def _session(self, val: requests.Session) -> None:
        self.session = val

    def check_availability(self, section_name: str) -> CheckResult:
        """
        Execute an HTTP GET request to AllAccess and parse section availability.
        """
        now = datetime.now(timezone.utc)
        try:
            resp = self.session.get(self.event_url, timeout=self.timeout)
        except requests.Timeout as exc:
            logger.warning("HTTP request timeout fetching %s: %s", self.event_url, exc)
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=section_name,
                event_url=self.event_url,
                timestamp=now,
                error_message=f"TimeoutError: Request timeout: {exc}",
            )
        except requests.RequestException as exc:
            logger.warning("HTTP request exception fetching %s: %s", self.event_url, exc)
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=section_name,
                event_url=self.event_url,
                timestamp=now,
                error_message=f"{type(exc).__name__}: Network error: {exc}",
            )
        except Exception as exc:
            logger.warning("Unexpected exception fetching %s: %s", self.event_url, exc)
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=section_name,
                event_url=self.event_url,
                timestamp=now,
                error_message=f"{type(exc).__name__}: {exc}",
            )

        # Handle HTTP status codes
        if resp.status_code == 429:
            retry_after = getattr(resp, "headers", {}).get("Retry-After", "unknown") if hasattr(resp, "headers") else "unknown"
            logger.warning("AllAccess returned HTTP 429 Rate Limit. Retry-After: %s", retry_after)
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=section_name,
                event_url=self.event_url,
                timestamp=now,
                error_message=f"Rate limit exceeded (HTTP 429). Retry-After: {retry_after}",
                status_code=429,
            )

        if resp.status_code == 403:
            logger.error("AllAccess returned HTTP 403 Forbidden (Possible WAF IP block).")
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=section_name,
                event_url=self.event_url,
                timestamp=now,
                error_message="HTTP 403 Forbidden (Possible AWS WAF block)",
                status_code=403,
            )

        if resp.status_code >= 500:
            logger.warning("AllAccess server error HTTP %d", resp.status_code)
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=section_name,
                event_url=self.event_url,
                timestamp=now,
                error_message=f"AllAccess server error: HTTP {resp.status_code}",
                status_code=resp.status_code,
            )

        if resp.status_code != 200:
            logger.warning("Unexpected HTTP status code: %d", resp.status_code)
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=section_name,
                event_url=self.event_url,
                timestamp=now,
                error_message=f"Unexpected HTTP status {resp.status_code}",
                status_code=resp.status_code,
            )

        # Parse valid HTML response defensively wrapped in try...except
        try:
            return parse_allaccess_html(resp.text, target_section=section_name, event_url=self.event_url)
        except Exception as exc:
            logger.exception("Unexpected error parsing AllAccess HTML: %s", exc)
            return CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=section_name,
                event_url=self.event_url,
                timestamp=now,
                error_message=f"Parser error: {exc}",
                status_code=200,
            )

    def close(self) -> None:
        """Close the requests session if created internally."""
        if self._owned_session and self.session:
            self.session.close()
            logger.debug("Closed AllAccess HTTP session.")
