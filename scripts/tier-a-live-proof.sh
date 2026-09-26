#!/usr/bin/env bash
set -euo pipefail

export COLLECTOR_MODEL_FILE=work/model_snapshot.json
export FULL_VALIDATION=true

run_optional() {
  local model="$1"
  local stage="$2"
  local limit="$3"
  set +e
  timeout --signal=TERM --kill-after=15s "$limit" python src/provider_fetch.py --model "$model" --stage "$stage"
  local rc=$?
  set -e
  echo "provider model=$model stage=$stage rc=$rc"
}

for model in ICON-D2 GFS GEFS-control ICON-EU ICON-D2-EPS; do
  run_optional "$model" base 6m
done

ecmwf_stage() {
  local stage="$1"
  local limit="$2"
  local last=1
  for source in azure google ecmwf; do
    set +e
    ECMWF_OPEN_DATA_SOURCE="$source" timeout --signal=TERM --kill-after=15s "$limit" python src/provider_fetch.py --model ECMWF-IFS --stage "$stage"
    local rc=$?
    set -e
    echo "ECMWF stage=$stage source=$source rc=$rc"
    if [ "$rc" -eq 0 ]; then
      return 0
    fi
    last=$rc
  done
  return "$last"
}
ecmwf_stage base 7m

for model in GFS GEFS-control ICON-EU; do
  run_optional "$model" extension 8m
done
ecmwf_stage extension 7m

timeout --signal=TERM --kill-after=15s 12m python src/collect_icon_tier_a.py --workers 4
timeout --signal=TERM --kill-after=15s 15m python src/collect_full_horizon.py

python src/audit_integrity.py --kind models --input work/model_snapshot.json --json-out work/model_integrity.json --md-out work/model_integrity.md
cat work/model_integrity.md >> "$GITHUB_STEP_SUMMARY"

python - <<'PY'
import json, os
d=json.load(open("work/model_integrity.json"))
a=json.load(open("work/model_snapshot.json")).get("icon_tier_a_acquisition") or {}
print("TIER_A_SUMMARY="+json.dumps({k:v for k,v in a.items() if k!="diagnostics"},sort_keys=True))
with open(os.environ["GITHUB_STEP_SUMMARY"],"a") as f:
    f.write("\n## Tier-A acquisition\n\n")
    f.write(json.dumps({k:v for k,v in a.items() if k!="diagnostics"},indent=2)+"\n")
if d["error_count"]:
    raise SystemExit(1)
PY

python src/push_private.py --kind models --file work/model_snapshot.json --integrity-json work/model_integrity.json --integrity-md work/model_integrity.md
