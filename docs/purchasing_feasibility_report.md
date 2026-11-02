# Technical Feasibility Analysis: Automated Ticket Purchasing & Safe Reservation Architecture on AllAccess Argentina

**Document Reference**: AA-ENG-FEAS-2026-M3  
**Target Platform**: AllAccess Argentina (`allaccess.com.ar`) & Crowder Ticketing Engine (`getcrowder.com`)  
**Target Milestone**: Milestone 3 (R3: Purchasing Feasibility & Safe Reservation Prototype)  
**Author**: AllAccess Monitor Engineering Group  
**Status**: Published / Final  
**Date**: November 2026  

---

## 1. Executive Summary & Feasibility Verdict

### 1.1 Objective and Scope
This document provides a comprehensive technical investigation into the feasibility of automating the ticket purchasing and reservation lifecycle on AllAccess Argentina (`allaccess.com.ar`). The analysis assesses the presentation layer, the underlying ticketing platform (**Crowder**), reverse-engineered checkout APIs, authentication and session flows, defensive edge obstacles (AWS WAF and Queue-It waiting rooms), legal and regulatory compliance hurdles (BCRA anti-fraud mandates and AllAccess Terms of Service), and provides an architectural blueprint for a safe, non-transactional reservation prototype.

### 1.2 Feasibility Verdict Matrix

| Dimension | Full End-to-End Automated Purchasing (Unattended Payment) | Safe Non-Transactional Cart Reservation (Human-in-the-Loop) |
|---|---|---|
| **Technical Feasibility** | **Infeasible (0% Unattended Reliability)** | **Feasible (High Reliability under Normal/Restock Conditions)** |
| **Edge Anti-Bot Resilience** | **Fails**: Blocked by AWS WAF Token Challenges & Queue-It HMAC-SHA256 tokens | **Viable**: Operates within normal polling thresholds, respects rate limits, handles session tokens |
| **Financial & 2FA Gateways** | **Fails**: Hard block at BCRA 3DS 2.0 OTP, mobile banking biometrics, and AllAccess Line ID code | **N/A**: Zero payment interaction; transactions handed off to human user |
| **Legal & Compliance Risk** | **Extreme / Prohibited**: Violates AllAccess ToS, triggers account bans, ticket cancellations, card blacklisting | **Safe / Negligible**: Non-transactional hold within user quotas; no card fraud or automated financial risk |
| **Safety Invariants** | **High Liability**: Unattended credit card charging, risk of incorrect sector/pricing purchases | **Guaranteed**: Hard budget ceilings, max quantity guards, zero storage of PCI-DSS financial data |
| **User Value** | **Zero / Negative**: High probability of financial loss, account banning, and revoked tickets | **High Value**: Eliminates manual seat-sniping latency; reserves seats for 10–15 min, delivering direct checkout URL |

### 1.3 Key Findings
1. **Unattended Payment Automation is Infeasible**: End-to-end purchasing cannot be reliably automated via standalone scripts. Even if a script bypasses edge bot protections to place tickets in a cart, Argentine banking regulations (BCRA) enforce mandatory 3-D Secure (3DS 2.0) verification via SMS One-Time Passwords (OTP) or mobile banking biometrics. Additionally, AllAccess enforces a proprietary "Line ID" security protocol requiring manual entry of a 6-digit confirmation code found on the cardholder's online pending statement.
2. **Safe Carting / Reservation is Highly Feasible and High-Value**: When high-demand tickets restock or drop, the primary bottleneck for legitimate buyers is securing the 10-to-15 minute inventory hold before other users or bots claim available seats. An automated system can detect availability, authenticate as the user, submit a reservation hold (`POST /api/cart`), and immediately notify the user across configured channels (Telegram, Discord, Email) with a direct, authenticated checkout URL.
3. **Architecture Decoupling**: AllAccess runs an isomorphic **Airbnb Rendr** (Backbone.js + Node.js) frontend backed by the **Crowder** (`getcrowder.com`) API core. Inventory state is embedded directly in the server-rendered HTML via `App.bootstrapData(...)`, making availability detection fast and efficient without heavy browser automation, while cart creation interacts directly with Crowder’s RESTful endpoints.

---

## 2. Platform Architecture: AllAccess Argentina & Crowder Core

### 2.1 Ecosystem Topography
AllAccess Argentina does not rely on generic off-the-shelf monolithic e-commerce platforms (such as Shopify, Magento, or WooCommerce). Instead, it utilizes an enterprise event ticketing stack distributed across specialized edge, presentation, inventory, and payment microservices:

```
                            ┌──────────────────────────────────────────────┐
                            │             User / Monitor Client            │
                            └──────────────────────┬───────────────────────┘
                                                   │
                                                   ▼
                                    ┌──────────────────────────────┐
                                    │    Cloudflare / Edge CDN     │
                                    │     (Caching & DNS Route)    │
                                    └──────────────┬───────────────┘
                                                   │
                     ┌─────────────────────────────┴─────────────────────────────┐
                     │                                                           │
        [High Traffic Surge / Drop]                                     [Normal / Off-Peak Traffic]
                     ▼                                                           ▼
      ┌─────────────────────────────┐                             ┌─────────────────────────────┐
      │    Queue-It Waiting Room    │                             │           AWS WAF           │
      │  (allaccess.queue-it.net)   │                             │  - challenge.js (Bot SDK)   │
      │  - KnownUser V3 Framework   │                             │  - aws-waf-token challenge  │
      │  - HMAC-SHA256 Token Gate   │                             │  - request-captcha-container│
      └──────────────┬──────────────┘                             └──────────────┬──────────────┘
                     │ (Valid queueittoken)                                      │
                     └─────────────────────────────┬─────────────────────────────┘
                                                   │
                                                   ▼
                                    ┌──────────────────────────────┐
                                    │    AllAccess Rendr Server    │
                                    │     (Node.js / Express)      │
                                    │ - Isomorphic Backbone SSR    │
                                    │ - App.bootstrapData payload  │
                                    │ - Session: connect.sid       │
                                    │ - CSRF: _csrf token          │
                                    └──────────────┬───────────────┘
                                                   │
                     ┌─────────────────────────────┴─────────────────────────────┐
                     │                                                           │
                     ▼                                                           ▼
      ┌─────────────────────────────┐                             ┌─────────────────────────────┐
      │     Crowder Ticketing API   │                             │   Argentine Payment Gateway │
      │      (getcrowder.com)       │                             │      (Decidir / Prisma)     │
      │ - Inventory Hold Engine     │                             │ - Credit/Debit Card Token   │
      │ - Rate & Quota Validator    │                             │ - BCRA 3DS 2.0 Challenge    │
      │ - 10-15 Min Hold TTL        │                             │ - Bank SMS OTP / Mobile Push│
      │ - Passcode / Presale Rules  │                             │ - 6-Digit "Line ID" Code    │
      └─────────────────────────────┘                             └─────────────────────────────┘
```

### 2.2 Presentation Layer: Isomorphic Airbnb Rendr
The web frontend is built on **Airbnb Rendr**, an isomorphic Node.js/Express framework that renders Backbone.js views and models on both the server and client.
- **Server-Side Hydration**: The server renders the initial HTML page and serializes the complete model state into a single JavaScript function call: `App.bootstrapData(...)`.
- **Client-Side SPA Transition**: Once loaded in a full browser, client-side scripts hydrate the DOM and perform asynchronous fetch requests for navigation and checkout interactions.
- **Scraping Advantage**: Because the entire event configuration (shows, dates, sector layouts, pricing, rate IDs, service charges, quotas) is embedded in `App.bootstrapData`, an automated client does not need to execute JavaScript or maintain headless Chrome instances to read real-time inventory.

### 2.3 Bootstrap Data Ingestion & Deceptive Flag Filtering
Our inspection of `allaccess_monitor/checker/allaccess.py` reveals the exact JSON structure of `App.bootstrapData`. A critical engineering nuance of the AllAccess/Crowder platform is that **`sector["available"]` is deceptive**:
- The platform frequently reports `"available": true` at the sector level even when all individual sub-sections and rate tiers are completely exhausted.
- **Strict Verification Filter**: To avoid false positives, the system must enforce compound multi-tier criteria:
  1. `rate.available is True`
  2. `rate.max > 0`
  3. `rate.availability is None or rate.availability > 0`
  4. `not rate.passcodeRequired` (or matching valid passcode provided)
  5. `not rate.showAsDisabled`
  6. Sub-section availability (`sub_section.available is True`)
  7. Show-level section availability (`show.sectionAvailability` entry is not `False`)

### 2.4 Backend Core: Crowder Platform (`getcrowder.com`)
Crowder is the white-label transactional ticketing and access control backend utilized by AllAccess across Argentina and South America. Key platform properties:
- **Centralized Quota & Hold Engine**: When a user selects tickets, the request routes to Crowder, which decrements the available pool and issues a temporary lock associated with a `cartId`.
- **Hold Duration**: The standard inventory hold window is **10 to 15 minutes** (600 to 900 seconds), governed by an `expiresAt` ISO UTC timestamp.
- **Concurrency & Race Condition Handling**: Crowder uses distributed transactional locks (Redis / database row-level locking). If two users request the last remaining seat concurrently, the first request receives HTTP 200 with the hold, while the second receives HTTP 409 Conflict (`"error": "No inventory available"`).

---

## 3. Reverse-Engineered Checkout API Endpoints

The checkout process on AllAccess proceeds through discrete RESTful operations between the browser, the Rendr Node.js server, and the Crowder core.

### 3.1 Step 1: Inventory Discovery & Show Inspection
- **Method & Path**: `GET /event/{slug}` (or `GET /event/{slug}/{showId}`)
- **Target URL**: `https://www.allaccess.com.ar/event/foo-fighters`
- **Request Headers**:
  ```http
  GET /event/foo-fighters HTTP/1.1
  Host: www.allaccess.com.ar
  User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36
  Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8
  Accept-Language: es-AR,es;q=0.9,en;q=0.8
  Sec-Ch-Ua: "Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"
  Sec-Ch-Ua-Mobile: ?0
  Sec-Ch-Ua-Platform: "Windows"
  Sec-Fetch-Dest: document
  Sec-Fetch-Mode: navigate
  Sec-Fetch-Site: none
  Sec-Fetch-User: ?1
  ```
- **Response**: Full HTML containing `App.bootstrapData(...)`.
- **Extracted Identifiers Required for Carting**:
  - `event.id`: Event identifier (e.g. `18337`)
  - `show.id`: Session identifier (e.g. `239575`)
  - `sector.id`: Sector identifier (e.g. `689304`)
  - `rate.id`: Pricing tier identifier (e.g. `4486883`)

### 3.2 Step 2: Rate & Quota Resolution
Within `App.bootstrapData`, each sector contains one or more `rates`:
```json
{
  "id": 4486883,
  "name": "Sivori Media",
  "description": "Acceso a Sivori Media",
  "price": 125000.00,
  "serviceChargeAmount": 18750.00,
  "availability": 10,
  "available": true,
  "max": 4,
  "passcodeRequired": false,
  "showAsDisabled": false
}
```
- Total ticket price equals `price + serviceChargeAmount` (e.g., $125,000 + $18,750 = $143,750 ARS).
- The maximum permissible tickets per single order is bounded by `rate.max` (typically 2 to 4).

### 3.3 Step 3: Cart Creation & Seat Hold Acquisition
When a user clicks "Comprar" or selects ticket quantity, the application dispatches a hold request.

- **Method & Path**: `POST /api/cart` (or `/api/shows/{showId}/reserve`)
- **Headers**:
  ```http
  POST /api/cart HTTP/1.1
  Host: www.allaccess.com.ar
  Content-Type: application/json; charset=UTF-8
  Accept: application/json, text/plain, */*
  X-Requested-With: XMLHttpRequest
  X-CSRF-Token: 9x8f0a2c-d4e5-4b8a-9c7a-1f2e3d4c5b6a
  Cookie: connect.sid=s%3A2h7K...; QueueITAccepted-allaccess-V3=...
  Origin: https://www.allaccess.com.ar
  Referer: https://www.allaccess.com.ar/event/foo-fighters
  User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36
  Sec-Fetch-Dest: empty
  Sec-Fetch-Mode: cors
  Sec-Fetch-Site: same-origin
  ```
- **Request Payload**:
  ```json
  {
    "showId": 239575,
    "sectorId": 689304,
    "rateId": 4486883,
    "quantity": 2,
    "passcode": null
  }
  ```
- **Response Payload (HTTP 200 OK — Hold Granted)**:
  ```json
  {
    "success": true,
    "cartId": "cart_9a41f8d2e5b7c",
    "expiresAt": "2027-02-25T20:15:00.000Z",
    "holdDurationSeconds": 600,
    "items": [
      {
        "ticketId": 1048576,
        "sectorId": 689304,
        "sectorName": "Sivori Media",
        "rateId": 4486883,
        "rateName": "Sivori Media",
        "quantity": 2,
        "unitPrice": 125000.00,
        "serviceCharge": 18750.00,
        "totalPrice": 287500.00
      }
    ],
    "checkoutUrl": "https://www.allaccess.com.ar/checkout?cart=cart_9a41f8d2e5b7c"
  }
  ```
- **Error Responses**:
  - `HTTP 409 Conflict`: Inventory exhausted between detection and cart request.
    ```json
    {"success": false, "error": "INSUFFICIENT_INVENTORY", "message": "No hay localidades disponibles"}
    ```
  - `HTTP 401 Unauthorized`: Session expired or missing login credentials.
  - `HTTP 403 Forbidden`: AWS WAF bot challenge triggered.
  - `HTTP 429 Too Many Requests`: Rate limit threshold exceeded.

### 3.4 Step 4: Active Cart State Inspection
- **Method & Path**: `GET /api/cart/{cartId}`
- **Purpose**: Verifies that the reservation hold remains active, checks remaining seconds before expiration, and validates reserved items.
- **Headers**: Same session cookie and CSRF token as Step 3.

### 3.5 Step 5: Explicit Cart Release / Rollback
- **Method & Path**: `DELETE /api/cart/{cartId}`
- **Purpose**: Releases the temporary inventory hold immediately if the user cancels or if the reservation engine aborts due to budget ceiling violations, returning inventory to the public pool cleanly.

### 3.6 Step 6: Direct Checkout Redirection
- **Format**: `https://www.allaccess.com.ar/checkout?cart={cartId}`
- When opened in an authenticated browser sharing the active session cookie (`connect.sid`), the user bypasses ticket selection entirely and is placed directly on the payment submission screen with the countdown timer actively ticking.

---

## 4. Authentication, Identity, and Session State

### 4.1 Account Requirements & Identity Verification
Unlike general merchandise retailers, event ticketing in Argentina is subject to national anti-scalping and identity regulations:
1. **Registered Profile**: Unauthenticated guest checkout is disabled on AllAccess. Every purchase must be bound to a verified AllAccess account.
2. **Argentine Tax/National ID (DNI / CUIT / CUIL)**: Accounts require a valid Argentine National Identity Document (DNI) or Passport number. Tickets are personalized and nominal.
3. **Phone & Email Verification**: Accounts must have verified email addresses; high-demand presales often require SMS phone verification.

### 4.2 Authentication Flow
Authentication uses standard session cookies managed by Express Session (`connect.sid`):
- **Login Endpoint**: `POST /api/auth/login` (or `POST /login`)
- **Headers**: `Content-Type: application/json`, `X-CSRF-Token: ...`
- **Payload**:
  ```json
  {
    "email": "user@example.com",
    "password": "SecurePassword123!"
  }
  ```
- **Response**: Returns HTTP 200 with `Set-Cookie` directives:
  - `connect.sid`: Signed HTTP-only session cookie.
  - `_csrf`: CSRF token for subsequent state-modifying requests.
  - `user_data`: Basic serialized user metadata (id, name, email, DNI).

### 4.3 Multi-Session Dynamics & Invalidation
- **Single Active Session**: In high-demand sales, AllAccess frequently invalidates older sessions when a new login occurs from a different IP address or user-agent fingerprint.
- **Session Continuity**: For a safe reservation prototype to hand off a cart hold to a human user, the prototype and the user's browser must share the same session context (via cookie injection or browser extension) or the prototype must run in the user's local desktop environment.

---

## 5. Anti-Bot, Traffic Shaping, and Defensive Edge Obstacles

AllAccess employs a defense-in-depth perimeter consisting of AWS WAF, Queue-It, and edge rate limiters.

```
+-------------------------------------------------------------------------+
|                         DEFENSIVE PERIMETER                             |
+-------------------------------------------------------------------------+
|                                                                         |
|  1. AWS WAF Edge                                                        |
|     - IP Reputation / Threat Intelligence                               |
|     - challenge.js (Canvas, WebGL, AudioContext, Navigator fingerprint) |
|     - Silent Token Evaluation (aws-waf-token)                           |
|     - Interactive Captcha (request-captcha-container)                   |
|                                                                         |
|  2. Queue-It Virtual Waiting Room (Traffic Spikes)                      |
|     - CDN / Middleware Interception                                     |
|     - KnownUser V3 Framework (HMAC-SHA256 Signed Tokens)                |
|     - Cookie: QueueITAccepted-<CustomerID>-V3_<EventID>                 |
|                                                                         |
|  3. Edge Rate Limiting & Anomaly Detection                             |
|     - 429 Too Many Requests (Retry-After header)                        |
|     - Burst traffic limits (~10-20 req/min/IP)                          |
+-------------------------------------------------------------------------+
```

### 5.1 AWS WAF Telemetry, Fingerprinting, and Challenges
AllAccess uses AWS WAF Bot Control to inspect incoming client traffic:
1. **Telemetry & Fingerprinting**: Injects `challenge.js` which executes in the browser to collect:
   - WebGL renderer and vendor strings
   - Canvas pixel rendering hash
   - Web Audio context output
   - Browser navigator properties (`navigator.webdriver`, hardware concurrency, platform)
   - Screen geometry and event listener latency
2. **Silent Challenges**: Solves computational proof-of-work in the background and sets the `aws-waf-token` cookie. HTTP client libraries (`requests`, `urllib`, `aiohttp`) lack a JavaScript engine and cannot generate or renew this token.
3. **Interactive CAPTCHA Challenges**: If anomaly scores exceed thresholds, the server returns an HTML page containing `<div id="request-captcha-container"></div>` with status 200 or 403.
   - *Impact on Automation*: Any script running raw HTTP requests is immediately halted upon receiving a WAF challenge. Automated bypass requires headless browser pools with stealth extensions and human or AI captcha-solving infrastructure, which violates ToS and introduces fatal latency during ticket drops.

### 5.2 Queue-It Virtual Waiting Room Integration
During major ticket releases (e.g., Taylor Swift, Coldplay, Foo Fighters, Lollapalooza), AllAccess activates **Queue-It**:
1. **Interception Trigger**: Edge CDN rules detect that requests exceed backend capacity and redirect incoming traffic to `https://allaccess.queue-it.net/?c=allaccess&e={eventId}`.
2. **KnownUser V3 Framework**:
   - Users entering the queue receive a randomized place in line.
   - When reaching the front, Queue-It generates a cryptographic token passed via query string:
     ```
     ?queueittoken=e_{eventId}~q_{queueId}~ts_{timestamp}~ce_{customData}~h_{hmacSha256}
     ```
   - The AllAccess server uses a private secret key to verify the HMAC-SHA256 signature.
   - Upon successful verification, the server sets the session cookie:
     `QueueITAccepted-allaccess-V3_{eventId}=...`
3. **Bypass Impossibility**: It is cryptographically impossible for an HTTP script to forge a valid `queueittoken` without possessing Queue-It's private shared secret. Any attempt to access `/api/cart` without the corresponding `QueueITAccepted` cookie returns an immediate redirect or HTTP 403/503.

### 5.3 Edge Rate Limiting & Traffic Shaping
- Sustained polling from a single IP address faster than 1 request per 3–5 seconds triggers an HTTP 429 (Too Many Requests).
- `allaccess_monitor` incorporates exponential backoff with random jitter and `Retry-After` header extraction, which prevents IP bans during monitoring cycles.

---

## 6. Regulatory, Payment, and Legal Barriers

### 6.1 Argentine Financial Regulations & BCRA Directives
The Central Bank of the Argentine Republic (BCRA) enforces strict cybersecurity and anti-fraud regulations governing electronic payment transactions:
- **Mandatory Two-Factor Authentication**: BCRA Communication "A" series requires payment service providers (PSPs) and acquiring banks to validate card-not-present transactions through at least two independent authentication factors (knowledge, possession, or inherence).
- **Anti-Fraud Algorithms**: Local processors (Prisma Medios de Pago / Decidir, Red Link, Mercado Pago) automatically score transactions. Automated checkout patterns (instantaneous form population, lack of mouse tracking, foreign IP addresses) trigger immediate fraud flags, freezing the transaction.

### 6.2 3-D Secure 2.0 (3DS) & Multi-Factor Verification
AllAccess routes card transactions through Decidir (Prisma Medios de Pago) with 3-D Secure 2.0 enabled:
- **Challenge Flow**: When `/api/checkout/pay` is invoked, the gateway initiates an iframe or redirect challenge to the issuing bank (Visa Secure, Mastercard Identity Check, Cabal).
- **Authorization Methods**:
  - SMS One-Time Password (OTP) sent to the cardholder's registered mobile device.
  - Push notification approval via the cardholder's mobile banking application (e.g., Santander, Galicia, BBVA, Modo).
  - Biometric authentication (fingerprint/FaceID) in the banking app.
- **Unattended Failure**: An automated purchasing script cannot complete this step without human intervention. Attempting to bypass or intercept banking 2FA is technically infeasible and constitutes financial fraud.

### 6.3 The AllAccess "Line ID" Verification Barrier
In addition to 3DS 2.0, AllAccess enforces a secondary verification barrier known as **Line ID**:
- Upon initiating payment, AllAccess places a temporary pending pre-authorization on the card containing a unique 6-digit verification code in the statement descriptor (e.g., `ALLACCESS*123456`).
- The user must open their online banking card portal (e.g., Visa Home, Masterconsultas, or home banking), locate the pending transaction, and input the 6-digit code into AllAccess within 10 minutes.
- This verification step cannot be automated via standard e-commerce scripts and requires human eyes on bank statements.

### 6.4 Legal Barriers & Terms of Service (ToS) Prohibitions
1. **Explicit Prohibition of Bots & Crawlers**: AllAccess Terms of Service strictly prohibit the use of automated mechanisms, spiders, robots, crawlers, or scraping scripts to access, query, reserve, or purchase tickets.
2. **Enforcement & Penalties**:
   - **Immediate Account Banning**: Automated activity results in permanent suspension of the associated account.
   - **Credit Card Blacklisting**: The card number, BIN, and cardholder DNI are blacklisted across the platform.
   - **Unilateral Ticket Revocation**: Under AllAccess purchase terms, the organizer reserves the right to void and cancel tickets purchased via automated means without refund.
   - **Legal Liability**: In Argentina, scalping and automated buying violate local ticket sale regulations (Ley de Defensa del Consumidor, Código Contravencional de la CABA Art. 93/94), exposing bot operators to civil and criminal prosecution.

---

## 7. Architectural Design: Non-Transactional Safe Reservation Prototype

To satisfy project requirement **R3** while strictly adhering to legal, technical, and financial safety boundaries, the system implements a **Non-Transactional Safe Reservation Prototype**.

```
+---------------------------------------------------------------------------------+
|                       SAFE RESERVATION WORKFLOW                                 |
+---------------------------------------------------------------------------------+
|                                                                                 |
|  1. Ticket Availability Edge Trigger                                            |
|     (allaccess_monitor FSM detects UNAVAILABLE -> AVAILABLE)                    |
|                                                                                 |
|  2. Safety & Policy Validation (ReservationEngine)                             |
|     [Pass] target_quantity <= max_limit (2)                                     |
|     [Pass] total_price <= max_budget_ars ($300,000)                             |
|     [Pass] passcode verification                                                |
|                                                                                 |
|  3. Automated Cart Hold Acquisition (AllAccessCartClient)                       |
|     POST /api/cart -> Acquires 10-15 min hold                                  |
|     Returns: cartId = "cart_9a41f8d2e", expiresAt = 2027-02-25T20:15:00Z        |
|                                                                                 |
|  4. Urgent Multi-Channel Alert Dispatch (CompositeNotifier)                     |
|     "Tickets Reserved! Hold expires in 10 minutes.                             |
|      Complete checkout: https://www.allaccess.com.ar/checkout?cart=..."         |
|                                                                                 |
|  5. Human-in-the-Loop Completion                                                |
|     Human opens authenticated URL in browser, reviews order,                    |
|     authorizes payment via 3DS/OTP/Line ID safely and legally                   |
+---------------------------------------------------------------------------------+
```

### 7.1 Non-Transactional Guarantees & Safety Invariants
1. **Zero Financial Interaction**: The prototype does not accept, store, transmit, or process credit card numbers, CVVs, expiration dates, or bank credentials.
2. **Deterministic Budget Ceiling**: The system rejects any cart reservation where `total_price > max_budget_ars`, preventing unexpected expenditures.
3. **Strict Quantity Caps**: The system restricts cart reservations to a maximum of 2 to 4 tickets (never exceeding promoter quotas or venue rate limits).
4. **Clean Hold Rollback**: If an alert cannot be delivered or if the user cancels, the client issues a `DELETE /api/cart/{cartId}` to release the hold back to the public pool.

### 7.2 Subsystem Domain Models (`allaccess_monitor/reservation/models.py`)

- `ReservationStatus`: Lifecycle enum (`PENDING`, `RESERVED`, `EXPIRED`, `FAILED_SOLD_OUT`, `FAILED_RATE_LIMITED`, `FAILED_CHALLENGE`, `FAILED_BUDGET_EXCEEDED`, `RELEASED`).
- `HoldItem`: Ticket item held in cart with unit price, fee, and subtotal calculation.
- `ReservationRequest`: Parameters for cart creation with strict non-transactional safety validations (rejection of payment keywords and card number patterns), quantity bounded to 1–4, and budget ceiling.
- `CartReservation`: Structure holding `cart_id`, status, items, expiration timestamp, dynamic seconds-remaining calculation, and direct checkout URL.

### 7.3 Client & Engine Interfaces (`allaccess_monitor/reservation/`)

#### 1. `BaseCartClient`
Abstract base class defining `reserve_tickets()`, `reserve_cart()`, `release_cart()`, and `get_cart()`.

#### 2. `ReservationEngine` (Safety & Orchestration)
The `ReservationEngine` wraps a `BaseCartClient` and enforces safety policies:
- Validates that `request.quantity` does not exceed system limit (default: 2, hard max: 4).
- Checks estimated total against `max_budget_ars` before calling API.
- Upon successful hold, formats an urgent `TicketAlert` containing the `direct_checkout_url` and remaining time.
- Triggers notifications through the existing `CompositeNotifier`.

#### 3. `MockAllAccessCheckoutService` (In-Memory Deterministic Mock)
To support automated unit and integration tests (R4) without live network traffic or AllAccess credentials:
- Maintains an in-memory dictionary of active carts, hold timestamps, and inventory quotas.
- Supports deterministic simulation scenarios:
  - `SCENARIO_SUCCESS`: Generates valid hold with 600s TTL.
  - `SCENARIO_SOLD_OUT`: Returns HTTP 409 Conflict.
  - `SCENARIO_RATE_LIMIT`: Returns HTTP 429 with `Retry-After: 30`.
  - `SCENARIO_WAF_CHALLENGE`: Returns HTTP 403 with `request-captcha-container` HTML.
  - `SCENARIO_EXPIRED`: Simulates hold timeout.

---

## 8. Conclusion
Automating ticket purchasing on AllAccess Argentina to the point of unattended payment is technically unviable and legally hazardous due to AWS WAF interactive puzzles, Queue-It cryptographic gates, BCRA 3DS 2.0 regulations, and the AllAccess Line ID code.

Conversely, an automated **Safe Reservation Prototype** that monitors inventory, claims a temporary 10–15 minute hold (`POST /api/cart`), and hands off an authenticated direct checkout URL to the human user is technically feasible, delivers maximum speed advantage during high-demand ticket drops, and eliminates all financial and legal liabilities.
