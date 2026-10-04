# bwwatch — Bradford White Wave fault watcher

A small, always-on Docker Compose service for a VPS. It signs in to the
Bradford White **Wave** cloud (the same service the phone app talks to), checks
your water heater for fault codes on a schedule (hourly by default), **logs
every fault to a crash-safe SQLite database**, and **pushes a notification to
your phone** — because the Android app doesn't.

> **Status: work in progress.** This first commit sets up the repository, the
> safety rules for secrets, and the plan. The watcher, `Dockerfile` and
> `docker-compose.yml` land in the following commits, and this README is
> updated as each piece lands. Nothing below is claimed to work until it says so
> in the "Status" notes of the relevant section.

## Goals

| Need | How it is handled |
|------|-------------------|
| Know when a fault code appears | Push notification (ntfy / Telegram / email / webhook), sent once per new fault |
| Keep a record of every fault | Append-only `faults` table in SQLite, plus a CSV export for the plumber / warranty claim |
| Never corrupt the log | SQLite in WAL mode with `synchronous=FULL`, one atomic transaction per poll, integrity checks, automatic verified backups, clean shutdown on `SIGTERM` |
| Never fail silently | The watcher alerts you when *it* can't log in or reach the API, and (optionally) pings an external dead-man's-switch |
| Lightweight | One small container, Python standard library only (no pip dependencies, no extra services) |

## Secrets

Everything sensitive lives in a `.env` file **on your VPS only**:

- `.env` is git-ignored — see [`.gitignore`](.gitignore). Only `.env.example`
  (placeholders, no real values) is ever committed.
- Do not paste your Wave password, tokens, or a login redirect URL into chat,
  issues or pull requests.
- Lock the file down on the server: `chmod 600 .env`.

## Planned layout

```
bwwatch/            Python package (stdlib only)
tests/              Unit + integration tests, including a mock Wave server
Dockerfile
docker-compose.yml
.env.example
```
