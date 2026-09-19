# Mardorf Data Collector

Public acquisition-only companion for the private `mardorf-kitevorhersage` project.

This repository intentionally contains **no forecast decision logic, calibration,
traffic-light thresholds, historical private datasets, or user-facing reports**.
It only retrieves source observations/model data and transfers versioned bundles
to the private repository.

## Data handling

- No collected weather/model payload is committed to this public repository.
- No collected payload is uploaded as a public Actions artifact.
- WeatherLink credentials and the private-repository write credential must be
  configured as GitHub Actions secrets.
- Scheduled workflows run only from the default branch.
- Pull requests from forks do not receive repository secrets.

The private repository remains the authoritative store and performs all
validation, normalization, calibration, forecasting and reporting.
