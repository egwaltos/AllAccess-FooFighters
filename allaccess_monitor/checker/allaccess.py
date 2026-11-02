"""
AllAccess HTTP ticket checker and HTML bootstrap data extraction engine.
"""
from datetime import datetime, timezone
import json
import logging
import unicodedata
from typing import Optional, Dict, Any, Tuple, Union, Sequence, List

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
    """
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(text))
    ascii_bytes = nfkd.encode("ASCII", "ignore")
    ascii_str = ascii_bytes.decode("utf-8")
    return " ".join(ascii_str.split()).lower()


def parse_allaccess_sections(
    html_text: str,
    target_sections: Optional[Sequence[str]] = None,
    event_url: str = "",
) -> Dict[str, CheckResult]:
    """
    Pure parser extracting ticket status for multiple venue sections from a single HTML page.
    If target_sections is None, returns results for all discovered sections.
    """
    now = datetime.now(timezone.utc)
    requested = list(target_sections) if target_sections is not None else None

    # Helper to return uniform error results across all requested targets
    def make_error_dict(err_status: AvailabilityStatus, msg: str, code: Optional[int]) -> Dict[str, CheckResult]:
        targets = requested if requested is not None else [""]
        res_map = {}
        for t in targets:
            res_map[t] = CheckResult(
                status=err_status,
                section_name=t,
                event_url=event_url,
                timestamp=now,
                error_message=msg,
                status_code=code,
            )
        return res_map

    try:
        # 1. Locate App.bootstrapData marker
        marker = "App.bootstrapData("
        idx = html_text.find(marker) if isinstance(html_text, str) else -1
        if idx == -1:
            if isinstance(html_text, str) and (
                "request-captcha-container" in html_text
                or "detectamos actividad sospechosa" in html_text
                or "challenge.js" in html_text
            ):
                logger.warning("AWS WAF Captcha challenge detected in HTML.")
                return make_error_dict(
                    AvailabilityStatus.ERROR,
                    "AWS WAF Captcha challenge detected (request-captcha-container)",
                    403,
                )

            if isinstance(html_text, str) and (
                '<div id="queue-view"' in html_text
                or "featureQueueitV2" in html_text
            ):
                logger.warning("Queue-It virtual waiting room active.")
                return make_error_dict(
                    AvailabilityStatus.ERROR,
                    "Queue-It virtual waiting room active",
                    503,
                )

            logger.error("Marker 'App.bootstrapData(' not found in HTML response.")
            return make_error_dict(
                AvailabilityStatus.ERROR,
                "App.bootstrapData marker not found in HTML response",
                200,
            )

        # 2. Decode balanced JSON data starting from marker
        start = idx + len(marker)
        decoder = json.JSONDecoder()
        try:
            bootstrap_data, _ = decoder.raw_decode(html_text[start:])
        except json.JSONDecodeError as exc:
            logger.error("Failed to decode JSON from App.bootstrapData: %s", exc)
            return make_error_dict(
                AvailabilityStatus.ERROR,
                f"JSON decoding error in bootstrapData: {exc}",
                200,
            )

        if not isinstance(bootstrap_data, dict):
            logger.error("Decoded bootstrapData is not a dict: %s", type(bootstrap_data).__name__)
            return make_error_dict(
                AvailabilityStatus.ERROR,
                f"Decoded bootstrapData is not a JSON object/dict (got {type(bootstrap_data).__name__})",
                200,
            )

        # 3. Defensive extraction of model and data
        model = bootstrap_data.get("model")
        if not isinstance(model, dict):
            logger.warning("Missing or non-dict 'model' in bootstrapData.")
            return make_error_dict(AvailabilityStatus.ERROR, "Missing or invalid 'model' in bootstrapData", 200)

        model_data = model.get("data")
        if not isinstance(model_data, dict):
            logger.warning("Missing or non-dict 'data' in model.")
            return make_error_dict(AvailabilityStatus.ERROR, "Missing or invalid 'data' in model", 200)

        event_name = str(model_data.get("name") or "Foo Fighters")
        event_slug = str(model_data.get("slug") or "")
        raw_shows = model_data.get("shows")
        shows = [s for s in raw_shows if isinstance(s, dict)] if isinstance(raw_shows, list) else []
        if not shows:
            logger.warning("No shows found in event data.")
            return make_error_dict(AvailabilityStatus.SECTION_NOT_FOUND, "No shows found in event data", 200)

        # 4. Catalog all sections across all shows
        discovered_sub_sections: Dict[str, List[SectionInfo]] = {}
        discovered_sectors: Dict[str, List[SectionInfo]] = {}
        all_unique_sections: List[SectionInfo] = []
        seen_unique_names = set()

        first_show = shows[0]
        first_show_id = first_show.get("id")
        default_session_name = str(first_show.get("name") or "") or None
        default_session_date = str(first_show.get("date") or "") or None

        def build_checkout_url(show_id_val: Optional[Any]) -> Optional[str]:
            if event_url:
                clean = event_url.rstrip("/")
                if show_id_val is not None and not clean.endswith(str(show_id_val)):
                    return f"{clean}/{show_id_val}"
                return clean
            if event_slug:
                if show_id_val is not None:
                    return f"https://www.allaccess.com.ar/event/{event_slug}/{show_id_val}"
                return f"https://www.allaccess.com.ar/event/{event_slug}"
            return None

        default_checkout_url = build_checkout_url(first_show_id)

        target_show_id: Optional[str] = None
        if event_url:
            parts = event_url.rstrip("/").split("/")
            if parts and parts[-1].isdigit():
                target_show_id = parts[-1]

        for show in shows:
            show_id = show.get("id")
            if target_show_id is not None and str(show_id) != target_show_id:
                continue

            session_name = str(show.get("name") or "") or default_session_name
            session_date = str(show.get("date") or "") or default_session_date
            checkout_url = build_checkout_url(show_id)

            sec_avail_map: Dict[str, bool] = {}
            for sa in (show.get("sectionAvailability") or []):
                if isinstance(sa, dict) and sa.get("name"):
                    sec_avail_map[normalize_section_name(sa["name"])] = bool(sa.get("available", False))

            for sector in (show.get("sectors") or []):
                if not isinstance(sector, dict):
                    continue
                sector_name = sector.get("name")
                norm_sec_name = normalize_section_name(sector_name) if sector_name else ""

                rates = [r for r in (sector.get("rates") or []) if isinstance(r, dict)]
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

                min_r = sector.get("minRate")
                if price is None and isinstance(min_r, dict):
                    if min_r.get("price") is not None:
                        price = _safe_float(min_r.get("price"))
                    if service_charge is None and min_r.get("serviceChargeAmount") is not None:
                        service_charge = _safe_float(min_r.get("serviceChargeAmount"))

                sub_sections = [s for s in (sector.get("sections") or []) if isinstance(s, dict)]

                # 1. Catalog sub-sections
                for sub in sub_sections:
                    sub_name = sub.get("name")
                    norm_sub_name = normalize_section_name(sub_name) if sub_name else ""
                    sub_sec_avail = bool(sub.get("available", False))
                    sa_sub = sec_avail_map.get(norm_sub_name, None)

                    is_sub_avail = rates_available and sub_sec_avail
                    if sa_sub is False:
                        is_sub_avail = False

                    info = SectionInfo(
                        name=str(sub_name or ""),
                        available=is_sub_avail,
                        price=price,
                        currency="ARS",
                        service_charge=service_charge,
                        raw_data=sector,
                        session_name=session_name,
                        session_date=session_date,
                        direct_checkout_url=checkout_url,
                    )
                    discovered_sub_sections.setdefault(norm_sub_name, []).append(info)
                    if norm_sub_name not in seen_unique_names:
                        seen_unique_names.add(norm_sub_name)
                        all_unique_sections.append(info)

                # 2. Catalog parent sector
                sections_available = any(bool(s.get("available", False)) for s in sub_sections) if sub_sections else True
                sa_sec = sec_avail_map.get(norm_sec_name, None)
                is_sec_avail = rates_available and sections_available
                if sa_sec is False:
                    is_sec_avail = False

                info_sec = SectionInfo(
                    name=sector_name,
                    available=is_sec_avail,
                    price=price,
                    currency="ARS",
                    service_charge=service_charge,
                    raw_data=sector,
                    session_name=session_name,
                    session_date=session_date,
                    direct_checkout_url=checkout_url,
                )
                discovered_sectors.setdefault(norm_sec_name, []).append(info_sec)
                if norm_sec_name not in seen_unique_names:
                    seen_unique_names.add(norm_sec_name)
                    all_unique_sections.append(info_sec)

        # 5. Build results for requested sections
        output_results: Dict[str, CheckResult] = {}
        sections_to_process = requested if requested is not None else [s.name for s in all_unique_sections]

        for target_sec in sections_to_process:
            norm_t = normalize_section_name(target_sec)

            # Match preference: Sub-section match > Parent Sector match
            candidates = discovered_sub_sections.get(norm_t) or discovered_sectors.get(norm_t)

            if candidates:
                best_info = next((c for c in candidates if c.available), candidates[0])
                status = AvailabilityStatus.AVAILABLE if best_info.available else AvailabilityStatus.UNAVAILABLE

                output_results[target_sec] = CheckResult(
                    status=status,
                    section_name=target_sec,
                    section_info=best_info,
                    event_name=event_name,
                    event_url=event_url,
                    timestamp=now,
                    status_code=200,
                    session_name=best_info.session_name,
                    session_date=best_info.session_date,
                    direct_checkout_url=best_info.direct_checkout_url,
                    all_sections=all_unique_sections,
                )
            else:
                logger.warning("Section '%s' (normalized: '%s') not found in event layout.", target_sec, norm_t)
                output_results[target_sec] = CheckResult(
                    status=AvailabilityStatus.SECTION_NOT_FOUND,
                    section_name=target_sec,
                    section_info=None,
                    event_name=event_name,
                    event_url=event_url,
                    timestamp=now,
                    error_message=f"Section '{target_sec}' not found in event layout (sectors or sections)",
                    status_code=200,
                    session_name=default_session_name,
                    session_date=default_session_date,
                    direct_checkout_url=default_checkout_url,
                    all_sections=all_unique_sections,
                )

        return output_results

    except Exception as exc:
        logger.exception("Unexpected error parsing AllAccess HTML: %s", exc)
        return make_error_dict(AvailabilityStatus.ERROR, f"Parser error: {exc}", 200)


def parse_allaccess_html(html_text: str, target_section: str, event_url: str = "") -> CheckResult:
    """Backward-compatible single-section parser delegating to parse_allaccess_sections."""
    results = parse_allaccess_sections(html_text, target_sections=[target_section], event_url=event_url)
    return results[target_section]


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

    def check_availability(self, section_name: str, event_url: Optional[str] = None) -> CheckResult:
        """
        Execute an HTTP GET request to AllAccess and parse section availability.
        """
        results = self.check_multi_availability([section_name], event_url=event_url)
        return results[section_name]

    def check_multi_availability(
        self, section_names: Sequence[str], event_url: Optional[str] = None
    ) -> Dict[str, CheckResult]:
        """
        Execute ONE single HTTP GET request to the event URL, and parse all
        requested sections from the response body in memory.
        """
        target_url = event_url or self.event_url
        now = datetime.now(timezone.utc)
        targets = list(section_names) if section_names else [self.event_url]

        def make_http_error(err_msg: str, status_code: Optional[int]) -> Dict[str, CheckResult]:
            return {
                sec: CheckResult(
                    status=AvailabilityStatus.ERROR,
                    section_name=sec,
                    event_url=target_url,
                    timestamp=now,
                    error_message=err_msg,
                    status_code=status_code,
                )
                for sec in targets
            }

        try:
            resp = self.session.get(target_url, timeout=self.timeout)
        except requests.Timeout as exc:
            logger.warning("HTTP request timeout fetching %s: %s", target_url, exc)
            return make_http_error(f"TimeoutError: Request timeout: {exc}", None)
        except requests.RequestException as exc:
            logger.warning("HTTP request exception fetching %s: %s", target_url, exc)
            return make_http_error(f"{type(exc).__name__}: Network error: {exc}", None)
        except Exception as exc:
            logger.warning("Unexpected exception fetching %s: %s", target_url, exc)
            return make_http_error(f"{type(exc).__name__}: {exc}", None)

        # Handle HTTP status codes
        if resp.status_code == 429:
            retry_after = getattr(resp, "headers", {}).get("Retry-After", "unknown") if hasattr(resp, "headers") else "unknown"
            logger.warning("AllAccess returned HTTP 429 Rate Limit. Retry-After: %s", retry_after)
            return make_http_error(f"Rate limit exceeded (HTTP 429). Retry-After: {retry_after}", 429)

        if resp.status_code == 403:
            logger.error("AllAccess returned HTTP 403 Forbidden (Possible WAF IP block).")
            return make_http_error("HTTP 403 Forbidden (Possible AWS WAF block)", 403)

        if resp.status_code >= 500:
            logger.warning("AllAccess server error HTTP %d", resp.status_code)
            return make_http_error(f"AllAccess server error: HTTP {resp.status_code}", resp.status_code)

        if resp.status_code != 200:
            logger.warning("Unexpected HTTP status code: %d", resp.status_code)
            return make_http_error(f"Unexpected HTTP status {resp.status_code}", resp.status_code)

        # Parse valid HTML response
        try:
            return parse_allaccess_sections(resp.text, target_sections=targets, event_url=target_url)
        except Exception as exc:
            logger.exception("Unexpected error parsing AllAccess HTML: %s", exc)
            return make_http_error(f"Parser error: {exc}", 200)

    def check_targets(self, targets: Sequence[Any]) -> List[CheckResult]:
        """
        Group targets by target.event_url, execute ONE HTTP GET per distinct URL,
        and map results back to their respective targets.
        """
        if not targets:
            return []

        from collections import defaultdict
        url_to_sections = defaultdict(list)
        for t in targets:
            url = getattr(t, "event_url", None) or self.event_url
            sec = getattr(t, "target_section", str(t))
            url_to_sections[url].append(sec)

        url_results: Dict[str, Dict[str, CheckResult]] = {}
        for url, sections in url_to_sections.items():
            url_results[url] = self.check_multi_availability(sections, event_url=url)

        results: List[CheckResult] = []
        for t in targets:
            url = getattr(t, "event_url", None) or self.event_url
            sec = getattr(t, "target_section", str(t))
            res = url_results.get(url, {}).get(sec)
            if res is None:
                res = CheckResult(
                    status=AvailabilityStatus.ERROR,
                    section_name=sec,
                    event_url=url,
                    error_message="Target result not found in batch evaluation",
                )
            results.append(res)
        return results

    def close(self) -> None:
        """Close the requests session if created internally."""
        if self._owned_session and self.session:
            self.session.close()
            logger.debug("Closed AllAccess HTTP session.")
