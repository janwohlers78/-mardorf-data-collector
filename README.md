# Mardorf Data Collector

Public acquisition-only companion for the private `mardorf-kitevorhersage` project.

This repository intentionally contains **no forecast decision logic, calibration,
traffic-light thresholds, historical private datasets, or user-facing reports**.
It only retrieves source observations/model data and transfers versioned bundles
to the private repository.

## Schedules

- Model due-check: hourly at minute 23 UTC. A full provider acquisition is started only when the last successful private model transfer is at least 150 minutes old; read failures are fail-open.
- SVG/SKM due-check: at minutes 13, 33 and 53 UTC. A real WeatherLink/SKM acquisition is started only when the last successful SVG transfer is at least 50 minutes old; read failures are fail-open.
- Wunstorf/ETNW secondary acquisition: 00:47, 04:47, 10:47, 16:47 and 22:47 UTC. Both child sources are independently audited and transferred; only after both integrity gates pass is one `secondary-batch-receipt-v1` published to the private repository.
- Code changes normally run a reduced model smoke test only; smoke tests never transfer data. Explicit `[full-model-validation]` / `[full-svg-validation]` validation commits exercise the full production transfer path.

## Sources

The collector currently retrieves:

- DWD ICON-D2
- DWD ICON-EU
- DWD ICON-D2-EPS through the named Open-Meteo extraction endpoint, with Open-Meteo run metadata checked before/after, >=10-minute settling, DWD cycle confirmation and exact 20-member identity
- ECMWF IFS Open Data
- NOAA/NCEP GFS
- NOAA/NCEP GEFS control
- WeatherLink v2 station 42374 (SVG)
- MeteoMap station 898 (SKM), optional legacy diagnostic only; its failure never gates SVG or model evaluation
- DWD station 05715 Wunstorf, historical land reference
- ETNW METAR from AviationWeather.gov, current Wunstorf redundancy

It performs only source extraction and basic unit/metadata normalization needed
to preserve an unambiguous transfer bundle. Forecast weighting, traffic-light
logic, calibration and verification remain private.

## Required GitHub Actions secrets

Production transfer remains disabled until these repository secrets are set:

- `WEATHERLINK_API_KEY`
- `WEATHERLINK_API_SECRET`
- `PRIVATE_REPO_TOKEN`

`PRIVATE_REPO_TOKEN` should be a fine-grained token restricted to
`janwohlers78/mardorf-kitevorhersage` with **Contents: Read and write** only.
It should not have administration, Actions, secrets, issues or organization
permissions.

## Data handling

- No collected weather/model payload is committed to this public repository.
- No collected payload is uploaded as a public Actions artifact.
- Generated files live only in the ephemeral runner `work/` directory.
- Transfer bundles are gzip-compressed and written directly to the private
  repository below `data/inbox/public_collector/`.
- Every publication uses `private-transfer-readback-v2`: the unpublished private commit tree and blobs are read back byte-for-byte before `main` is moved. Gzip payloads are decompressed and checked against the SHA-256 recorded by the audit before transfer. Mutable latest pointers are monotonic by source generation time, and `latest_success` is published only in the verified receipt-bearing commit.
- Wunstorf and ETNW child receipts remain independently auditable, but private canonical promotion is triggered only by `data/inbox/public_collector/transfer_receipts/secondary/latest.json`. The batch finalizer verifies both current child receipts against the same collector invocation before publishing this transaction boundary.
- Runtime Python wheels are version- and SHA-256-pinned in `requirements-runtime.txt`; GitHub-maintained actions are pinned to full commit SHAs.
- Scheduled workflows run from the default branch.
- Pull requests from forks do not receive repository secrets.
- The private repository remains the authoritative persistent store and performs
  integrity checking, forecasting, calibration and reporting.

## Safety boundary

A successful acquisition does **not** imply that data are operationally usable.
The private repository must independently verify timestamps, model identity,
family independence, completeness, freshness and observation semantics before
using any transferred bundle.


## Integrity reporting

Every acquisition is followed by `collector-integrity-v1.5`.

For production model bundles the v1.5 gate also requires the complete native-hourly ICON-D2-EPS source used by v15: one stable run, exact UTC hours through +48 h, and all 20 fixed wind/direction/gust members. This hourly block is retained from the same Open-Meteo response already used for the 3-hour EPS summary; it creates no second provider request.\n\nFor models the report identifies, per source, the selected run, exact run age,
age limit, expected/received lead hours, exact absent or duplicate leads,
required-field failures, timestamp inconsistencies and provider/decode exceptions.
Provider-cycle horizon limitations are distinguished from real download failures.
GRIB-backed sources additionally verify provider `dataDate/dataTime`, `stepRange` and
`validityDate/validityTime` before a record is accepted. The audit hard-fails on
model-record identity or collection-spot mismatches.

For SVG the report records each WeatherLink endpoint separately with HTTP status,
request duration, response size and exception details, plus current observation
age and exact five-minute archive-window coverage.

For optional SKM the report distinguishes HTTP/request failure from a valid
MeteoMap JSON response whose measurement series is empty. SKM status is persisted
but never participates in the primary SVG/model success gate.

The Markdown report is written to the GitHub Actions job summary. When private
transfer is configured, both JSON and Markdown reports are also persisted in the
private repository, including failed acquisition attempts.

### Why code pushes use a reduced model test

A code push still calls every one of the six model source paths, but only for a
representative lead set. This is not an Actions-minutes optimization: public
standard runners are free. It prevents a sequence of ordinary code commits from
repeatedly downloading the same full 120-hour provider datasets and unnecessarily
loading DWD, ECMWF and NOAA services. Scheduled due runs remain full acquisitions and therefore continuously exercise
the complete provider horizon that is actually published for the selected model
cycle.
