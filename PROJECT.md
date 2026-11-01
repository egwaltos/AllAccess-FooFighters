# Project: AllAccess Foo Fighters Ticket Monitor

## Architecture
The application is a Python-based continuous monitoring daemon with modular components designed for high reliability, zero false positives, sub-millisecond signal responsiveness, and multi-channel alerting.

```
                      ┌──────────────────┐
                      │     User CLI     │ (python -m allaccess_monitor)
                      └────────┬─────────┘
                               │
                      ┌────────▼─────────┐
                      │      cli.py      │ (argparse, loads .env, builds config)
                      └────────┬─────────┘
                               │
                      ┌────────▼─────────┐
                      │ PollingScheduler │
                      └────────┬─────────┘
                               │
       ┌───────────────────────┴───────────────────────┐
       │ Tick Execution                                │
       ▼                                               ▼
 ┌───────────────────────┐                   ┌───────────────────┐
 │     TicketChecker     │                   │  GracefulShutdown │
 │ (AllAccess / Mock)    │                   │ (threading.Event) │
 └──────────┬────────────┘                   └───────────────────┘
            │ returns CheckResult
            ▼
 ┌───────────────────────┐
 │   AlertStateManager   │
 │ (Edge & Cooldown)     │
 └──────────┬────────────┘
            │ should_alert == True?
            ├──────────────────────────────┐
            │ Yes                          │ No
            ▼                              ▼
 ┌───────────────────────┐         ┌───────────────────────────────┐
 │   CompositeNotifier   │         │ Log status & wait next cycle  │
 │ (Telegram/Discord/    │         │ (Standard interval or backoff)│
 │  Email/Console)       │         └───────────────────────────────┘
 └───────────────────────┘
```

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | F1: Bootstrap Extraction | Extract inline `App.bootstrapData` JSON from AllAccess HTML page without fragile full-page regex | M1 | Survey Explorer 1 |
| 2 | F2: Unicode & Accent Normalization | NFKD normalization matching "Sivori media" against "Sivori Media", "Sívori media", etc. | M1 | Survey Explorer 1 |
| 3 | F3: Availability & False-Positive Filter | Strict check: both rate availability (`available: True`, `max > 0`) and section availability (`available: True`), rejecting deceptive sector-level `available: True` flags | M1 | Survey Explorer 1 |
| 4 | F4: Pricing Extraction | Parse ticket price and service charge from sector rate information | M1 | Survey Explorer 1 |
| 5 | F5: WAF & Challenge Detection | Detect AWS WAF / Captcha blocks (`request-captcha-container`) and raise distinct exceptions | M1 | Survey Explorer 1 |
| 6 | F6: Telegram Notifier | Deliver alerts via Telegram Bot API with HTML parse mode and link to purchase | M2 | Survey Explorer 2 |
| 7 | F7: Discord Notifier | Deliver alerts via Discord Webhook with rich embeds and status fields | M2 | Survey Explorer 2 |
| 8 | F8: Email Notifier | Deliver alerts via SMTP (STARTTLS port 587 and SSL port 465) with multipart HTML + text | M2 | Survey Explorer 2 |
| 9 | F9: Composite Multiplexer | Multiplex alerts across all enabled channels with per-channel error isolation | M2 | Survey Explorer 2 |
| 10 | F10: State Machine & Cooldown Throttling | Immediate alert on `UNAVAILABLE -> AVAILABLE`, suppress duplicates during cooldown, reset on `UNAVAILABLE` | M2 | Survey Explorer 2 |
| 11 | F11: Configuration & Secret Masking | Cascading config (CLI > .env > defaults) with automatic credential redaction in `__repr__` and logs | M1 | Survey Explorer 2 & 3 |
| 12 | F12: Responsive Polling Scheduler | Main loop using `threading.Event().wait()` for sub-millisecond graceful shutdown on Windows & POSIX | M3 | Survey Explorer 3 |
| 13 | F13: Exponential Backoff & Jitter | Exponential backoff with full jitter and `Retry-After` header parsing on HTTP 429/5xx | M3 | Survey Explorer 3 |
| 14 | F14: CLI Interface & Daemon Flags | `argparse` CLI with `--interval`, `--section`, `--url`, `--once`, `--dry-run`, `--mock-status` | M3 | Survey Explorer 3 |
| 15 | F15: Mock & Simulation Fixtures | 5 canonical test fixtures (`AVAILABLE`, `UNAVAILABLE`, `SECTION_MISSING`, `NETWORK_ERROR`, `MALFORMED`) | M3 | Survey Explorer 3 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | M1: Core Domain, Config & Ticket Checker | `models.py`, `config.py`, `logging_setup.py`, `checker/base.py`, `checker/allaccess.py`, `checker/mock.py`, parser & checker unit tests | none | DONE |
| 2 | M2: Notifiers & State Throttler | `notifier/base.py`, `notifier/telegram.py`, `notifier/discord.py`, `notifier/email.py`, `notifier/console.py`, `notifier/composite.py`, `state.py`, notifier & state unit tests | M1 | DONE |
| 3 | M3: Polling Scheduler, CLI Daemon & Fixtures | `scheduler.py`, `cli.py`, `__main__.py`, `__init__.py`, `.env.example`, `README.md`, 5 test fixtures, scheduler & CLI unit tests | M1, M2 | PLANNED |
| 4 | M4: Final Milestone (E2E Pass & Adversarial Hardening) | Phase 1: 100% pass of E2E test suite (Tiers 1-4). Phase 2: Adversarial coverage hardening (Tier 5) | M3, E2E Track | PLANNED |

## Interface Contracts

### Domain Models (`models.py`)
```python
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any

class AvailabilityStatus(Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    SECTION_NOT_FOUND = "SECTION_NOT_FOUND"
    ERROR = "ERROR"

@dataclass(frozen=True)
class SectionInfo:
    name: str
    available: bool
    price: Optional[float] = None
    currency: str = "ARS"
    service_charge: Optional[float] = None
    raw_data: Optional[Dict[str, Any]] = None

@dataclass(frozen=True)
class CheckResult:
    status: AvailabilityStatus
    section_name: str
    section_info: Optional[SectionInfo] = None
    event_name: str = "Foo Fighters"
    event_url: str = "https://www.allaccess.com.ar/event/foo-fighters"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_message: Optional[str] = None
    status_code: Optional[int] = None

@dataclass(frozen=True)
class TicketAlert:
    event_name: str
    section_name: str
    status: str                         # "AVAILABLE" | "REMINDER"
    event_url: str
    price: Optional[float] = None
    currency: str = "ARS"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_reminder: bool = False
```

### Checker Interface (`checker/base.py`)
```python
from abc import ABC, abstractmethod
from allaccess_monitor.models import CheckResult

class BaseTicketChecker(ABC):
    @abstractmethod
    def check_availability(self, section_name: str) -> CheckResult:
        """Fetch and evaluate ticket availability for the target section."""
        pass
```

### Notifier Interface (`notifier/base.py`)
```python
from abc import ABC, abstractmethod
from allaccess_monitor.models import TicketAlert

class BaseNotifier(ABC):
    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Identifier name of the notification channel."""
        pass

    @abstractmethod
    def send(self, alert: TicketAlert) -> bool:
        """Deliver alert to channel. Returns True on success, False on failure."""
        pass
```

### State Throttler Interface (`state.py`)
```python
@dataclass
class AlertDecision:
    should_alert: bool
    is_reminder: bool = False
    reason: str = ""

class AlertStateManager:
    def __init__(self, cooldown_seconds: int = 1800, state_file: Optional[Path] = None):
        ...
    def process_check_result(self, is_available: bool, current_time: Optional[float] = None) -> AlertDecision:
        ...
```

## Code Layout
```
allaccess_monitor/
├── __init__.py               # Package metadata and version definition
├── __main__.py               # Direct execution entrypoint: python -m allaccess_monitor
├── cli.py                    # Argument parsing, flag validation, CLI entrypoint
├── config.py                 # Dataclasses, .env loader, validation, secret masking
├── models.py                 # Domain models (AvailabilityStatus, SectionInfo, CheckResult, TicketAlert)
├── checker/
│   ├── __init__.py
│   ├── base.py               # Abstract BaseChecker protocol
│   ├── allaccess.py          # Real HTTP fetcher, Rendr bootstrap extractor, section availability evaluator
│   └── mock.py               # MockTicketChecker for simulation & test suite
├── notifier/
│   ├── __init__.py
│   ├── base.py               # Abstract BaseNotifier protocol
│   ├── composite.py          # CompositeNotifier multiplexing to enabled channels
│   ├── telegram.py           # Telegram Bot API notifier (HTML mode)
│   ├── discord.py            # Discord Webhook notifier (Rich Embeds)
│   ├── email.py              # SMTP Email notifier (STARTTLS, MIME text+HTML)
│   └── console.py            # Console / Dry-run notifier
├── state.py                  # AlertStateManager: edge detection, cooldown throttling, persistence
├── scheduler.py              # PollingScheduler: loop, threading.Event, backoff, metrics
└── logging_setup.py          # Logger configuration, custom credential redaction filter
tests/
├── __init__.py
├── fixtures/
│   ├── __init__.py
│   ├── available_response.html
│   ├── sold_out_response.html
│   ├── section_missing_response.html
│   └── malformed_response.html
├── test_parser.py
├── test_state.py
├── test_notifier.py
├── test_scheduler.py
└── test_cli.py
.env.example
README.md
```
