# Project: AllAccess Ticket Alert & Reservation System Enhancement

## Architecture

The system extends `allaccess_monitor` to support multi-event and multi-sector monitoring, rich multi-channel alerts with per-target cooldown deduplication, and a safe, non-transactional ticket reservation prototype.

```
                           ┌─────────────────────────────────────────┐
                           │          Configuration Source           │
                           │  - Legacy env vars (single event)       │
                           │  - Comma-separated sectors              │
                           │  - Static events config (JSON / file)   │
                           └────────────────────┬────────────────────┘
                                                │
                                                ▼
                           ┌─────────────────────────────────────────┐
                           │              MonitorConfig              │
                           │  - targets: List[MonitoredTarget]       │
                           │  - backward-compatible single properties │
                           └────────────────────┬────────────────────┘
                                                │
                                                ▼
                           ┌─────────────────────────────────────────┐
                           │            PollingScheduler             │
                           │  - Cycles through monitored targets     │
                           │  - Groups checks by event URL for batch │
                           │  - Exponential backoff & jitter         │
                           └────────┬──────────────────────┬─────────┘
                                    │                      │
                                    ▼                      ▼
                     ┌──────────────────────┐      ┌─────────────────────────────┐
                     │ AllAccessTicketChecker│      │      AlertStateManager      │
                     │ - App.bootstrapData  │      │ - Partitioned by target     │
                     │ - Multi-sector parse │      │ - Independent cooldown      │
                     │ - Session/date info  │      │ - State-change edge trigger │
                     └──────────────────────┘      └──────────────┬──────────────┘
                                                                  │
                                                                  ▼
                                                   ┌─────────────────────────────┐
                                                   │      CompositeNotifier      │
                                                   │ - Telegram, Discord, Email  │
                                                   │ - Event, date/session,      │
                                                   │   sector, price, checkout   │
                                                   └──────────────┬──────────────┘
                                                                  │ (Optional auto-reserve)
                                                                  ▼
                                                   ┌─────────────────────────────┐
                                                   │   ReservationEngine &       │
                                                   │   MockCheckoutService       │
                                                   │ - Non-transactional hold    │
                                                   │ - Budget & quantity guards  │
                                                   │ - Safe human-in-loop link   │
                                                   └─────────────────────────────┘
```

## Feature Inventory

| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| F1 | `MonitoredTarget` model | Structured dataclass representing an event + target sector | M1 | Survey 1 |
| F2 | Backward-compatible Config | Support legacy single-event env vars while parsing multi-target JSON/comma-separated configs | M1 | Survey 1 |
| F3 | Multi-sector HTML parsing | Parse multiple sectors and session date/time from a single `App.bootstrapData` page | M1 | Survey 1/2 |
| F4 | Target-partitioned State Tracking | Partition `AlertStateManager` tracking by `(event_url, sector)` to prevent state flapping | M1 | Survey 2 |
| F5 | Multi-target polling cycle | Update `PollingScheduler` to check all targets in cycle and handle independent availability | M1 | Survey 1 |
| F6 | Rich Notification Models | Enrich `TicketAlert` with session date/time and direct checkout URL | M2 | Survey 2 |
| F7 | Telegram Notifier formatting | Render event, date/session, sector, price, direct link, and timestamp in Telegram | M2 | Survey 2 |
| F8 | Discord Notifier formatting | Render rich embed with session date, sector, price, direct link, and timestamp in Discord | M2 | Survey 2 |
| F9 | Email Notifier formatting | Render plain text & HTML with complete event, session date, sector, price, direct link | M2 | Survey 2 |
| F10 | Console Notifier formatting | Render clean formatted banners with session date, sector, status, and direct link | M2 | Survey 2 |
| F11 | Configurable Cooldown & Deduplication | Enforce per-target cooldown timer and instant state-change alert triggers | M2 | Survey 2 |
| F12 | Technical Feasibility Report | Document checkout API endpoints, session flow, Crowder backend, and anti-bot limitations | M3 | Survey 3 |
| F13 | Safe Reservation Domain Models | Models for `CartReservation`, `ReservationRequest`, `ReservationStatus`, `CartItem` | M3 | Survey 3 |
| F14 | Safe Carting Client & Engine | `AllAccessCartClient` & `ReservationEngine` with quantity/budget limits and no payments | M3 | Survey 3 |
| F15 | Mock Checkout Service | Deterministic in-memory mock simulating Crowder hold, timeout, 409 sold out, 429 rate limit | M3 | Survey 3 |
| F16 | Standalone Reservation CLI Script | Executable demonstration script/module for safe ticket carting and hold acquisition | M3 | Survey 3 |
| F17 | Pytest Framework Setup | Add `pytest>=8.0.0` dependency, `pytest.ini` configuration, verify full suite executes | M4 | Survey 3 |
| F18 | Comprehensive Unit & Integration Tests | Unit & integration tests for multi-target config, parsing, state, alerts, and carting | M4 | Survey 1/2/3 |
| F19 | E2E Testing Suite & Regression Verification | Opaque-box regression check ensuring 486 existing tests + new test tiers pass cleanly | M4 | Survey 1/2/3 |

## Milestones

| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Multi-Event & Multi-Sector Monitoring | F1, F2, F3, F4, F5: `MonitoredTarget`, `MonitorConfig` parsing, multi-sector extraction, partitioned `AlertStateManager`, scheduler loop | none | DONE (554 tests pass) |
| M3 | Purchasing Feasibility Report & Reservation Prototype | F12, F13, F14, F15, F16: Feasibility report markdown, reservation models, cart client, reservation engine, mock checkout service, CLI runner | none | DONE (640+ tests pass) |
| M4 | Comprehensive Test Suite & Pytest Verification | F17, F18, F19: `pytest` installation and configuration, unit/integration tests for M1-M3, full suite execution passing 100% | M1, M2, M3 | DONE (681 tests pass under pytest & unittest) |

## Interface Contracts

### 1. `MonitoredTarget` & `CheckResult`
```python
@dataclass(frozen=True)
class MonitoredTarget:
    event_url: str
    target_section: str
    event_name: Optional[str] = None
    max_price: Optional[float] = None

@dataclass(frozen=True)
class CheckResult:
    status: AvailabilityStatus
    event_name: str = "Foo Fighters"
    event_url: str = "https://www.allaccess.com.ar/event/foo-fighters"
    section: Optional[SectionInfo] = None
    all_sections: List[SectionInfo] = field(default_factory=list)
    status_code: Optional[int] = None
    error_message: Optional[str] = None
    response_time_ms: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    session_name: Optional[str] = None
    session_date: Optional[str] = None
    direct_checkout_url: Optional[str] = None
```

### 2. `TicketAlert`
```python
@dataclass(frozen=True)
class TicketAlert:
    event_name: str
    section_name: str
    status: str
    event_url: str
    price: Optional[float] = None
    currency: str = "ARS"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_reminder: bool = False
    session_name: Optional[str] = None
    session_date: Optional[str] = None
    direct_checkout_url: Optional[str] = None
```

### 3. `AlertStateManager` Multi-Target State Contract
```python
class AlertStateManager:
    def evaluate(self, result: CheckResult) -> AlertDecision: ...
    # Backward compatibility:
    @property
    def is_currently_available(self) -> bool: ...
    @property
    def last_alert_time(self) -> Optional[float]: ...
```

### 4. Reservation Subsystem Contract
```python
class BaseCartClient(ABC):
    @abstractmethod
    def reserve_tickets(self, request: ReservationRequest) -> CartReservation: ...
    @abstractmethod
    def release_cart(self, cart_id: str) -> bool: ...
```

## Code Layout

- `allaccess_monitor/models.py`: Core domain models (`MonitoredTarget`, `SectionInfo`, `CheckResult`, `TicketAlert`).
- `allaccess_monitor/config.py`: Configuration parsing, `events.json` / env var multi-target loader.
- `allaccess_monitor/checker/allaccess.py`: Multi-sector parser & session metadata extractor.
- `allaccess_monitor/state.py`: Partitioned `AlertStateManager`.
- `allaccess_monitor/scheduler.py`: Multi-target polling scheduler.
- `allaccess_monitor/notifier/*.py`: Telegram, Discord, Email, Console rich formatters.
- `allaccess_monitor/reservation/`:
  - `models.py`: Cart reservation models.
  - `client.py`: Base and HTTP cart clients.
  - `engine.py`: Reservation engine with safety budget & quantity limits.
  - `mock_service.py`: In-memory deterministic mock checkout service.
- `allaccess_monitor/scripts/reserve_prototype.py` (or `reservation_cli.py`): Executable prototype runner.
- `docs/purchasing_feasibility_report.md`: Standalone technical feasibility report.
- `pytest.ini` & `requirements.txt`: Test configuration and pytest package.
- `tests/`:
  - `test_multi_target_config.py`: Testing multi-event/multi-sector config and backwards compatibility.
  - `test_multi_target_state.py`: Testing partitioned state tracking and cooldown isolation.
  - `test_alert_formatting.py`: Testing rich message generation across all 4 channels.
  - `test_reservation_prototype.py`: Testing safe carting, budget ceilings, mock checkout responses.
  - All existing 12 test files maintained with 100% pass rate.
