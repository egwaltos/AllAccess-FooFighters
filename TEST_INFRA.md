# E2E Test Infra: AllAccess Foo Fighters Ticket Monitor

## Test Philosophy
- Opaque-box, requirement-driven. Derived strictly from `ORIGINAL_REQUEST.md` and user-facing CLI/interfaces.
- No dependency on internal module private methods.
- Methodology: Category-Partition, Boundary Value Analysis (BVA), Pairwise Combinatorial Testing, Real-World Workload Testing.

## Feature Inventory & Test Mapping
| # | Feature | Source (Requirement) | Tier 1 (Coverage) | Tier 2 (Boundary) | Tier 3 (Pairwise) | Tier 4 (Workload) |
|---|---------|---------------------|:-----------------:|:-----------------:|:-----------------:|:-----------------:|
| 1 | F1: Bootstrap Extraction | ORIGINAL_REQUEST §R1 | ≥5 | ≥5 | ✓ | ✓ |
| 2 | F2: Accent Normalization | ORIGINAL_REQUEST §R1 | ≥5 | ≥5 | ✓ | ✓ |
| 3 | F3: False-Positive Rejection | ORIGINAL_REQUEST §R1 | ≥5 | ≥5 | ✓ | ✓ |
| 4 | F4: Pricing Extraction | ORIGINAL_REQUEST §R1 | ≥5 | ≥5 | ✓ | ✓ |
| 5 | F5: WAF & Challenge Handling | ORIGINAL_REQUEST §R1 | ≥5 | ≥5 | ✓ | ✓ |
| 6 | F6: Telegram Alerts | ORIGINAL_REQUEST §R2 | ≥5 | ≥5 | ✓ | ✓ |
| 7 | F7: Discord Alerts | ORIGINAL_REQUEST §R2 | ≥5 | ≥5 | ✓ | ✓ |
| 8 | F8: Email Alerts | ORIGINAL_REQUEST §R2 | ≥5 | ≥5 | ✓ | ✓ |
| 9 | F9: Composite Dispatch | ORIGINAL_REQUEST §R2 | ≥5 | ≥5 | ✓ | ✓ |
| 10 | F10: Throttling & Deduplication | ORIGINAL_REQUEST §R2 | ≥5 | ≥5 | ✓ | ✓ |
| 11 | F11: Configuration & Secret Masking | ORIGINAL_REQUEST §R3 | ≥5 | ≥5 | ✓ | ✓ |
| 12 | F12: Responsive Polling Loop | ORIGINAL_REQUEST §R3 | ≥5 | ≥5 | ✓ | ✓ |
| 13 | F13: Backoff & Jitter | ORIGINAL_REQUEST §R3 | ≥5 | ≥5 | ✓ | ✓ |
| 14 | F14: CLI Interface & Flags | ORIGINAL_REQUEST §R3 | ≥5 | ≥5 | ✓ | ✓ |
| 15 | F15: Simulated Mock Scenarios | ORIGINAL_REQUEST §Verification | ≥5 | ≥5 | ✓ | ✓ |

## Test Architecture
- Test Runner: Python standard library `unittest` (`python -m unittest discover -s tests -p "test_*.py"`).
- Test Layout:
  - `tests/test_parser.py`: Ticket status detection, bootstrap extraction, accent handling, false-positive protection.
  - `tests/test_notifier.py`: Telegram, Discord, Email, and Composite notification delivery with simulated payloads.
  - `tests/test_state.py`: Edge detection, cooldown throttling, duplicate suppression.
  - `tests/test_scheduler.py`: Polling loop, responsive interruption, exponential backoff with jitter.
  - `tests/test_cli.py`: Command-line options, `--once`, `--dry-run`, `--mock-status`, exit codes.
  - `tests/test_e2e_scenarios.py`: End-to-end multi-tier application workflows.
- Pass/Fail Semantics:
  - 100% test pass required (0 failures, 0 errors).
  - All mock tests must be fully deterministic with zero live network calls.

## Real-World Application Scenarios (Tier 4)
| # | Scenario | Features Exercised | Expected Outcome |
|---|----------|--------------------|------------------|
| 1 | Full Day Monitoring Simulation | F1, F3, F10, F12, F14 | Monitor runs 24 simulated checks throughout the day. When tickets appear on check 10, immediate alert dispatched. Checks 11-15 throttled. When tickets sell out on check 16, state resets. When tickets reappear on check 20, new alert dispatched. |
| 2 | Multi-Channel Alert Delivery with Network Glitch | F6, F7, F8, F9 | Tickets become available. Telegram succeeds, Discord fails with 500, Email succeeds. Composite reports success without crashing. Discord error logged. |
| 3 | Severe Server Degradation (HTTP 429 & 502) | F5, F12, F13 | AllAccess responds with 429 Retry-After: 30, then 502 Bad Gateway. Polling scheduler backs off gracefully without spamming origin or dying. When 200 OK returns, normal polling resumes. |
| 4 | Configuration Cascading & Secret Security | F11, F14 | User sets CLI flags overriding .env settings. Logs verify secrets (bot token, webhook, password) are strictly redacted with `[REDACTED]`. |
| 5 | Dry-Run One-Shot Production Check | F1, F2, F3, F14, F15 | Operator invokes `--once --dry-run` to check ticket availability. Sivori media status parsed cleanly, output formatted to stdout, exits with code 2 (unavailable) or 0 (available). |
