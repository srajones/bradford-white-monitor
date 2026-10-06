# bwwatch — Bradford White Aerotherm & Wave Heat Pump Water Heater Fault Watcher, Monitor & Logger

[![Docker Ready](https://img.shields.io/badge/docker-compose%20ready-blue.svg)](docker-compose.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12%20slim-brightgreen.svg)](Dockerfile)
[![Read-Only Enforced](https://img.shields.io/badge/safety-100%25%20read--only%20enforced-success.svg)](bwwatch/readonly.py)
[![Home Assistant](https://img.shields.io/badge/home%20assistant-webhook%20ready-41bdf5.svg)](#home-assistant-integration)
[![Zero Ports](https://img.shields.io/badge/inbound%20ports-none%20(0)-lightgrey.svg)](#ports-firewall-and-network-security)

An always-on, ultra-lightweight Docker monitoring service for **Bradford White Aerotherm® and Aerotherm® G2** hybrid electric heat pump water heaters (and any water heater connected to the **Bradford White Wave mobile app**).

It connects to the Bradford White Wave cloud, polls your water heater, **logs every fault to a crash-safe database**, tracks compressor vs. electric heating element energy usage, and **immediately alerts you** via **Home Assistant**, **ntfy**, **Telegram**, or **Email** when an issue arises.

---

### Why bwwatch?

The official Bradford White Wave Android and iOS apps **do not send push notifications** for fault codes. If your heat pump encounters an issue — such as **Fault 10 (Superheat Fault)** or **Fault 11 (Low Suction Temperature Fault)** — the heater may silently fall back to expensive resistive electric element heating or lock out entirely. You often only discover the problem days or weeks later when you run out of hot water or open an inflated electric bill.

**bwwatch solves this:**
- 🔔 **Instant Alerts:** Pushes alerts straight to your phone the moment a fault is logged.
- ⚡ **Energy & Temperature Monitoring:** Tracks heat pump compressor vs electric element kWh and setpoints over time.
- ⏱️ **Fault History & Cleared Faults:** Catches transient faults that appear and clear on their own between checks.
- 🎮 **Zero Memory CLI Menu (`bwcheat` / `bwctl`):** An interactive numbered menu (options 0–13) so you never need to remember complex Docker commands.
- 📊 **One-Click Reports & CSV Exports:** Exports `report.md`, `faults.csv`, `readings.csv`, and `energy.csv` for plumbers or warranty claims.
- ☁️ **Optional Cloud Backup:** Guided hand-held script (`setup-rclone.sh`) to mirror backups to Dropbox with `rclone` (100% optional).
- 🪶 **Lightweight — No Browser:** Plain Python talking directly to the API: no headless browser, no Selenium/Playwright, ~20–35 MB RAM, and zero open ports.
- 🛡️ **Guaranteed Read-Only:** Hardened in code — it cannot modify your water heater's temperature or operating mode.

---

## Quick Navigation

- [Quickstart (Install from Scratch)](#quickstart-install-from-scratch)
- [Interactive Menu & Everyday Commands (`bwcheat` / `bwctl`)](#interactive-menu--everyday-commands)
- [Live Data Viewers (Readings, Energy, Faults)](#live-data-viewers)
- [Home Assistant Integration](#home-assistant-integration)
- [Optional Dropbox Cloud Sync (`setup-rclone.sh`)](#optional-dropbox-cloud-backup-via-rclone)
- [How Fault Detection Works](#how-fault-detection-works)
- [Read-Only Guarantee & Safety Invariant](#read-only-guarantee)
- [Ports, Firewall & Network Security](#ports-firewall-and-network-security)
- [Configuration Reference (`.env`)](#configuration-reference-env)
- [Troubleshooting & FAQ](#troubleshooting--faq)
- [Architecture & Testing](#architecture--testing)

---

## Quickstart (Install from Scratch)

### Prerequisites
- A Linux server, VPS (e.g. Vultr, DigitalOcean, Hetzner), Raspberry Pi, or home server running **Docker Engine with Docker Compose** (the `docker compose` plugin).
- A Bradford White Wave account (the email and password you use in the Wave phone app).
- About 5 minutes.

### Option 1: Automated Guided Installer (Recommended)

Run these commands on your server:

```bash
# Create directory and clone the repository
sudo mkdir -p /opt/bwheater && sudo chown -R "$USER" /opt/bwheater
git clone https://github.com/srajones/bwwhitefaultcode.git /opt/bwheater
cd /opt/bwheater

# Run the guided installer
./install.sh
```

[`install.sh`](install.sh) guides you through every step:
1. Verifies Docker and system requirements.
2. Builds the lightweight container (`bwwatch:local`).
3. Asks how you want to be alerted (Home Assistant webhook, ntfy, Telegram, or email) and sends a **real test alert** to confirm delivery.
4. Performs sign-in (headless via credentials in `.env` or via a one-time browser link).
5. Starts the service and verifies that everything is operating normally.

| Command | What it does |
|---|---|
| `./install.sh` | Install bwwatch, or offer to check/reconfigure an existing setup |
| `./install.sh --check` | Verify current health without changing anything (`./bwctl verify` is identical) |
| `./install.sh --reconfigure` | Re-run the guided setup (change alert channels, credentials, time zone) |
| `./install.sh --uninstall` | Completely remove the container and image (asks before touching your data) |
| `./install.sh --help` | Display installer help options |

---

### Option 2: Fast 3-Minute Manual Setup (Docker Compose)

Prefer configuring via `.env` directly? It takes under 3 minutes:

```bash
cd /opt/bwheater

# 1. Copy the environment template
cp .env.example .env && chmod 600 .env

# 2. Edit .env with your credentials and alert settings:
#    BW_USERNAME="your-wave-email@example.com"
#    BW_PASSWORD="your-wave-password"
#    NTFY_TOPIC="my-secret-topic-name"  (or HA_WEBHOOK_URL=...)
nano .env

# 3. Build and launch
docker compose build
docker compose run --rm bwwatch login --auto
docker compose run --rm bwwatch test-notify
docker compose up -d

# 4. Verify everything works
./bwctl verify
```

---

## Interactive Menu & Everyday Commands

You don't need to remember Docker commands or syntax. Running `./bwctl` or `./bwcheat` launches the interactive console menu:

```text
========================================================================
             BRADFORD WHITE WATER HEATER WATCHER MENU
========================================================================
  1) Status Summary       (Health, last check time, mode, setpoint)
  2) Live Readings        (Table of tank modes & setpoint temps)
  3) Fault History        (Table of all active & past fault codes)
  4) Energy Usage         (Heat pump vs electric backup kWh)
  5) Recent Changes       (Sensors and settings that changed)
  6) Trigger Manual Poll  (Poll now & log directly to database)
  7) Generate Report      (Create Markdown report & CSV files)
  8) Sync to Dropbox      (Export fresh report & sync to Dropbox)
  9) Test Notifications  (Send test alert to phone / Home Assistant)
 10) View Live Logs       (Follow real-time service logs, Ctrl+C exits)
 11) Restart Watcher      (Restart container & re-read .env)
 12) Update Watcher       (Rebuild & update container with latest code)
 13) Setup Dropbox Sync   (Guided setup for optional rclone cloud backup)
  0) Exit
========================================================================
Select an option [0-13]:
```

### Command-Line Reference (`./bwctl`)

You can run commands directly without the menu by passing arguments to `./bwctl`:

| Command | Description |
|---|---|
| `menu` · `cheat` | Open the interactive numbered menu (`./bwctl menu` or `./bwctl cheat`) |
| `status` | One-screen summary: health, last check, setpoint, mode, active faults, backups |
| `poll` | Trigger an immediate manual poll and record results directly to SQLite |
| `readings` | Chronological table of operating modes and setpoint temperatures |
| `faults` `[--all] [--raw]` | List all recorded fault codes, occurrence dates, and cleared status |
| `energy` `[--view hourly\|daily]` | Table of heat pump compressor vs electric backup element kWh |
| `report` | Generate comprehensive `report.md` and CSV files in `data/exports/` |
| `sync` | Export latest reports and sync backups/reports to Dropbox via rclone |
| `changes` `[--hours N]` | Show what settings and sensors changed over a time window |
| `fields` | List all fields reported by the cloud and their current values |
| `calls` | Inspect cloud API request counts, latencies, and rate limits |
| `discover` `[--now] [--reset]` | Inspect or retry discovery of the `getApplianceErrors` endpoint |
| `export` `faults\|polls\|readings\|energy` | Export any database table as CSV to stdout or a file |
| `test-notify` `[--event fault]` | Send a real test notification to phone / Home Assistant |
| `logs` `[-f]` | View real-time service output (`-f` follows live) |
| `restart` | Restart container and re-read updated `.env` configuration |
| `update` | Rebuild image after a `git pull` and verify health |
| `verify` `[--wait N]` | Run end-to-end self-test (checks cloud, database, alerts, backups) |
| `login` | Re-authenticate with Wave (automatic via `.env` or browser link) |
| `check` | Dry-run check: reads cloud status and displays what bwwatch understands |
| `call` `"<request>"` | Make an arbitrary read-only request to inspect raw JSON |
| `probe` `[--yes]` | Run optional discovery probes if Wave changes endpoints |
| `backup` · `dbcheck` | Create verified SQLite backup now · Verify database integrity |
| `start` · `stop` | Start or stop the background Docker container |
| `reconfigure` · `uninstall` | Shortcuts for `./install.sh --reconfigure` and `--uninstall` |
| `help` | Display built-in command assistance |

Under the hood, these map to the container's internal commands: `run`, `login`, `check`, `call`, `probe`, `status`, `faults`, `export`, `backup`, `dbcheck`, `test-notify`, `verify`, `fields`, `changes`, `calls`, `discover`, `energy`, `readings`, `poll`, `report`, `healthcheck`, `setup`, and `version`.

---

## Live Data Viewers

### 1. Water Heater Readings (`./bwctl readings`)
Shows chronological recorded operating modes and setpoint temperatures over time:
```text
Time (EDT)           Appliance    Mode        Setpoint   Heat Mode Value
2026-10-05 12:38:34  WaterHeater  Heat Pump   120°F      2
2026-10-05 11:38:31  WaterHeater  Heat Pump   120°F      2
2026-10-05 10:38:30  WaterHeater  Hybrid      125°F      3
```

> **Note on Tank Water Temperature:** The Bradford White Wave cloud API provides the configured mode and temperature setpoint (`setpointFahrenheit`). Live internal tank water temperatures are handled locally on the physical heater display and are not broadcast over the cloud API. Any temperature field returned by the cloud is automatically logged.

### 2. Energy Usage Breakdown (`./bwctl energy`)
Inspect how much electricity your water heater is consuming, separated into efficient heat pump compressor usage vs. expensive backup electric heating elements:
```text
Period               Compressor (kWh)   Element (kWh)   Total (kWh)
2026-10-05 Daily                 2.14            0.00          2.14
2026-10-04 Daily                 3.82            1.45          5.27
```

### 3. Fault Code History (`./bwctl faults`)
Displays all active and historical fault codes, timestamps, and whether they cleared:
```text
Occurred (Appliance)   Code  Status     Description            Cleared At
2026-10-05 06:12:00    10    cleared    Superheat Fault        2026-10-05 07:44:00
```

### 4. Comprehensive Reports & CSV Exports (`./bwctl report`)
Generates structured Markdown and CSV exports inside `/opt/bwheater/data/exports/`:
- `report.md`: Markdown summary of heater health, active faults, recent readings, and element energy usage.
- `faults.csv`: Complete fault history for plumber analysis or warranty claims.
- `readings.csv`: Operating modes, setpoint history, and poll timestamps.
- `energy.csv`: Energy consumption breakdown by hour and day.

---

## Home Assistant Integration

bwwatch integrates seamlessly with **Home Assistant** via native webhooks. It pushes rich notifications to your phone through the Home Assistant Companion App.

### Step 1: Create the Automation in Home Assistant

Go to **Settings → Automations & Scenes → Create Automation**, click the three dots in the top right, select **Edit in YAML**, and paste the following:

```yaml
alias: Water Heater Fault Alert
description: Sent by bwwatch when the Wave cloud reports a water heater fault
triggers:
  - trigger: webhook
    webhook_id: "REPLACE_WITH_A_LONG_RANDOM_SECRET_KEY"
    allowed_methods:
      - POST
    local_only: false
conditions: []
actions:
  - choose:
      # Branch 1: Water Heater Fault Detected
      - conditions:
          - condition: template
            value_template: "{{ trigger.json.event == 'fault' }}"
        sequence:
          - action: notify.send_message
            target:
              entity_id: notify.mobile_app_YOUR_PHONE
            data:
              title: "⚠️ {{ trigger.json.title }}"
              message: "{{ trigger.json.message }}"
              data:
                priority: high
                ttl: 0
                channel: Emergency
                notification_icon: mdi:water-boiler-alert

      # Branch 2: Fault Cleared / Recovered
      - conditions:
          - condition: template
            value_template: "{{ trigger.json.event in ['cleared', 'recovered'] }}"
        sequence:
          - action: notify.send_message
            target:
              entity_id: notify.mobile_app_YOUR_PHONE
            data:
              title: "✅ {{ trigger.json.title }}"
              message: "{{ trigger.json.message }}"

      # Branch 3: Service Health Warning (e.g. Wave re-authentication needed)
      - conditions:
          - condition: template
            value_template: "{{ trigger.json.event == 'health' }}"
        sequence:
          - action: notify.send_message
            target:
              entity_id: notify.mobile_app_YOUR_PHONE
            data:
              title: "🔧 bwwatch Service Alert"
              message: "{{ trigger.json.message }}"
mode: queued
```

*(Note: Older Home Assistant installations before 2024.10 can replace `action: notify.send_message` with `action: notify.mobile_app_YOUR_PHONE` or `service: notify.mobile_app_YOUR_PHONE`.)*

### Step 2: Configure the Webhook URL in bwwatch

Set your webhook URL in `/opt/bwheater/.env`:

```env
HA_WEBHOOK_URL="https://hooks.nabu.casa/YOUR_WEBHOOK_PATH"
# Or private Tailscale/VPN URL:
# HA_WEBHOOK_URL="http://100.x.y.z:8123/api/webhook/REPLACE_WITH_A_LONG_RANDOM_SECRET_KEY"
```

Then restart: `./bwctl restart` (or `./bwctl update`).

### Step 3: Test the Automation

Run:
```bash
./bwctl test-notify --event fault
```
A mock Fault 99 alert will be delivered to your phone immediately! Check the **Traces** tab of your Home Assistant automation to inspect the delivered payload.

### Webhook Payload Schema (`trigger.json`)

| Field | Description |
|---|---|
| `source` | Always `"bwwatch"` |
| `event` | Event type: `fault`, `cleared`, `recovered`, `health`, `setting`, `info` |
| `title` | Ready-to-display title (e.g. `Water heater fault 10 — WaterHeater`) |
| `message` | Detailed description with timestamps and duration |
| `priority` | Priority from 1 (info) to 5 (urgent) |
| `appliance` | Object containing `{name, mac, serial}` |
| `fault` | Object containing `{id, code, description, occurred_at, detected_at, kind}` |

---

## Optional Dropbox Cloud Backup via Rclone

> [!NOTE]
> **Dropbox backup is 100% OPTIONAL.**
> bwwatch already maintains automated, crash-safe SQLite backups and CSV reports locally inside `/opt/bwheater/data/`. You do not need Dropbox or rclone to use bwwatch.

If you would like off-site copies of your database backups and reports mirrored to your Dropbox, bwwatch provides a hand-held interactive setup script: [`setup-rclone.sh`](setup-rclone.sh).

### Guided Setup (`./setup-rclone.sh`)

Simply run:
```bash
/opt/bwheater/setup-rclone.sh
```

The script walks you through each step:
1. **Checks for rclone:** Offers to install it automatically if missing.
2. **Guides Dropbox OAuth:** If on a headless VPS, explains how to run `rclone authorize "dropbox"` on your personal computer to generate a token, or launches `rclone config` directly.
3. **Tests Connection:** Verifies that your Dropbox remote responds.
4. **Initial Sync:** Runs an immediate test sync to verify folders.
5. **Automated Daily Sync (Cron):** Offers to schedule an automated daily sync every morning at 6:00 AM (`0 6 * * * /opt/bwheater/bwctl sync`).

### Manual Sync Command
Whenever you want to sync, simply run:
```bash
./bwctl sync
```
This generates fresh `report.md`, `faults.csv`, `readings.csv`, and `energy.csv` files, and copies them along with verified database backups to:
- `Dropbox/WaterHeater/backups/`
- `Dropbox/WaterHeater/reports/`

---

## How Fault Detection Works

### Real-Time Endpoint Discovery
The Bradford White Wave mobile app queries fault history using `GET /wave/getApplianceErrors`.
bwwatch verifies and automatically queries this endpoint:
```text
GET /wave/getApplianceErrors?mac_address={mac}
```
If you ever want to set or override the endpoint manually, add it to `.env`:
```env
BW_FAULT_REQUEST='GET /wave/getApplianceErrors?mac_address={mac}'
```

### Catching Transient & Cleared Faults
Many common heat pump faults — such as **Fault 10 (Superheat Fault)** — are intermittent. When operating conditions return to normal, the water heater clears the fault on its own.
Because bwwatch analyzes historical error records, even if a fault begins and clears while you are asleep or between normal polling checks, bwwatch still catches it:
- Logs the occurrence timestamp, clear timestamp, and total fault duration.
- Sends an alert marked **"(cleared)"** so you know an intermittent fault happened.

### Accelerated Polling During Active Faults
- **Normal state:** Polled once per hour (gentle on cloud servers, ~96 requests/day total).
- **Active fault detected:** Automatically accelerates to **every 10 minutes** for up to 12 hours so you get rapid updates and precise timing on when the fault clears.
- **Fault clears:** Returns to the normal hourly polling schedule.

---

## Read-Only Guarantee

bwwatch includes a mathematically verified read-only safety guard ([`bwwatch/readonly.py`](bwwatch/readonly.py)) that inspects **every single HTTP request** before it leaves the server:
- Any request with mutation methods (`PUT`, `DELETE`, `PATCH`) is rejected.
- Any request containing modification terms (`change`, `set`, `update`, `delete`, `reset`, `clear`, `ack`, `changeSetpoint`, `changeOpMode`) is rejected.
- Requests containing query parameters or payload keys for `temperature`, `mode`, or `setpoint` are blocked immediately.
- **There is no configuration flag, setting, or override that can disable this guard.**

bwwatch cannot alter your water heater temperature, cannot turn off heating, and cannot adjust operating modes.

---

## Ports, Firewall and Network Security

### No Inbound Ports & No Nginx
bwwatch listens on **no port** and needs no reverse proxy, no nginx, and no subdomain.
- It makes only **outgoing** HTTPS requests (to Bradford White APIs and your alert webhook).
- `docker-compose.yml` has **no `ports:` mapping**.
- It is invisible to external port scanners.

### Firewall & ufw Rules
Your firewall (e.g. `ufw`) needs only standard outgoing access:
- Outgoing TCP 443 (HTTPS) to `consumer.bradfordwhiteapps.com`, `gw.prdapi.bradfordwhiteapps.com`, and your alert host.
- Outgoing UDP/TCP 53 (DNS).
- **No incoming ports are required.**

---

## Everything Stays in `/opt/bwheater`

bwwatch strictly confines itself to its working directory (`/opt/bwheater`):
- Uses `umask 077` to guarantee all created files are private.
- The installer and updater **never install software** into host system paths, never modify `/etc`, create no global systemd units, and place no files in `/usr/local`.
- Everything lives inside:
  - `/opt/bwheater/.env`: Mode 600 settings.
  - `/opt/bwheater/data/bwwatch.db`: SQLite database in WAL mode with synchronous=FULL.
  - `/opt/bwheater/data/backups/`: Verified daily SQLite backups (keeps 14 newest).
  - `/opt/bwheater/data/exports/`: Generated `report.md` and CSV files.
  - `/opt/bwheater/data/token.json`: Secure rotating OAuth session token.

---

## Configuration Reference (`.env`)

See [`.env.example`](.env.example) for the full annotated list. Key options:

```env
# --- Wave Cloud Authentication (Headless VPS Login) ---
BW_USERNAME=your-email@example.com
BW_PASSWORD=your-password

# --- Polling Frequency ---
# Polling interval in seconds (default: 3600 = 1 hour; minimum: 300 = every 5 minutes)
BW_POLL_INTERVAL_SECONDS=3600

# --- Time Zone ---
# Time zone for alerts and display (e.g. America/New_York, America/Chicago, UTC)
DISPLAY_TZ=America/New_York

# --- Alert Channels (Configure one or more) ---
# Home Assistant Webhook:
HA_WEBHOOK_URL=https://hooks.nabu.casa/your-webhook-token

# ntfy Push Notifications:
NTFY_TOPIC=my-unique-waterheater-topic-abc123

# Telegram:
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=-100123456789

# Email (SMTP):
SMTP_HOST=smtp.mailgun.org
SMTP_TO=me@example.com

# --- Optional Fault History Request ---
BW_FAULT_REQUEST='GET /wave/getApplianceErrors?mac_address={mac}'
```

---

## Troubleshooting & FAQ

### How do I change the polling interval (e.g. to every 30 minutes)?
1. Edit `/opt/bwheater/.env` and set:
   ```env
   BW_POLL_INTERVAL_SECONDS=1800
   ```
2. Restart the watcher:
   ```bash
   /opt/bwheater/bwctl restart
   ```
*(Note: Values below 300 seconds / every 5 minutes are rejected to prevent excessive API load.)*

### How do I know if I got logged out or Wave is failing?
bwwatch will **never fail silently**.
- If your token expires or sign-in fails, bwwatch triggers a **Health Warning** alert:
  *"Wave sign-in needs attention — faults are NOT being monitored"*.
- After authentication failure, it backs off and stops hammering the server, retrying automatically every 6 hours or immediately when you run `./bwctl login`.
- Run `./bwctl status` or `./bwctl verify` at any time to verify authentication and health.

### What if I don't want to store my password in `.env`?
You don't have to! Leave `BW_USERNAME` and `BW_PASSWORD` blank. Run:
```bash
./bwctl login
```
bwwatch will print a one-time sign-in URL. Open it in any browser, log in to Wave, and paste back the resulting redirect link. bwwatch will extract the token and store it securely in `data/token.json`.

### How do I view live logs?
Run:
```bash
./bwctl logs -f
```
Press `Ctrl+C` to exit.

---

## Architecture & Testing

### How It Works Under the Hood
```text
every hour:  refresh token ─▶ list appliances ─▶ status (+ fault request) ─▶ ONE transaction:
                                 poll · settings · faults · queued alerts ─▶ deliver alerts ─▶ idle
```

Written in pure Python 3.12 using the Python standard library with zero external pip dependencies. Operates as an unprivileged container user (UID 10001) with all Linux capabilities dropped (`cap_drop: ALL`).

### Running the Test Suite
The repository includes a comprehensive unit and integration test suite:

```bash
python3 -m unittest discover -s tests -t .
```

The test suite runs against mock Wave cloud servers and mock alert endpoints, validating:
- 100% read-only request filtering.
- OAuth token rotation and error recovery.
- SQLite WAL transaction atomicity and simulated `kill -9` recovery.
- Alert delivery retries and webhook formatting.
- Endpoint discovery probes (about 14 test cases).
- Script confinement and installer verification.

---

## Credits and Disclaimer

The sign-in flow and API structures were uncovered through community research — special thanks to Graham Clenaghan's MIT-licensed [ha-bradford-white-wave](https://github.com/gclenaghan/ha-bradford-white-wave) and [bradford-white-wave-client](https://github.com/gclenaghan/bradford-white-wave-client).

bwwatch is an independent, read-only implementation. It is unofficial and not affiliated with Bradford White Corporation; the Wave API is undocumented and subject to change.

**Disclaimer:** bwwatch is a monitoring and logging aid — **not a life-safety device**. Do not rely on it as a substitute for physical temperature/pressure relief (T&P) valves, water leak sensors, or regular professional water heater maintenance.
