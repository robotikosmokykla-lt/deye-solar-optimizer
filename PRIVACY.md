# Privacy and what this repository does not contain

This project talks to a cloud account and controls hardware at a physical address,
so it is worth being explicit about what is and is not published here.

## Not in this repository

* No credentials of any kind — no Deye app id or secret, no login, no password, no
  API tokens. `.env.example` contains placeholders only.
* No inverter or logger serial numbers.
* No site coordinates or address. `SITE_LATITUDE` / `SITE_LONGITUDE` are `0.0`.
* No measured telemetry, production history, consumption history or order ids from
  any real installation. Values in tests are synthetic fixtures.
* No state database, log, JSONL journal or diagnostic bundle.

## How that is enforced

`.gitignore` excludes the real `.env`, `deye.env`, `*.db`, `*.log`, `*.jsonl`,
`*.tar.gz` and the `day-check-*` / `deye-check-*` diagnostic directories.

Before publishing anything, check that nothing sensitive is staged:

```bash
git ls-files | grep -E '\.env$|\.db$|\.jsonl$|\.log$'   # must print nothing
git grep -nE 'APP_SECRET=|PASSWORD=' -- ':!*.example'   # must print nothing
```

## Your own deployment

Once configured, your installation holds real secrets and real data. Keep them out
of git:

* Runtime configuration lives at `/etc/deye-solar-optimizer/deye.env`, owned
  `root:deyeopt` mode `640`. It is outside the repository. Never copy it in.
* State and logs live under `/var/lib/deye-solar-optimizer/`.
* `deye-day-export` bundles include a **redacted** `.env`, but they also contain
  your telemetry and forecasts. Treat a bundle as private data before sharing it
  for support.

## The dashboard has no authentication

`dashboard_server.py` is read-only — it opens SQLite in read-only mode and has no
inverter write path — but anyone who can reach the port sees your telemetry,
forecasts, tariffs and battery state. It binds to `127.0.0.1` by default.

If you expose it, prefer a private network interface (a VPN address, for example)
or a reverse proxy that adds authentication. Binding it to `0.0.0.0` publishes your
household's energy data to every host that can route to you.
