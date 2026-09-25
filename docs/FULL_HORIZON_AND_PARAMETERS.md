# Acquisition contract: complete native horizon, compatible core

2026-09-25. The public repository acquires provider data and transfers verified
payloads. Canonical Parquet storage, DuckDB queries and all analysis belong to
the private repository. Do not commit generated weather data here.

## Live handoff to the private weather archive

The collector remains intentionally storage-format agnostic. It transfers the
provider-native `models` records plus `full_horizon_archive` under the verified
immutable receipt. The private promotion path is the transaction boundary and
persists any payload carrying `full_horizon_archive` through
`weather_archive.persist_archive()`. Since private PR #126 this writes
`mardorf-weather-archive-v2`: provider fields are long-format `weather_values`
rows, so new parameters do not require Parquet schema changes. Existing
`canonical-model-record-v1` consumers remain unchanged.

This separation is deliberate: acquisition/provider semantics live here;
Parquet/DuckDB persistence, normalization and analysis live only in
`janwohlers78/mardorf-kitevorhersage`.

## Native model horizons

`src/collect_full_horizon.py` runs after the existing core extension. It keeps
`models`, `quality`, `retrieved_at_utc` and ensemble source fields unchanged and
adds `full_horizon_archive`. Target endpoints:

| Model | Cycle-specific maximum |
|---|---|
| ICON-D2 / ICON-D2-EPS | 48 h |
| ICON-EU | 120 h main cycles, 51 h intervening cycles |
| ECMWF-IFS | 360 h at 00/12 UTC, 144 h at 06/18 UTC |
| GFS | 384 h |
| GEFS-control | 840 h at 00 UTC; 384 h otherwise |

Sampling remains 3-hourly through 72 h, then 6-hourly. The full time extent is
requested; not every finer provider time step. GEFS beyond 240 h needs the 0.5°
a product for wind/precipitation plus b for gust, with actual grid identity.
The wind/precipitation product is authoritative for far-horizon coverage; a
lagging optional gust product is recorded explicitly and never converted into
zero. IFS 06/18 beyond the existing 90-hour compatibility range is requested up
to the current overview catalogue's 144-hour limit; unpublished chunks remain
explicit missing data, not fabricated coverage.

Successful chunks are saved atomically. Four workers and a 12-minute workflow
timeout bound the extra acquisition. The existing <=120 h analysis-compatible
core can still transfer if the archive sidecar is partial; corrupt archive
identities fail the integrity audit. Coverage is recomputed independently in
the private repository. Every field's native interval/product metadata remains
part of the retained record.

## Additional weather parameters

The machine-readable `config/weather_acquisition_plan.json` is the provider
adapter registry. The archive contract itself is already open to arbitrary
fields: every scalar in `values` or `derived` is persisted without a provider
allow-list. Current connected records already retain wind components/gusts and,
where supplied by the provider adapter, precipitation and convection fields.
The remaining adapter expansion must preserve optional-field semantics: missing
non-wind parameters are explicit missing data and may not invalidate otherwise
usable wind coverage.

The expansion order is temperature, dewpoint, humidity, pressure,
precipitation, clouds, radiation and convection indicators. Index matching must
include level, time statistic and member, not just shortName. CAPE variants
remain different quantities.

Bundle requests by product/run/lead, download only relevant fields and spatial
subsets where the provider supports this. For global ECMWF fields measure real
download bytes rather than assuming point extraction implies a small download.
Record GRIB unit/paramId/typeOfLevel/level/stepType/startStep/endStep/stepUnits,
model version, product/grid identity, member identity, retrieval/first-seen time
and response hash. Unit normalization and statistical calibration are separate.

New IFS/GEFS ensembles, GEPS and AIFS adapters are planned separately; this
contract extends already connected model identities, including GEFS control
only.

Primary source contracts:

* https://www.ecmwf.int/en/forecasts/datasets/open-data
* https://www.nco.ncep.noaa.gov/pmb/products/gens/
* https://opendata.dwd.de/weather/nwp/icon-eu/grib/00/
* https://opendata.dwd.de/weather/nwp/icon-d2/grib/00/

The complete storage/normalization/cost/compatibility design is maintained in
the private repository's `docs/weather-acquisition-storage-v1.md`.
