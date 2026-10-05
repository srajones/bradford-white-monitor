# bwwatch — Bradford White Wave fault watcher

A small, always-on Docker Compose service for a VPS (or any always-on machine). It signs in to the
Bradford White **Wave** cloud — the same service the phone app uses — checks your water heater about
once an hour, **logs every fault to a crash-safe database**, and **alerts you** by push notification,
email, Telegram or a **Home Assistant** webhook. It exists because the Android app doesn't send push
notifications for faults.

- **Guided install.** One script, [`install.sh`](install.sh), walks you through it, tests every step for real
  (a real test alert, the real sign-in) and checks the finished install end to end —
  see [Install](#install).
- **Read-only.** It cannot change a setting on your water heater. This is enforced in code, not by good
  behaviour — see [Read-only guarantee](#read-only-guarantee).
- **Stays in one folder.** Everything it creates lives in `/opt/bwheater`; nothing else on the server is touched —
  see [Everything stays in `/opt/bwheater`](#everything-stays-in-optbwheater).
- **Light — and no browser.** Plain Python talking to an API: no headless browser, no extra packages, about
  20–35 MB of RAM — see [Is it lightweight?](#is-it-lightweight-does-it-run-a-browser).
- **Gentle.** About 4 small requests per hour, never more often than every 5 minutes —
  see [How often it contacts Bradford White](#how-often-it-contacts-bradford-white).
- **Hard to corrupt.** SQLite in WAL mode with full fsync, one transaction per poll, integrity checks,
  verified backups, automatic recovery — see [Your data](#your-data-and-how-it-is-protected).
- **Never silent.** If it can't sign in or can't reach Wave, it tells you, instead of looking like
  "no faults".
- **No password on the server.** You sign in once in your own browser; only a revocable token is kept.

> **One thing is still yours to find:** the exact request the app's *Notifications* tab uses is not
> publicly documented. Until you set it (see [Finding the fault request](#finding-the-fault-request)),
> bwwatch records the heater's settings and watches the status data for fault-looking fields, but it can't
> read the notification list. Everything else works from day one.

---

## Install

You need a Linux server with **Docker Engine and the Compose plugin** (the `docker compose` command) —
on Debian, follow [Docker's own steps for Debian](https://docs.docker.com/engine/install/debian/), which install
both. You also need your phone (to receive alerts) and a web browser (to sign in to Wave once). It takes about
five minutes. Run these on the server:

```bash
# as root; with sudo, put it in front of the first two commands and use  sudo ./install.sh  below
mkdir -p /opt/bwheater
# the repository is private: when git asks for a password, give it a GitHub personal access token (or use a deploy key)
git clone https://github.com/srajones/bwwhitefaultcode.git /opt/bwheater
cd /opt/bwheater && ./install.sh
```

(Not root? `sudo mkdir -p /opt/bwheater && sudo chown "$USER" /opt/bwheater` first, and run the installer as a
user who may use Docker, or with `sudo ./install.sh`. If `git` is missing: `apt-get install git`.)

The installer does four things, and tells you what it is doing and whether each one worked:

1. **Checks this server** — Docker running and new enough, the Compose file valid, no clash with another
   container named `bwwatch`, that nothing will listen on a port, what your firewall looks like (read-only),
   enough disk space, Docker set to start at boot (it only *warns* about that).
2. **Builds the program** — a small Docker image from the files in the folder.
3. **Guided setup** — plain questions, each one tested for real:
   - *Can this server reach Bradford White?*
   - *How should it alert you?* It makes up a long random [ntfy](https://ntfy.sh) topic for you to subscribe to in
     the phone app, sends a **real test alert** and asks whether it arrived (Home Assistant, Telegram, email and
     a generic webhook work too — pick several if you like). You can't finish without a working channel unless you
     say so on purpose.
   - *Your time zone* (alerts show your local time).
   - *Sign in to Wave, once.* It prints a link; you sign in in your own browser and paste back the address the
     browser ends on (the steps are on screen — the same flow the community
     [Home Assistant integration](https://github.com/gclenaghan/ha-bradford-white-wave#authentication)
     uses). It then lists your water heater with its current mode and setpoint and asks "is that yours?".
   - *The Notifications request* — optional for now; see [Finding the fault request](#finding-the-fault-request).
     If you have it, the installer tests it on the spot and shows what it found.
   - *Review and save.* Secrets are hidden; the settings are written exactly the way Docker reads them back, and
     the installer **checks that the running program sees precisely what you chose** before starting anything.
4. **Starts it and tests it** — starts the container, waits for its first real check of your water heater, then
   prints a health report (service alive, polled the cloud, heater read, alerts delivered, database intact,
   backup made) and asks you to confirm that the "bwwatch started" message reached your phone.

It is safe to stop with **Ctrl-C** at any point and safe to run again: nothing is installed until the end of the
guided setup, a Wave sign-in you already did is kept, and your previous settings are saved as `.env.bak-<time>`
before they are replaced. When something fails, it says what, why it usually happens and the exact next step.

| Command | What it does |
|---------|--------------|
| `./install.sh` | install, or — if bwwatch is already installed here — offer to check it or run the setup again |
| `./install.sh --check` | is it working right now? Changes nothing (`./bwctl verify` is the same report) |
| `./install.sh --reconfigure` | run the guided setup again (alerts, time zone, fault request…); the service is paused during it and restarts afterwards |
| `./install.sh --uninstall` | remove the container and image; asks before it touches your recorded data |
| `./install.sh --help` | the list of options |

> **Tested on:** Docker Engine 29 with Compose 5 on Ubuntu 24.04 — including the real installer, driven through a
> terminal against a real container (see [Tests](#tests)). The scripts are plain POSIX `sh` plus Docker, so a Debian
> VPS should behave the same, but it has not been run on Debian itself. **Not tested:** the live Bradford White
> servers (no credentials were used) — your first run is the real test.

---

## How do I reach it? Ports, nginx, subdomains, the firewall

**You don't, and it needs none of that.** bwwatch has no web page and listens on **no port**: it is a background
service that only makes *outgoing* HTTPS requests (to Bradford White and to your alert service). You use it on the
server with `./bwctl` (over SSH), and it reaches you with push alerts — or a Home Assistant webhook. So there is no
`IP:port` to open (not 56284, not any other), no nginx and no subdomain. `docker-compose.yml` has no `ports:`
line, the installer checks that and then checks that the running container publishes none, and the tests insist on it.
(If you ever add a web page to a project like this, keep it off the public internet — behind a VPN such as Tailscale
or an SSH tunnel to `localhost`. A non-standard port only hides a service from casual scans; it is not protection.)

What the installer checks about ports and the firewall — **read-only; it never changes a rule, a port or a service**:

- The Compose file publishes no port and does not use the host network; afterwards, that the running container
  publishes none either (`docker inspect`).
- Which TCP ports are already in use on the server, shown for the record (`ss`); bwwatch uses none of them and adds none.
- `ufw`: if it is installed and you run the installer as root, it reads `ufw status verbose` — and *only* that;
  it never runs `allow`, `deny`, `enable`, `reload` or anything else. It warns if ufw denies outgoing connections
  (the one thing that could get in the way) and notes if `firewalld` is active. Without root it says so and skips.
- Whether the container really can reach Bradford White is tested in the guided setup, from *inside* the container —
  the real path, whatever the firewall rules say.

**What a firewall must allow:** outgoing TCP 443 (HTTPS) and DNS from the server, to `consumer.bradfordwhiteapps.com`,
`gw.prdapi.bradfordwhiteapps.com` and your alert service (`ntfy.sh`, Telegram, your mail server…). That is the default
on a normal ufw setup. **Nothing inbound is needed**, and because nothing is published there is also nothing for
Docker to punch through ufw (Docker's published ports bypass ufw — a common surprise — but bwwatch publishes none).

**What it adds to the server's networking:** one private Docker network, `bwwatch_default`, which Docker gives a free
address range that does not overlap one already in use; `./install.sh --uninstall` removes it, and an install you
abandon before it starts removes it too. No other network, no firewall rule of its own, no change to Docker's settings.

---

## Everything stays in `/opt/bwheater`

The installer and `bwctl` work only inside the folder they live in. They never write anywhere else on the server —
not `/etc`, not `/tmp`, not your home folder, no systemd unit, no cron job, no link in `/usr/local` — and they never
install software for you. (Tests check this on every run: after each scenario the home folder, the temp folder and the
working directory must still be empty.)

| Where | What |
|-------|------|
| `/opt/bwheater/.env` | your settings (mode 600). Old versions: `.env.bak-<time>` |
| `/opt/bwheater/data/` | the database, daily backups, the sign-in token, a small rotating log (`logs/`) — about 0.3 MB after the first poll |
| `/opt/bwheater/install.log` | what the installer did (no secrets) |
| `.install.out`, `.env.new` | scratch files that exist only while the installer runs |
| **Docker's own storage** (`/var/lib/docker`) | the image `bwwatch:local`, the container `bwwatch` and one private Docker network, `bwwatch_default` (removed again by the uninstall); the container's log is capped at 3 × 10 MB. The Python base image it is built on is cached there too, shared with anything else that uses it |

It never touches any other container, image, volume or network on the server: every Docker command is pinned to the
project `bwwatch` and the folder it lives in (`docker compose -p bwwatch -f /opt/bwheater/docker-compose.yml …`), it
runs no `prune`, mounts nothing but its own `data/`, and refuses to proceed if a container called `bwwatch` that
belongs to a different folder already exists. The container itself is locked down (read-only filesystem, all
capabilities dropped, no network ports, 128 MB / half a CPU limit), and `docker compose down -v` or an image rebuild
cannot delete `data/` because it is a plain folder, not a Docker volume. (Docker's command-line tool may keep its own
usual bookkeeping in `~/.docker`; that is Docker's, not ours.)

**What leaves the server:** the sign-in refresh and a few read requests per hour to Bradford White (the Microsoft
Azure sign-in server `consumer.bradfordwhiteapps.com` and the API `gw.prdapi.bradfordwhiteapps.com`); the alert text
(fault code, heater name…) to the channel *you* chose — ntfy.sh unless you run your own ntfy server, Telegram, your
mail server, Home Assistant or your webhook; the optional dead-man's-switch ping if you configure one; and, once, the
download of the Python base image from Docker Hub when it is built. There is no telemetry, no analytics and no update
check. Your Wave password never reaches the server — you type it only into Bradford White's own page in your browser.

**To remove it all:** `./install.sh --uninstall` (type `DELETE` when asked to also erase `data/`, `.env` and the log),
then `cd / && rm -rf /opt/bwheater` if you want the program files gone too.

---

## Is it lightweight? Does it run a browser?

**No browser, and very little of anything.** bwwatch talks to Bradford White's API directly with ordinary HTTPS
requests from Python's standard library — no Chrome, no Selenium or Playwright, no Node, and not a single extra package
to install (so there is nothing to go stale or be hijacked). The only browser involved is **yours**, once: Bradford White's
sign-in page can't be scripted reliably (the author of the community client tried headless login and gave up), so you
sign in in your own browser, paste back the address it ends on, and from then on bwwatch uses the saved refresh token
against the API.

Measured on a running container: about **20–35 MB of RAM** and ~0 % CPU (it is asleep between polls and keeps no
connection open); the image is the small Debian-based `python:3.12-slim` plus about 300 KB of code; the data folder is
well under 1 MB. The compose file caps it at 128 MB and half a CPU.

---

## Install by hand

You don't need the script. It automates these steps (run in `/opt/bwheater`):

```bash
cp .env.example .env && chmod 600 .env        # then edit .env: at least one alert channel (see .env.example)
docker compose build                          # about a minute the first time
docker compose run --rm bwwatch login         # sign in once, in your browser
docker compose run --rm bwwatch test-notify   # does a test alert reach your phone?
docker compose run --rm bwwatch check         # read everything once and show what bwwatch understands
docker compose up -d                          # run it, always
docker compose exec bwwatch bwwatch verify --wait 120    # did it really work?
```

The quickest alert channel is [ntfy](https://ntfy.sh): install the ntfy app, make up a long random topic name
(`openssl rand -hex 12`), put it in `NTFY_TOPIC=` and subscribe to that topic in the app. Telegram, email, a generic
webhook and Home Assistant work too — all documented in [`.env.example`](.env.example). **No Wave password goes in
`.env`.** The `login` command prints a link; sign in, and when the browser ends on an error page (that's expected:
it is trying to open the phone app), open the developer tools (F12 → Network), click the failed/302 request and copy
its **`location`** response header — it starts with `com.bradfordwhiteapps.bwconnect://oauth/redirect?...` — and paste
it back. The code in it works once and expires within minutes. Edited `.env`? `docker compose up -d --force-recreate`
(or `./bwctl restart`) makes the service read it again.

---

## Finding the fault request

The Notifications tab in the app loads its list with a request that nobody has published. You need its
path (and parameters) once. The known calls look like this, so the missing one probably does too:

```
GET /wave/getApplianceList?username=<your account id>
GET /wave/getApplianceStatus?macAddress=<heater MAC>
```

**Option A — read it out of the app (no traffic capture needed).** The Wave app is built with Flutter
(the community client identifies itself as `Dart/3.8`). Flutter apps ignore a phone's proxy settings and
don't trust user-installed certificates, so ordinary capture apps usually show nothing useful. But Flutter
keeps API paths as plain text in the app's `libapp.so`:

```bash
adb shell pm list packages | grep -i -E 'bradford|wave'     # find the package name
adb shell pm path <package>                                  # then `adb pull` each path it prints
unzip -p base.apk lib/arm64-v8a/libapp.so > libapp.so        # (the split that contains lib/)
strings -n 6 libapp.so | grep -i -E '/wave/|get[A-Za-z]*(Notif|Fault|Alert|Alarm|Event|History)'
```

You're looking for another `get…` name next to `getApplianceList` / `getApplianceStatus`, such as
`getNotifications`. (This is a general technique; it may not work on every build.)

**Option B — capture the app's traffic** on a rooted phone or an emulator, using a tool that can bypass
Flutter's certificate handling (e.g. reFlutter or a Frida script). Heavier, but shows the exact request.

**Option C — let bwwatch guess.** The guided setup offers it, or run `./bwctl probe --yes`: about 14 read-only
`GET`s (guessed names such as `getNotifications`, `getFaultHistory`, 3 seconds apart, once) are sent and any that exist
are reported. It only ever sends `get…` requests that pass the read-only guard. It may find nothing.

**Then test and set it.** Easiest: `./install.sh --reconfigure` and choose "I have it" at step 5 — it runs the request
for real and shows the entries it found. By hand:

```bash
./bwctl call "GET /wave/getNotifications?username={account_id}&macAddress={mac}"
```

When the answer is your notification list, put the same text in `.env`:

```
BW_FAULT_REQUEST='GET /wave/getNotifications?username={account_id}&macAddress={mac}'
```

(Single quotes, because the value contains `&`, `{` and spaces; the setup writes them for you.) Placeholders:
`{account_id}` `{mac}` `{serial}` `{name}`. For a `POST`, add a JSON body:
`BW_FAULT_REQUEST='POST /wave/getNotifications {"mac_address": "{mac}"}'`. Then run `./bwctl check` — it should list
the existing entries — and `./bwctl restart`.

On the first poll after you set it, existing entries are recorded as *pre-existing* without alerting
(one summary message tells you the most recent ones). Only entries that appear after that raise an alert.
If the notification list also holds non-fault messages, narrow it with `BW_FAULT_MATCH` (a regular
expression). If `check` says it can't recognise the response format, set `BW_FAULT_LIST_PATH` (and, if
needed, the other `BW_FAULT_*` options) — the raw response is stored either way, so nothing is lost.
Even an unrecognised response still triggers an alert when it *changes*.

---

## Home Assistant

bwwatch can call a **webhook** in Home Assistant whenever a fault is detected. (The guided setup asks for the
address and sends a test fault to it.)

**1. Create an automation** with a webhook trigger (Settings → Automations → Create → *Webhook*), or paste
this YAML (Home Assistant 2024.10+; older versions use `trigger:` / `platform:` / `service:`):

```yaml
alias: Water heater fault
description: Sent by bwwatch when the Wave cloud reports a new fault
triggers:
  - trigger: webhook
    webhook_id: "PASTE-A-LONG-RANDOM-ID"   # works like a password - make it long and keep it secret
    allowed_methods: [POST]
    local_only: false                      # REQUIRED: bwwatch calls from the internet
conditions:
  - condition: template
    value_template: "{{ trigger.json.event == 'fault' }}"
actions:
  - action: notify.mobile_app_YOUR_PHONE
    data:
      title: "{{ trigger.json.title }}"
      message: "{{ trigger.json.message }}"
      data: { priority: high, ttl: 0 }
mode: queued
```

> Webhook triggers default to *local network only*, and Home Assistant doesn't tell the caller when it
> ignores a webhook, so a wrong setting can fail silently. Leave `local_only: false` (UI: turn off *Only
> accessible from the local network*).

**2. Give the server a way to reach it.** A VPS can't see your home network. Options:
- **Home Assistant Cloud (Nabu Casa):** enable the cloud hook for the webhook (Settings → Home Assistant
  Cloud → Webhooks, or the *Public URL* button in the trigger; menus move between versions) to get an
  `https://hooks.nabu.casa/…` address.
- **Tailscale** (or WireGuard) on both machines: use the private address,
  `http://100.x.y.z:8123/api/webhook/<id>`. The tunnel encrypts it.
- Your own HTTPS reverse proxy or Cloudflare Tunnel. Don't expose plain-HTTP Home Assistant to the internet.

**3. Put the address in** the guided setup (`./install.sh --reconfigure`, choose Home Assistant) **or** in `.env`:

```
HA_WEBHOOK_URL=https://hooks.nabu.casa/<long-id>        # or https://your-ha.example/api/webhook/<id>
HA_WEBHOOK_EVENTS=fault,health                          # optional: only these kinds (default: all)
```

**4. Test it** without waiting for a real fault: `./bwctl test-notify --event fault`
sends a clearly labelled test fault (code `99`) to every channel; open the automation's *Traces* to see it.

**What your automation receives** (`trigger.json`):

| Field | Meaning |
|-------|---------|
| `source` | always `"bwwatch"` |
| `event` | `fault` · `health` (bwwatch is failing / needs sign-in) · `recovered` · `cleared` · `setting` (mode/setpoint changed) · `info` |
| `title`, `message` | ready-to-show text |
| `priority` | 1 (quiet) … 5 (urgent) |
| `time` | when bwwatch created the alert (UTC) |
| `appliance` | `{name, mac, serial}` — on fault alerts |
| `fault` | `{id, code, description, occurred_at, detected_at, kind, source}` — on fault alerts |

Examples: `{{ trigger.json.fault.code }}`, `{{ trigger.json.appliance.name }}`. The webhook address is never
written to logs or error messages.

---

## How often it contacts Bradford White

**bwwatch never talks to the water heater itself.** It talks to Bradford White's cloud — the same servers
the phone app uses — and the heater keeps talking to that cloud on its own. There is no push feed available
to it, so bwwatch checks on a timer and is completely idle in between (no connection is kept open).

Per poll, with the default hourly schedule and one heater:

| Request | Per poll | Per day |
|---------|---------:|--------:|
| Sign-in token refresh (Microsoft Azure sign-in server, `consumer.bradfordwhiteapps.com`) | 1 | 24 |
| `getApplianceList` | 1 | 24 |
| `getApplianceStatus` (per heater) | 1 | 24 |
| Your fault/notification request (once configured) | 1 | 24 |
| **Total** | **4** | **96** |

For comparison, the community Home Assistant integration (from its source) polls status every 60 seconds
(list + status) plus energy usage every 5 minutes — roughly **3,700 requests a day** for one heater.
bwwatch's default is about 1/40th of that, and even at its fastest allowed setting it is under a third.

Built-in limits (all enforced in code and covered by tests):

- **Never faster than every 5 minutes** (`BW_POLL_INTERVAL_SECONDS` below 300 is rejected) — and that
  floor holds *across restarts*, so a crash loop or repeated `docker restart` can't cause rapid polling.
  (After a restart or reconfigure the first poll may be held back for a few minutes for exactly this reason;
  the installer tells you when that is what it is waiting for.)
- **Obeys `429 Too Many Requests`** and its `Retry-After` — no retry within the poll, and the next poll waits.
- **Backs off** after 3 failed polls in a row (never *faster*; with the hourly default it stays hourly).
- **Retries are tiny:** at most 3 attempts per request, a few seconds apart, only for network/5xx errors.
- **Stops hammering a rejected sign-in:** after the sign-in server refuses your token, bwwatch alerts you
  and does *not* contact it again until you run `login` (plus one retry every 6 hours in case it was a hiccup).
- The only commands that send more than a handful of requests are the ones you run by hand: `probe`
  (about 14, once, only with `--yes`).

Honest limits: Bradford White doesn't publish rate limits, so nobody can *promise* you won't be limited or
blocked — only that this is far below what the common integration already does. Cloud services also
sometimes treat datacenter (VPS) addresses more suspiciously than home ones. If `check` is refused from the
server but works from home, run the same compose file on a Raspberry Pi or home PC instead.

---

## Read-only guarantee

You asked that this tool never change your heater's configuration. The Wave API changes settings with
ordinary `GET` requests (`changeSetpoint`, `changeOpMode`), so "only GET" would not be enough. Instead:

- Every request to the Wave API passes [`bwwatch/readonly.py`](bwwatch/readonly.py) — a short file you can read
  in a few minutes — **when your settings are loaded and again immediately before sending**, before any
  sign-in or network traffic.
- A request is refused if its method isn't `GET`/`POST`; if *any* path segment contains an action word
  (`change`, `set`, `update`, `delete`, `reset`, `clear`, `ack`, …); if the endpoint name doesn't read as a
  read (`get…`, `list…`, `…history`, `…notifications`); if the query/body carries `temperature`, `mode`,
  `setpoint`; or if a `{placeholder}` sits in the path.
- **There is no setting, flag or environment variable that turns this off.** The commands `call` and
  `probe`, and the guided setup, use the same guard.
- The only other things it sends: the sign-in token refresh (OAuth), your own alerts, and the optional
  heartbeat ping. (The installer's "can this server reach Bradford White?" step is one plain `GET` of the
  sign-in page, with no token, to see whether the server answers.)
- It reads what the status call returns — mode, setpoint, and any temperature fields — and records them
  every poll. (Per the community client's notes, the status response does **not** include the tank
  temperature; any temperature field that does appear is recorded.)

Tests prove this: write endpoints are refused before anything is sent, a mock server that *would* change the
heater is never touched during normal operation — including by the installer's guided setup and by the real
container in the end-to-end test — and a source-level test ensures nothing else can open a connection to the Wave
host. The one caveat is inherent: the guard judges endpoint **names**, and the API is undocumented. That's why only the
two built-in reads plus the single request *you* configure are ever used — paste only a request the app makes when it
merely *shows* notifications. If a genuine read is refused because its name isn't recognisable, that is deliberately a
code change (add the word in `readonly.py`), not a setting.

---

## Your data and how it is protected

Everything lives in `/opt/bwheater/data` on the host (a plain folder, so `docker compose down -v` and image
rebuilds can't delete it; it belongs to the container's own unprivileged user, mode 700, so use `./bwctl` rather than
reading it directly):

| File | What |
|------|------|
| `bwwatch.db` (+ `-wal`, `-shm`) | the log: every poll, fault, setting reading, queued alert |
| `backups/` | verified copies, daily, newest 14 kept |
| `token.json` | the rotating sign-in token (private, never in backups or exports) |
| `logs/` | a small rotating text log (about 5 MB at most); `LOG_FILE=` in `.env` moves or disables it |
| `corrupt/` | a damaged database, if one is ever found (kept, never deleted) |
| `status.json`, `bwwatch.lock` | liveness and single-instance lock |
| `env.generated`, `env.expected.json` | only during setup; removed by the installer |

**Why an unexpected shutdown can't corrupt it:**
- SQLite in **WAL mode with `synchronous=FULL`**: a commit is on disk before it is acknowledged; a crash or
  power loss can lose an *unfinished* write but never leaves the file half-written.
- **One transaction per poll.** The poll, its faults, its readings *and the alerts they require* commit
  together. A fault is therefore never recorded without its alert being queued, and a half-finished poll
  simply doesn't exist and is redone.
- Alerts are queued in the database and retried until delivered, so a network outage can't lose one.
- The token file is replaced atomically (write, fsync, rename), and a rotated token is saved *before* use.
- **Integrity check at start-up and daily; verified backups** made with SQLite's online backup API (never by
  copying a live file). If damage is ever found, the file is moved to `corrupt/`, the newest good backup is
  restored, and you get an alert.
- `docker stop` shuts down cleanly (finishes the write, folds the WAL into the database). A hard kill is
  survived too.

**Tested, not just claimed:** repeated `kill -9` at random moments while the service writes as fast as it
can; after every kill the database reopens cleanly, passes `integrity_check`, keeps every commit it had
acknowledged, and satisfies cross-table invariants. The same tests *fail* if the transactions are sabotaged.
The real container is also killed from outside and brought back by Docker's restart policy in the end-to-end test.
What a test can't prove is a **power cut**: that depends on your VPS's disk honouring `fsync` (which
`synchronous=FULL` relies on). On a VPS this is normally fine; for extra safety copy `/opt/bwheater/data/backups` off
the machine now and then (they're consistent files, safe to `rsync`; they contain your appliance details but no
sign-in token).

**Rules of thumb:** don't `cp` the live `bwwatch.db` (use `./bwctl backup`); don't put `data/` on a network
share; keep one instance running (a second one refuses to start).

**Restoring by hand** (normally unnecessary — it self-recovers), from `/opt/bwheater`:

```bash
./bwctl stop
docker compose run --rm --entrypoint sh bwwatch -c 'cp /data/backups/bwwatch-<timestamp>.db /data/bwwatch.db && rm -f /data/bwwatch.db-wal /data/bwwatch.db-shm'
./bwctl start
```

**Privacy:** the database holds your heater's name, MAC address, serial number and the raw responses. Treat
exports (`export faults`) and backups accordingly before sharing them with a plumber or on a forum.

---

## Day-to-day

Run `./bwctl` from `/opt/bwheater` (or call it by its full path, `/opt/bwheater/bwctl`, from anywhere). It uses the
running container, or a one-off one if the service is stopped.

| `./bwctl …` | What it does |
|-------------|--------------|
| `status` | one-screen summary: service health, last poll, heater settings, faults, pending alerts, backups |
| `faults [--limit N] [--all] [--raw]` | the logged faults, newest first |
| `export faults\|polls\|readings [--out FILE]` | CSV, e.g. for a warranty claim: `./bwctl export faults > faults.csv` |
| `check` | read everything once and show what bwwatch understands (writes nothing) |
| `login` | sign in again (needed if you get the *"sign-in needs attention"* alert) |
| `call "<request>" [--mac MAC]` | one read-only request, answer printed — to try a request before putting it in `.env` |
| `probe --yes` | look for the notifications endpoint (optional) |
| `test-notify [--event fault]` | send a test alert to every channel |
| `backup` · `dbcheck` | make a verified backup now · verify the database and list backups |
| `verify [--wait N]` | end-to-end check: service alive, polled the cloud, heater read, alerts delivered, backups made |
| `logs [-f]` | what the service is doing |
| `start` · `stop` · `restart` | control the service (`restart` makes it re-read `.env`) |
| `update` | after a `git pull`: rebuild the image, restart, and check |
| `reconfigure` · `uninstall` | the same as `./install.sh --reconfigure` / `--uninstall` |

Under the hood these are the container's own commands — `docker compose exec bwwatch bwwatch <command>` — which are:
`run`, `login`, `check`, `call`, `probe`, `status`, `faults`, `export`, `backup`, `dbcheck`, `test-notify`, `verify`,
`setup` (the guided setup wizard; `install.sh` runs it for you), `healthcheck` (exit 0 if the service is alive; Docker
uses it) and `version`.

**Alerts you may receive:** *Water heater fault N — name* (a new fault); *Wave sign-in needs attention —
faults are NOT being monitored* (run `./bwctl login`); *Wave monitoring is failing* / *working again*;
*Water heater setting changed* (e.g. mode `Heat Pump → Electric` — handy if the heater falls back);
*Fault flag cleared*; *bwwatch database was damaged and has been recovered*; *Watching …* / *bwwatch
started*. Faults are priority 4 by default (`FAULT_PRIORITY`).

**Updating:** `cd /opt/bwheater && git pull && ./bwctl update`.

---

## Troubleshooting

**The installer**

- **"Docker is installed, but you are not allowed to use it."** Run it as root (`sudo ./install.sh`). Adding your user
  to the `docker` group also works but gives that user root-level power over the server, so it's your call.
- **"Docker Compose v2 is missing."** Install Docker the way Docker documents it for your system
  ([Debian](https://docs.docker.com/engine/install/debian/)); that includes the `docker-compose-plugin` package. The old
  standalone `docker-compose` is not supported.
- **"A container named bwwatch already exists and belongs to another folder."** An earlier copy of bwwatch is running
  from somewhere else. Go to that folder and run `docker compose down`, then run the installer again. It will never
  remove a container it did not create.
- **The build fails.** Almost always the server couldn't download the Python base image: check DNS and internet access
  on the server, or Docker Hub's download limit; try again later. The full output is in `install.log`.
- **"Could not reach the Wave sign-in server."** Check the server's internet connection and DNS, its clock (a wrong date
  breaks HTTPS) and any firewall or proxy rules — the installer's firewall report (Part 1) may already have pointed at
  one (for example *"ufw denies outgoing connections by default"*). Only outgoing HTTPS and DNS are needed.
- **The sign-in paste is rejected.** The code in that address works once and expires within minutes. At the prompt type
  `new` for a fresh link, sign in again, and paste the new address promptly.
- **The test alert doesn't arrive.** The setup offers to resend it, change the settings, or skip. For ntfy the topic in
  the app must match the one shown exactly; on Android allow notifications for the ntfy app and turn off battery
  optimisation for it. For Home Assistant check `local_only: false` and the automation's *Traces*.
- **The installer stopped half-way** (Ctrl-C, closed terminal, error). Run `./install.sh` again. A paused service is
  restarted automatically if you cancel a reconfigure.
- **"First poll: held back for N more seconds."** After a restart bwwatch never polls Bradford White within 5 minutes
  of its previous poll. Nothing is wrong; check again with `./install.sh --check` later.

**The service**

- **"Wave sign-in needs attention."** The sign-in server rejected the saved token (refresh tokens can
  expire or be revoked). Run `./bwctl login`. bwwatch pauses contacting the sign-in server until you do.
- **No alerts arrive.** `./bwctl test-notify` shows each channel's result (and `./install.sh --check` whether any were
  delivered).
- **`check` is blocked/403 from the server but works from home** — see the note on VPS addresses above.
- **Something looks wrong.** `./bwctl logs`, then `./bwctl status` and `./bwctl dbcheck`.
- **Using a different Wave account / starting over.** `./install.sh --uninstall` and answer `DELETE` (or delete
  `data/token.json` to only change account), then `./install.sh`.

---

## How it works

```
every hour:  refresh token ─▶ list appliances ─▶ status (+ fault request) ─▶ ONE transaction:
                                 poll · settings · faults · queued alerts ─▶ deliver alerts ─▶ idle
```

`bwwatch/` is plain Python (3.9+, standard library only): `readonly.py` (the guard), `wave.py` (sign-in and
API), `cycle.py` (one poll), `service.py` (the loop, backups, health), `db.py` (storage safety), `faults.py`
(lenient parsing of the undocumented responses), `notify.py` (channels), `wizard.py` (the guided setup),
`verify.py` (the health report), `probe.py` (the optional endpoint search), `cli.py` (commands).
`install.sh` and `bwctl` are short POSIX shell scripts that only start the container and show its output.

### Tests

```bash
python3 -m unittest discover -s tests -t .      # about 380 tests, no installs needed, about a minute
```

They run against a mock Wave cloud and mock alert services: sign-in rotation, retries, rate limits, every alert
channel, failure alerts, `kill -9` crash safety, privilege drop, the read-only guard, the guided setup (scripted
conversations, including every Ctrl-C point), the settings file's quoting rules, the health report, and the
installer scripts themselves — run for real against a pretend `docker`, over a pseudo-terminal, with checks that
nothing is written outside the install folder. If `shellcheck` is installed it is run over both scripts too.

An opt-in end-to-end test drives the **real installer against a real Docker daemon**: it builds the image, answers the
wizard's questions through a terminal, starts the container, runs `bwctl`, restarts, crashes and reconfigures it, then
uninstalls it and checks that decoy containers and volumes next to it were left alone:

```bash
BWWATCH_DOCKER_E2E=1 python3 -m unittest tests.test_docker_e2e -v      # needs Docker (Linux); takes about two minutes
```

**Not verified:** the live Bradford White servers (no credentials were used), the real fault request (unknown), and
real ntfy / Telegram / SMTP / Home Assistant services (tested against local imitations). Your first
`./install.sh` is the real test.

## Credits and disclaimer

The sign-in flow and API calls were worked out by the community — thanks to Graham Clenaghan's MIT-licensed
[ha-bradford-white-wave](https://github.com/gclenaghan/ha-bradford-white-wave) and
[bradford-white-wave-client](https://github.com/gclenaghan/bradford-white-wave-client). bwwatch is an
independent, read-only implementation and shares no code with them. It is unofficial and not affiliated with
Bradford White; the API is undocumented and may change. It is a monitoring aid — **not a safety device.**
Don't rely on it for anything where a missed alert could hurt someone (gas, leaks, scalding); install proper
leak and temperature protection too.
