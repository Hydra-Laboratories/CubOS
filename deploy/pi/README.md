# CubOS Pi in-app updates

By default (`CUBOS_UPDATE_MODE=tag`) the Operator UI checks origin for the
newest weekly release tag (calver, `vYYYY.MM.DD`, cut by
`.github/workflows/weekly-release.yml` — see "Release cadence" below) rather
than tracking `origin/main` HEAD directly: merging to main ships to CI, not to
appliances, and the banner shows a human-readable tag ("v2026.08.03 →
v2026.08.10") instead of raw SHAs. Set `CUBOS_UPDATE_MODE=branch` to fall back
to the previous behavior of tracking `origin/${CUBOS_UPDATE_BRANCH}` HEAD
directly (useful for a dev/staging appliance that should pick up every merge).

Either way, when an operator chooses **Update & restart**, the API launches
`update.sh` as a detached `cubos-update` systemd unit running as the service
user (`cub`), never as root — repo code (git checkout, pip build hooks, npm
scripts) must not execute with elevated privileges. Root is borrowed only for
the single `systemctl restart cubos` command via its own sudoers entry.
The script checks out the commit the chosen tag (or branch HEAD) points at,
reinstalls the editable CubOS packages, rebuilds the Operator UI when needed,
restarts CubOS, and verifies API health. A failed install, build, restart, or
health check rolls the checkout, Python packages, and (when it changed)
Operator UI bundle back to the previous revision, then re-verifies health.

Updater exit codes, visible in the journal: `0` update succeeded, `1` update
failed and rollback succeeded, `2` rollback failed (manual recovery required),
`3` another update already holds the lock, `64` usage error. A lock in the
repo (`.cubos-update.lock`) serializes updates; a lock left by a dead process
is reclaimed automatically.

## Assumptions

The default appliance layout is:

- repository: `/home/cub/CubOS`
- Python virtual environment: `/home/cub/CubOS/.venv`
- systemd service: `cubos`
- API health endpoint: `http://127.0.0.1:8742/api/v1/health`

The checkout must have an `origin` remote, and the `cub` user must be able to
fetch it without an interactive prompt. `git`, `curl`, and the virtual
environment's `pip` must be installed. `npm` is optional; when it is absent, a
frontend change is logged and the backend update continues.

## One-time setup

From the CubOS checkout:

```bash
chmod +x deploy/pi/update.sh
sudo install -o root -g root -m 0440 deploy/pi/cubos-update.sudoers /etc/sudoers.d/cubos-update
sudo visudo -cf /etc/sudoers.d/cubos-update
```

The API service supports these settings through its environment:

```ini
CUBOS_UPDATE_MODE=tag
CUBOS_UPDATE_BRANCH=main
CUBOS_UPDATE_TAG_PATTERN=^v\d{4}\.\d{2}\.\d{2}$
CUBOS_UPDATE_REPO_DIR=/home/cub/CubOS
CUBOS_UPDATE_SCRIPT=/home/cub/CubOS/deploy/pi/update.sh
CUBOS_UPDATE_SERVICE=cubos
```

`CUBOS_UPDATE_BRANCH` and `CUBOS_UPDATE_TAG_PATTERN` only matter for their
respective mode (`branch` and `tag`).

For a non-default script invocation, `update.sh` also accepts
`CUBOS_REPO`, `CUBOS_VENV`, `CUBOS_SERVICE`, and `CUBOS_HEALTH_URL`. Update the
absolute paths and service name in `cubos-update.sudoers` as well; sudo command
matching is exact. The `--uid=cub` value must name the account the CubOS
service runs as (the API passes its own user). Validate the edited file with
`visudo` before installing it.

After changing the API service environment, reload systemd and restart CubOS:

```bash
sudo systemctl daemon-reload
sudo systemctl restart cubos
```

## Release cadence

`.github/workflows/weekly-release.yml` runs Sundays at 08:00 UTC (and on
manual `workflow_dispatch` for an out-of-band release). It finds the last
`vYYYY.MM.DD` tag, skips the release if `main` has no new commits since then
(quiet weeks produce no release and no update banner), otherwise runs the full
test/lint/build suite and, only if that passes, creates an annotated calver
tag plus a GitHub Release with `git log --oneline <lastTag>..HEAD` as notes.
Appliances in tag mode pick up the new tag on their next `/system/update`
poll; nothing auto-applies it — an operator still clicks **Update &
restart** when convenient.

## Watching an update

The updater writes timestamped progress to its transient systemd unit:

```bash
journalctl -u cubos-update -f
```

On a development machine without `systemd-run`, the API launches the script in
a detached session and appends output to `~/.cubos/update.log`.

## Manual rollback

Find the last known-good revision in the updater journal (a SHA in branch
mode, or the previous release tag — e.g. `v2026.08.03` — in tag mode), then
run:

```bash
cd /home/cub/CubOS
git checkout --detach <previous-sha-or-tag>
/home/cub/CubOS/.venv/bin/pip install -e packages/core -e services/api
sudo systemctl restart cubos
curl --fail http://127.0.0.1:8742/api/v1/health
```

If the rolled-back revision changed `apps/operator-web/`, rebuild that checkout
with `npm ci && npm run build` from `apps/operator-web` before restarting the
service.
