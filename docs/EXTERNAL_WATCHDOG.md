# External collector watchdog

The public collector keeps GitHub's built-in schedules, but scheduled Actions are not treated as the only invocation source.

## Single external endpoint

Configure one external HTTP job against:

`POST https://api.github.com/repos/janwohlers78/mardorf-data-collector/actions/workflows/collector-watchdog.yml/dispatches`

Request body:

```json
{"ref":"main"}
```

Headers:

```text
Accept: application/vnd.github+json
Authorization: Bearer <fine-grained-token>
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

Use a fine-grained token restricted to `janwohlers78/mardorf-data-collector` with only **Actions: Read and write**.

Recommended cron-job.org cadence: every 20 minutes. The watchdog itself never fetches weather/model data. It dispatches:

- `collect-svg.yml` with `watchdog=true`
- `collect-models.yml` with `watchdog=true`
- `collect-secondary.yml` with `watchdog=true`

Each collector then reads verified private state and decides whether acquisition is due.

## Freshness gates

- SVG: 40 minutes
- Models: 150 minutes
- Secondary atomic batch: 240 minutes

GitHub schedules and the external watchdog therefore converge on the same provider-fetch path. A watchdog invocation while data are still fresh exits after the due-check and performs no provider request.

Manual `workflow_dispatch` of an individual collector leaves `watchdog=false` by default and intentionally bypasses the freshness gate for diagnostics/explicit validation.

The due-check is fail-open: if verified private state cannot be read, the collector runs rather than silently allowing data to age.
