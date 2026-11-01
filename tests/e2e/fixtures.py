"""HTML and mock data builders for AllAccess E2E test scenarios."""
import json
from typing import Optional, List, Dict, Any

def build_allaccess_html(
    section_name: str = "Sivori Media",
    available: bool = True,
    price: Optional[float] = 125000.0,
    service_charge: Optional[float] = 18750.0,
    max_qty: int = 4,
    availability: Optional[int] = 10,
    sector_available: bool = True,
    passcode_required: bool = False,
    show_as_disabled: bool = False,
    extra_sectors: Optional[List[Dict[str, Any]]] = None,
    include_bootstrap: bool = True,
    corrupt_json: bool = False,
    waf_challenge: bool = False,
    queue_it_redirect: bool = False,
    empty_shows: bool = False
) -> str:
    """Build a realistic AllAccess HTML document with Rendr App.bootstrapData payload."""
    if waf_challenge:
        return (
            '<!DOCTYPE html><html><head><title>Captcha Challenge</title></head><body>'
            '<div id="page-captcha">Disculpa, detectamos actividad sospechosa'
            '<div id="request-captcha-container"></div></div></body></html>'
        )

    if queue_it_redirect:
        return (
            '<!DOCTYPE html><html><head><title>Fila Virtual AllAccess</title></head><body>'
            '<div id="queue-view"><h1>Estás en la fila virtual</h1></div></body></html>'
        )

    if not include_bootstrap:
        return '<!DOCTYPE html><html><head><title>AllAccess Event</title></head><body><div>No data</div></body></html>'

    if corrupt_json:
        return '<!DOCTYPE html><html><body><script>App.bootstrapData({"model": {"broken":</script></body></html>'

    section_id = 360261
    sector_id = 689304
    rate_id = 4486883

    # Sivori media sector
    target_sector = {
        "id": sector_id,
        "name": section_name,
        "available": sector_available,
        "rates": [
            {
                "id": rate_id,
                "name": section_name,
                "description": f"Acceso a {section_name}",
                "price": price,
                "serviceChargeAmount": service_charge,
                "availability": availability,
                "available": available,
                "max": max_qty,
                "passcodeRequired": passcode_required,
                "showAsDisabled": show_as_disabled,
                "pickerPrice": f"{price:.2f}" if price is not None else None
            }
        ],
        "sections": [
            {
                "id": section_id,
                "name": section_name,
                "available": available,
                "lowAvailability": False
            }
        ]
    }

    target_sec_avail = {
        "id": section_id,
        "name": section_name,
        "uuid": "509aabf4-24be-4c44-a86f-de570742c1e4",
        "available": available,
        "lowAvailability": False
    }

    sectors = [target_sector]
    sec_avail = [target_sec_avail]

    if extra_sectors:
        sectors.extend(extra_sectors)

    shows = []
    if not empty_shows:
        shows.append({
            "id": 239575,
            "name": "25/02/2027 20:00",
            "available": True,
            "sectionAvailability": sec_avail,
            "sectors": sectors
        })

    model_data = {
        "id": 18337,
        "name": "Foo Fighters",
        "salesStatus": "IN_SALE",
        "onSale": True,
        "shows": shows
    }

    bootstrap_obj = {
        "model": {
            "summary": {"model": "Event", "id": "18337"},
            "data": model_data
        }
    }

    json_str = json.dumps(bootstrap_obj, ensure_ascii=False)
    return (
        '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">'
        '<title>Foo Fighters - AllAccess</title></head><body>'
        '<div id="app"></div>'
        f'<script>App.bootstrapData({json_str}); App.start();</script>'
        '</body></html>'
    )
