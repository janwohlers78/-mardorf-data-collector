# External collector watchdog

The public collector uses **cron-job.org as the primary independent heartbeat**. GitHub's built-in schedules remain an independent fallback because scheduled Actions have previously been delayed.

## Single external endpoint

The existing cron-job.org job should continue to call:

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

Recommended cron-job.org cadence remains every 20 minutes.

## Due-aware routing

The watchdog no longer blindly dispatches all three collector workflows. One short router job:

1. reads the verified private success pointer for SVG, models and the atomic secondary batch;
2. calculates the actual age of each source;
3. checks whether that source is due;
4. checks whether the corresponding collector is already queued or running;
5. dispatches only a due and idle collector with `watchdog=true`.

The router itself performs **no provider download, no Python dependency installation and no ecCodes installation**. Its normal no-op path is only checkout + three standard-library freshness checks + the dispatch decision.

Freshness thresholds:

- SVG: 40 minutes
- Models: 150 minutes
- Secondary atomic batch: 240 minutes

Every child collector repeats its own freshness gate before provider access. This deliberate second check closes the race between the router decision and child startup.

The due-check remains fail-open: if verified private state cannot be read, collection is requested rather than silently allowing data to age. The router additionally suppresses duplicate dispatch while a child workflow is already queued or running.

## GitHub fallback schedules

GitHub-native schedules are retained only as an independent backup path:

- SVG: hourly at minute 13 UTC
- Models: every 3 hours at minute 23 UTC
- Secondary: 00:47, 04:47, 10:47, 16:47 and 22:47 UTC

They use the same child freshness gates. Therefore either scheduler can recover the system without creating a separate acquisition implementation.

Manual `workflow_dispatch` of an individual collector still leaves `watchdog=false` by default and intentionally bypasses the freshness gate for diagnostics or explicit validation.

## Actions accounting

GitHub's current billing documentation states that standard GitHub-hosted runners in **public repositories are not billable Actions minutes**. The Actions usage page can still show runtime/job counts for this repository; those figures should not be added to the private repository's billable-minute budget as if they were private-runner minutes.

The router nevertheless avoids the previous 20-minute fan-out of four public jobs per heartbeat and substantially reduces run noise and provider-dispatch churn.
