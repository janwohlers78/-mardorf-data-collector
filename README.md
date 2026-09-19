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
