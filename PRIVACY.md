# Privacy and what this repository does not contain

This project talks to a cloud account and controls hardware at a physical address,
so it is worth being explicit about what is and is not published here.

## Public source files

* No credentials of any kind — no Deye app id or secret, no login, no password, no
  API tokens. `.env.example` contains placeholders only.
* No inverter or logger serial numbers.
* No site coordinates or address. `SITE_LATITUDE` / `SITE_LONGITUDE` are `0.0`.
* No measured telemetry, production history, consumption history or order ids from
  any real installation. Values in tests are synthetic fixtures.
* No state database, log, JSONL journal or diagnostic bundle.

## Keeping runtime data out of commits

`.gitignore` excludes real environment files (with explicit exceptions for the
placeholder template and synthetic `tools/demo.env`), credentials, databases and
their WAL/SHM sidecars, state snapshots, logs, archives and diagnostic directories.
Ignore rules do not remove a file that is already tracked, and `git add -f` can
bypass them.

Before publishing anything, check that nothing sensitive is staged:

```bash
git diff --cached --name-status
git diff --cached
git ls-files --others --exclude-standard
python3 tools/privacy_check.py
# Optional: compare privately against this installation, without printing values.
sudo python3 tools/privacy_check.py --compare-env /etc/deye-solar-optimizer/deye.env
# Also audit all blobs reachable from the branch to publish.
sudo python3 tools/privacy_check.py --compare-env /etc/deye-solar-optimizer/deye.env --history main
```

Credential field names legitimately appear in source, tests, the placeholder
template and the synthetic demo. Check values, not just names. Compare against
private deployment values locally without printing them. Review documentation,
test fixtures, screenshots and commit author metadata as well as executable code.

Audit the history reachable from the exact branch being published. Deleting a
value from today's files does not delete earlier commits. Publish only the
intended branch; avoid `git push --all` and indiscriminate tag pushes. Historical
data requires a separately reviewed history cleanup or a fresh publication
history. Never assume a clean current-file scan proves old public copies clean.

## Your own deployment

Once configured, your installation holds real secrets and real data. Keep them out
of git:

* Runtime configuration lives at `/etc/deye-solar-optimizer/deye.env`, owned
  `root:deyeopt` mode `640`. It is outside the repository. Never copy it in.
* State and logs live under `/var/lib/deye-solar-optimizer/`.
* `deye-day-export` bundles include a **redacted** `.env` (credentials, device/site
  identifiers, coordinates, station IDs, private bind addresses and paths), but they also contain
  your telemetry and forecasts. Treat a bundle as private data before sharing it
  for support.

## The dashboard has no login authentication

Anyone who can reach the dashboard sees telemetry, forecasts, tariffs and battery
state. It binds to `127.0.0.1` by default. The analytics database connection is
read-only, but optional house-mode controls can change inverter settings through
the privileged local Unix-socket broker. These controls default to disabled.
CSRF/Origin/Host checks do not authenticate a user or make a public dashboard safe.

If you expose it, prefer a private network interface (a VPN address, for example)
or a reverse proxy that adds authentication. Binding it to `0.0.0.0` exposes your
household's energy data to every host that can route to you, and optional enabled
mode controls can also be available to dashboard users.
