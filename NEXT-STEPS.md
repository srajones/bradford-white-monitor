# What is left to do

Written 2026-10-05, after the first real look inside the Wave app. Start with section 1: it needs you, not code.

## 0. Where things stand

**Working and pushed:** the guided installer (`/opt/bwheater`, nothing outside it), the setup wizard, hourly polling with a
5-minute floor, alerts (ntfy / Telegram / email / webhook / Home Assistant), the crash-proof SQLite log with daily
backups, the read-only guard (no code path can change the heater), tracking of faults that clear on their own, a history
of **every field** the cloud reports (`./bwctl fields`, `./bwctl changes`), and a **log of every request** made to the cloud
(stored in the `api_calls` table; there is no viewer for it yet).

**The one thing between you and "Fault 10" alerts:** `BW_FAULT_REQUEST` is not set, so bwwatch cannot read the
Notifications list yet. That request is now known (section 1).

## 1. Found: the Notifications request (do this first, 5 minutes, no code)

I read the Wave app itself (version 1.1.3371, the `.apks` you put in Dropbox; it is a Flutter app, and its Dart code
still contains the names of every request it makes). Facts, all read straight from the app:

| What | Finding |
| --- | --- |
| **The Notifications screen reads** | **`/wave/getApplianceErrors`** (Dart: `notifications_controller.dart` → `ApplianceErrorsResponse` → `ApplianceErrorHistory`, also `ApplianceActiveError`) |
| Names the app's code uses for the answer (the exact layout is only confirmed by a real answer) | `error_history` (the list), `error_code`, `error_string`, `timestamp` |
| Other reads the app makes | `getApplianceList`, `getApplianceStatus`, `getApplianceDetails`, `getEnergyUsage` (POST), `getSchedules`, `getAccountDetails`, `gettous` |
| Requests that **change** things (bwwatch refuses all of these) | `changeSetpoint`, `changeOpMode`, `setSchedules`, `renameAppliance`, `changePassword`, `changeAccountDetails`, `registerAppliance` |
| Parameter names the app uses | `macAddress`, `mac_address`, `serialNumber`, `username`, `view_type` — the strings do not say which one belongs to `getApplianceErrors` |
| Push notifications | **none** — the app contains no push-message library (no Firebase Cloud Messaging, no OneSignal/Braze/Azure-hub style SDK, no local-notification plugin). That is why it never warns you: it only fetches the list while you have the Notifications screen open |

Try these on the VPS, in this order, until one prints JSON containing `error_history`. Each is one harmless read:

```sh
cd /opt/bwheater
./bwctl call "GET /wave/getApplianceErrors?macAddress={mac}"
./bwctl call "GET /wave/getApplianceErrors?mac_address={mac}"
./bwctl call "GET /wave/getApplianceErrors?macAddress={mac}&serialNumber={serial}"
```

(`{mac}` and `{serial}` are filled in from your first heater. `--mac AA:BB:..` picks another heater, but then only the first two forms work, because `--mac` leaves `{serial}` empty.)

Then turn it on: put the line that worked in `/opt/bwheater/.env` and restart:

```sh
BW_FAULT_REQUEST=GET /wave/getApplianceErrors?macAddress={mac}      # use the form that worked
./bwctl restart
./bwctl verify
```

Your existing Fault 10 entries are recorded silently as history (no flood of alerts); anything **active**, and anything
new from then on, alerts. **Please paste the JSON of the first request that worked** (blank out the MAC if you like): the
cleared/active wording is the one detail I could not read from the app, and a real sample lets me tune it (item 2 below).

## 2. Still to build, in priority order

### Do next (small)

1. **Let bwwatch find the working form by itself.** With `BW_FAULT_REQUEST` unset, try the three forms above once (at most
   3 requests, 3 seconds apart, each passing the read-only guard), keep the first that answers with JSON, remember it in the
   database (`meta` key `learned.fault_request`), and tell you with an info alert and in `status`. An explicit
   `BW_FAULT_REQUEST` always wins. `./bwctl discover --reset` forgets it. *This replaces the earlier plan of guessing ~100
   endpoint names: the name is known now.* Keep that bigger guesser only as an optional fallback if Wave ever renames it.
2. **Tune the parser on a real answer.** Already done (pushed): the list key `error_history` and the text key `error_string`
   are recognised. Still open: where the **"(Cleared)"** state lives in the JSON (a flag? a status word? part of the text?),
   and whether the app's separate `ApplianceActiveError` (the fault that is active right now) is its own field outside the
   list; if so, treat it as an *active* entry. Needs the sample from section 1. Add a test with that sample.
3. **Replace the "request not found yet" messaging** in the wizard (step 5), the installer's closing reminder,
   `verify` ("Fault history" line), the start-up info alert and the README "Finding the fault request" section with the
   simple truth: *the request is `getApplianceErrors`; bwwatch uses it automatically.*

### Then

4. **Faster polling while a fault is active.** `BW_FAULT_POLL_INTERVAL_SECONDS` (default 600, never below 300) while a fault
   is active, for at most `BW_FAULT_POLL_HOURS` (default 12) from when it was first seen; normal interval otherwise.
   Needs `Outcome.active`, `Service.next_delay`, tests, `.env.example`, README. (The interval itself is already a setting:
   `BW_POLL_INTERVAL_SECONDS`, default 3600, minimum 300.)
5. **`./bwctl calls`** — show the request log (requests per endpoint in the last 24 h, status codes, speed, any 429
   "slow down" answers) and a "Requests, last 24h" line in `status`. The data is already being stored.
6. **Energy use.** `POST /wave/getEnergyUsage` with `{"mac_address": "<mac>", "view_type": "hourly|daily|weekly|monthly"}`
   returns `timestamp`, `total_energy`, `heat_pump_energy`, `element_energy`, `reported_minutes`. The `energy_usage` table
   exists and is empty. Plan: hourly view at most every 55 minutes, daily every 23 h, weekly/monthly every 6 days, at most
   two views per poll, errors never fail the poll, `./bwctl energy` to view, include in `export`. A rise in `element_energy`
   (backup element running) while a fault is open would be a very useful clue.
7. **More read-only data to log field by field:** `getApplianceDetails` and `getSchedules` (settings `BW_LOG_REQUEST_1..5`
   were removed from the code until this is built). **Not** `getAccountDetails` by default: it holds personal details.
8. **`./bwctl apkscan`** is now optional. The by-hand method that found the above is in section 3. Build the command only
   if you want a one-step re-check after Wave updates the app (it would run inside the container with the APK folder
   mounted read-only, and `apk/` is already in `.gitignore`).
9. **Final verification pass:** `shellcheck`, the whole test suite, and the real-Docker end-to-end run
   (`BWWATCH_DOCKER_E2E=1`, bridge and host networking) after items 1-7; update README commands table and `.env.example`
   for every new setting (the tests in `tests/test_project_files.py` fail until they match).

## 3. How the request was found (so it can be repeated after an app update)

1. Download the app bundle (`.apks`/`.xapk`/`.apk`) from any APK site on a normal computer. It is a zip of zips.
2. `base.apk` holds the Java side; `split_config.arm64_v8a.apk` holds `lib/arm64-v8a/libapp.so`, the compiled Dart code.
3. Print every run of readable text (4+ characters) in `libapp.so` and search it: `wave/` shows every request path,
   `package:bwconnect/` shows the app's own source files (`screens/notifications/...`), and `grep` for `error_`,
   `macAddress`, `mac_address` shows names and parameters.
4. No push-message classes in `classes.dex` and none in the Flutter plugin list (`package:` names in `libapp.so`) means the
   app cannot push.

Nothing was run, installed or logged in; this is only reading text inside a copy of the public app. The APK is not in the
repo, and `apk/` plus `*.apk`/`*.apks`/`*.xapk`/`*.apkm` are git-ignored so one cannot be committed by accident.

## 4. Rules every change must keep (yours, verbatim in spirit)

- **Read-only, no override.** Nothing may ever change a setting on the heater. `bwwatch/readonly.py` is the single gate; it
  refuses every `change…`/`set…`/`rename…`/`register…` request, and there is no switch to turn it off.
- **Your Wave password is never stored anywhere.** One browser sign-in, then the rotating refresh token.
- **Gentle on Bradford White:** 5-minute floor, honour 429/Retry-After, back off after failures, few requests per poll.
- **Everything stays in `/opt/bwheater`.** Nothing installed on the host, no other containers, images, volumes or networks
  touched; the firewall check only reads.
- **Never create a pull request unless asked;** work lands on `claude/gracious-dijkstra-9wx2ac`.

## 5. Honest limits right now

- I cannot test against your real account from here (your password and token never leave your server), so the exact
  parameter form and the JSON of `getApplianceErrors` are confirmed only once you run section 1.
- Until `BW_FAULT_REQUEST` is set, bwwatch still alerts on heater settings changes and on fault-like fields in the status
  data, and it logs every field, but it cannot see the Notifications list.
