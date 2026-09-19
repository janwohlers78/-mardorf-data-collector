# Mardorf Data Collector

Public acquisition-only companion for the private `mardorf-kitevorhersage` project.

This repository intentionally contains **no forecast decision logic, calibration,
traffic-light thresholds, historical private datasets, or user-facing reports**.
It only retrieves source observations/model data and transfers versioned bundles
to the private repository.

## Schedules

- Model collection: every 3 hours at minute 35 UTC.
- SVG WeatherLink collection: hourly at minute 17 UTC.
- Code changes run a reduced model smoke test only; smoke tests never transfer data.

## Sources

The collector currently retrieves:

- DWD ICON-D2
- DWD ICON-EU
- DWD ICON-D2-EPS through the named Open-Meteo extraction endpoint
- ECMWF IFS Open Data
- NOAA/NCEP GFS
- NOAA/NCEP GEFS control
- WeatherLink v2 station 42374 (SVG)

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

Every acquisition is followed by `collector-integrity-v1`.

For models the report identifies, per source, the selected run, exact run age,
age limit, expected/received lead hours, exact absent or duplicate leads,
required-field failures, timestamp inconsistencies and provider/decode exceptions.
Provider-cycle horizon limitations are distinguished from real download failures.

For SVG the report records each WeatherLink endpoint separately with HTTP status,
request duration, response size and exception details, plus current observation
age and exact five-minute archive-window coverage.

The Markdown report is written to the GitHub Actions job summary. When private
transfer is configured, both JSON and Markdown reports are also persisted in the
private repository, including failed acquisition attempts.

### Why code pushes use a reduced model test

A code push still calls every one of the six model source paths, but only for a
representative lead set. This is not an Actions-minutes optimization: public
standard runners are free. It prevents a sequence of ordinary code commits from
repeatedly downloading the same full 120-hour provider datasets and unnecessarily
loading DWD, ECMWF and NOAA services. The scheduled three-hour production runs
remain full acquisitions and therefore continuously exercise the complete
operational horizon.
