# AllAccess Foo Fighters Ticket Monitor

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Status](https://img.shields.io/badge/status-ready%20for%20production-brightgreen.svg)]()
[![Tests](https://img.shields.io/badge/tests-100%25%20passing-success.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A continuous, production-grade monitoring daemon engineered to detect ticket availability for **"Sivori media"** (and any other venue section) for the **Foo Fighters** concert on [AllAccess Argentina](https://www.allaccess.com.ar/event/foo-fighters). When tickets appear, instant alerts are dispatched across multi-channel messaging integrations (Telegram Bot, Discord Webhook, and SMTP Email).

---

## Architecture Overview

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
        ┌──────────────────────┴───────────────────────┐
        │ Tick Execution                               │
        ▼                                              ▼
  ┌───────────────────────┐                  ┌───────────────────┐
  │     TicketChecker     │                  │  GracefulShutdown │
  │ (AllAccess / Mock)    │                  │ (threading.Event) │
  └──────────┬────────────┘                  └───────────────────┘
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

---

## Key Features

- **F1: Resilient Rendr Bootstrap Extraction**: Extracts inline `App.bootstrapData` JSON state from Rendr-rendered HTML without fragile full-page regex or heavy headless browsers.
- **F2: Unicode & Accent Normalization**: Uses NFKD canonical decomposition to match `"Sivori media"` against `"Sivori Media"`, `"Sívori media"`, and other accent/case variations.
- **F3: Zero False-Positive Availability Filter**: Evaluates rate-level availability (`available: True`, `max > 0`, `availability > 0`) and section-level availability while ignoring deceptive top-level sector availability flags.
- **F4: Accurate Pricing & Fee Extraction**: Extracts base ticket price and service charge amounts from rates and minRate metadata.
- **F5: WAF & Challenge Protection**: Detects AWS WAF Captchas, rate limiting (HTTP 429), and Queue-It virtual waiting rooms without crashing.
- **F6–F9: Multi-Channel Alert Engine**: Dispatches notifications across Telegram Bot API (HTML formatted), Discord Webhook (Rich Embeds), and SMTP Email (multipart HTML/text) with fault isolation via `CompositeNotifier`.
- **F10: State Machine & Cooldown Throttling**: Dispatches an immediate alert on `UNAVAILABLE -> AVAILABLE`, suppresses duplicate alerts during a configurable cooldown window, and resets immediately upon tickets selling out.
- **F11: Cascading Configuration & Secret Redaction**: Supports CLI arguments > `.env` variables > built-in defaults. Sensitive tokens and passwords are automatically masked with `[REDACTED]` in logs, string representations, and tracebacks.
- **F12: Responsive Polling Scheduler**: Threading-event based sleep mechanism enabling sub-millisecond graceful shutdown upon `SIGINT` (Ctrl+C) or `SIGTERM`.
- **F13: Exponential Backoff & Jitter**: Automatically applies exponential backoff with full uniform jitter and HTTP 429 `Retry-After` header respect during origin server degradation.
- **F14: Operator CLI & Daemon Flags**: Rich command-line flags supporting single-tick execution (`--once`), offline dry-run (`--dry-run`), and mock state simulation (`--mock-status`).
- **F15: Deterministic Mock Simulation**: Five canonical test states (`available`, `sold_out`, `section_missing`, `network_error`, `waf_challenge`) for offline verification and CI/CD pipelines.

---

## Quickstart (3 Steps)

### Step 1: Clone and Install Dependencies

```bash
git clone https://github.com/example/allaccess-monitor.git
cd allaccess-monitor
python -m pip install -r requirements.txt
```

*(Note: Standard library Python 3.9+ is fully supported; `requests` and `python-dotenv` are optional dependencies for live networking and `.env` loading).*

### Step 2: Configure Environment

```bash
cp .env.example .env
```

Open `.env` and configure your preferred notification channel credentials (e.g., Telegram Bot Token, Discord Webhook, or SMTP Email).

### Step 3: Run a Test Check (Dry-Run)

```bash
python -m allaccess_monitor --once --dry-run --mock-status available
```

Output:
```
[DRY RUN] Running in dry-run mode: external notification dispatch is disabled.
[DRY RUN] Status: AVAILABLE - Tickets found for section 'Sivori media'! Price: $143,750 ARS (Base: $125,000 + Srv: $18,750)
```

---

## CLI Usage & Options

Run the application as an executable Python module:

```bash
python -m allaccess_monitor [OPTIONS]
```

### Command-Line Arguments

| Flag | Short | Default | Description |
|---|---|---|---|
| `--interval` | `-i` | `60.0` | Polling check interval in seconds between availability requests. |
| `--section` | `-s` | `Sivori media` | Target venue section name (case and accent insensitive). |
| `--url` | `-u` | `https://www.allaccess.com.ar/event/foo-fighters` | AllAccess event page URL. |
| `--cooldown` | | `1800` | Cooldown suppression window in seconds between duplicate alerts. |
| `--config` | `-c` | `None` | Path to custom `.env` configuration file. |
| `--timeout` | | `10.0` | HTTP request socket timeout in seconds. |
| `--retries` | | `3` | Maximum HTTP request retries on transient connection drop. |
| `--once` | | `False` | Run a single availability check, print/dispatch alert, and exit immediately. |
| `--dry-run` | | `False` | Test run without dispatching external notifications (prints to console). |
| `--mock-status` | | `None` | Canned response simulation (`available`, `sold_out`, `section_missing`, `network_error`, `waf_challenge`). |
| `--channels` | | `None` | Comma-separated list of active notification channels (e.g. `telegram,discord`). |
| `--state-file` | | `.monitor_state.json` | Path to JSON state file for cross-process alert persistence. |
| `--log-file` | | `None` | Path to write rotating log file with automated secret redaction. |
| `--verbose` | `-v` | `False` | Enable detailed DEBUG logging. |
| `--quiet` | `-q` | `False` | Suppress INFO logs, showing only WARNING and ERROR messages. |
| `--version` | | | Show program version and exit. |

### Exit Codes

- **`0`**: Available tickets detected in `--once` mode, or clean process termination on shutdown.
- **`1`**: Check error, network timeout, HTTP 5xx, or configuration validation failure.
- **`2`**: Tickets currently unavailable / sold out in `--once` mode, section not found, or CLI syntax error.

---

## Common Recipes

### Continuous Monitoring Daemon (Production)

```bash
python -m allaccess_monitor --interval 45 --section "Sivori media"
```

### Dry-Run Test Against Real Endpoint

```bash
python -m allaccess_monitor --once --dry-run
```

### Simulating Sold-Out State

```bash
python -m allaccess_monitor --once --mock-status sold_out
```

### Multi-Channel Selective Dispatch

```bash
python -m allaccess_monitor --channels telegram,discord
```

---

## Notification Setup Guide

### 1. Telegram Bot
1. Open Telegram and search for `@BotFather`.
2. Send `/newbot`, follow prompts, and copy the provided HTTP API token.
3. Obtain your Chat ID by messaging `@userinfobot` or adding your bot to a channel/group.
4. Set in `.env`:
   ```bash
   TELEGRAM_ENABLED=true
   TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789
   TELEGRAM_CHAT_ID=123456789
   ```

### 2. Discord Webhook
1. In your Discord server, open **Server Settings** -> **Integrations** -> **Webhooks**.
2. Click **New Webhook**, select your alert channel, and click **Copy Webhook URL**.
3. Set in `.env`:
   ```bash
   DISCORD_ENABLED=true
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1234567890/token-abc123xyz
   ```

### 3. SMTP Email (e.g. Gmail)
1. In your Google Account, enable 2-Step Verification and generate an **App Password**.
2. Set in `.env`:
   ```bash
   EMAIL_ENABLED=true
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USE_TLS=true
   SMTP_USERNAME=your.email@gmail.com
   SMTP_PASSWORD=your-16-char-app-password
   EMAIL_FROM=your.email@gmail.com
   EMAIL_TO=recipient1@example.com,recipient2@example.com
   ```

---

## Daemon & Service Deployment

### Systemd Service (Linux / Raspberry Pi)

Create `/etc/systemd/system/allaccess-monitor.service`:

```ini
[Unit]
Description=AllAccess Foo Fighters Ticket Availability Monitor
After=network.target

[Service]
Type=simple
User=monitor
WorkingDirectory=/opt/allaccess-monitor
ExecStart=/opt/allaccess-monitor/venv/bin/python -m allaccess_monitor --interval 45
Restart=always
RestartSec=10
EnvironmentFile=/opt/allaccess-monitor/.env

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now allaccess-monitor.service
```

### Cloud Execution via GitHub Actions Cron (No Local PC Required)

The repository includes [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml), which runs automatically in the cloud via GitHub Actions `schedule` (`cron: "*/5 * * * *"`) without requiring your machine to stay powered on:

1. **Push the repository to GitHub**.
2. Go to **Settings → Secrets and variables → Actions → New repository secret** and add your notification credentials:
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (for Telegram alerts)
   - `DISCORD_WEBHOOK_URL` (optional, for Discord alerts)
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO` (optional, for Email alerts)
3. **Test your notifications from GitHub**:
   - Open the **Actions** tab → select **Monitor Entradas Foo Fighters (Sivori Media)** → click **Run workflow**.
   - Check the box **"Enviar notificación de prueba a Telegram/Discord/Email"** (`test_notify: true`) and click **Run workflow**.
4. **Anti-Spam State Persistence**:
   - The workflow automatically caches `.monitor_state.json` between cron runs using `actions/cache@v4`, preserving cooldown state across runs.

### Docker & Docker Compose

```bash
docker compose up -d --build
```
Or using `docker build` directly:
```bash
docker build -t allaccess-monitor .
docker run -d --name ticket-monitor --env-file .env allaccess-monitor
```

---

## Testing & Quality Assurance

The project includes comprehensive automated test suites covering parser extraction, state machine transitions, notification dispatchers, exponential backoff, CLI interfaces, and end-to-end user workflows.

### Run All Unit and E2E Tests

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

### Run Specifically the E2E Scenario Suite

```bash
python -m unittest tests/test_e2e_scenarios.py -v
```

All tests are 100% deterministic and make zero live network calls.
