#!/usr/bin/env python3
import hashlib, json, shutil, sys
from pathlib import Path

ROOT=Path("work/archive_v3_probe")
PAYLOAD_PATH=Path("work/canary/model_snapshot.json")
sys.path.insert(0,str(Path("storage_probe").resolve()))
import weather_archive as wa

raw=PAYLOAD_PATH.read_bytes()
payload=json.loads(raw)
if ROOT.exists(): shutil.rmtree(ROOT)
ROOT.mkdir(parents=True)

def provenance(tag):
    return {
        "payload_sha256":hashlib.sha256(tag+raw).hexdigest(),
        "payload_path":f"canary/{tag.decode(errors='ignore') or 'first'}/model_snapshot.json",
        "receipt_path":f"canary/{tag.decode(errors='ignore') or 'first'}/receipt.json",
        "collector_generated_at_utc":payload["retrieved_at_utc"],
    }

p1=provenance(b"")
m1=wa.persist_archive(ROOT,payload,p1)
if not m1.get("manifest_persisted"):
    raise SystemExit("first Archive-v3 persistence unexpectedly produced no manifest")
files={x["dataset"]:x for x in m1["files"]}
if set(files)!={"forecast_records","weather_values"}:
    raise SystemExit(f"unexpected archive file set: {files}")

con=wa.connect_archive(ROOT)
physical={}
for model in ("GFS","GEFS-control"):
    physical[model]={
        "forecast_records":con.sql(
            f"SELECT count(*), max(lead_hours) FROM forecast_records WHERE model='{model}'"
        ).fetchone(),
        "weather_values":con.sql(
            f"SELECT count(*), max(lead_hours) FROM weather_values WHERE model='{model}'"
        ).fetchone(),
        "parameters":con.sql(
            f"SELECT parameter_native,count(*) FROM weather_values WHERE model='{model}' GROUP BY 1 ORDER BY 1"
        ).fetchall(),
    }

gfs_tcc=con.sql("""
SELECT step_type_native,step_range_native,unit_native
FROM weather_values
WHERE model='GFS' AND lead_hours=120 AND parameter_native='tcc'
ORDER BY step_type_native,step_range_native
""").fetchall()
gfs_cape=con.sql("""
SELECT type_of_level_native,level_native,step_type_native,unit_native
FROM weather_values
WHERE model='GFS' AND lead_hours=120 AND parameter_native='cape'
ORDER BY type_of_level_native,level_native
""").fetchall()
gefs_tcc=con.sql("""
SELECT step_type_native,step_range_native,unit_native
FROM weather_values
WHERE model='GEFS-control' AND lead_hours=120 AND parameter_native='tcc'
ORDER BY step_type_native,step_range_native
""").fetchall()
gefs_cape=con.sql("""
SELECT type_of_level_native,level_native,step_type_native,unit_native
FROM weather_values
WHERE model='GEFS-control' AND lead_hours=120 AND parameter_native='cape'
ORDER BY type_of_level_native,level_native
""").fetchall()
con.close()

if {r[0] for r in gfs_tcc}!={"instant","avg"}:
    raise SystemExit(f"GFS TCDC variants collapsed: {gfs_tcc}")
if not {"0","18000","9000","25500"} <= {r[1] for r in gfs_cape}:
    raise SystemExit(f"GFS CAPE layers lost: {gfs_cape}")
if {r[0] for r in gefs_tcc}!={"avg"}:
    raise SystemExit(f"GEFS TCDC semantics changed: {gefs_tcc}")
if not {"0","18000"} <= {r[1] for r in gefs_cape}:
    raise SystemExit(f"GEFS CAPE layers lost: {gefs_cape}")

# Real retention policy: this live proof selected a 06Z NOAA cycle. Archive-v3
# deliberately retains >120 h only for 00/12Z, so far-horizon rows must be
# filtered rather than silently persisted against policy.
for model in ("GFS","GEFS-control"):
    if physical[model]["forecast_records"][1] > 120:
        raise SystemExit(f"{model}: 06Z far-horizon rows bypassed Archive-v3 retention")

# Separately prove that the exact writer's row/schema machinery can round-trip
# every acquired far-horizon native message without losing metadata. We bypass
# only the retention decision in memory; the production persistence test above
# remains unmodified and authoritative for what is actually retained.
original_retention=wa.retention_decision
wa.retention_decision=lambda model,row,terminal=None: {
    "retain":True,"cycle_retained":True,"lead_retained":True,
    "policy_version":wa.DELTA_POLICY_VERSION,
}
try:
    records,values,coverage,stats=wa.make_tables(payload,p1,set())
finally:
    wa.retention_decision=original_retention

import pyarrow as pa
import pyarrow.parquet as pq
all_path=ROOT/"all_acquired_weather_values.parquet"
table=pa.Table.from_pylist(values,schema=wa.schemas()["weather_values"])
pq.write_table(table,all_path,compression="zstd",use_dictionary=True)
roundtrip=pq.read_table(all_path)
if not roundtrip.equals(table):
    raise SystemExit("all-acquired far-horizon Parquet roundtrip mismatch")

import duckdb
dc=duckdb.connect(":memory:")
dc.register("v",roundtrip)
far_checks={
    "gfs_384_tcc":dc.sql("SELECT step_type_native,step_range_native,unit_native FROM v WHERE model='GFS' AND lead_hours=384 AND parameter_native='tcc' ORDER BY 1,2").fetchall(),
    "gfs_384_cape":dc.sql("SELECT type_of_level_native,level_native,step_type_native,unit_native FROM v WHERE model='GFS' AND lead_hours=384 AND parameter_native='cape' ORDER BY 1,2").fetchall(),
    "gefs_246_tcc":dc.sql("SELECT step_type_native,step_range_native,unit_native FROM v WHERE model='GEFS-control' AND lead_hours=246 AND parameter_native='tcc'").fetchall(),
    "gefs_246_cape":dc.sql("SELECT type_of_level_native,level_native,step_type_native,unit_native FROM v WHERE model='GEFS-control' AND lead_hours=246 AND parameter_native='cape'").fetchall(),
    "gefs_246_dpt":dc.sql("SELECT count(*) FROM v WHERE model='GEFS-control' AND lead_hours=246 AND parameter_native='2d'").fetchone()[0],
}
dc.close()
if {x[0] for x in far_checks["gfs_384_tcc"]}!={"instant","avg"}:
    raise SystemExit(f"GFS f384 TCDC lost: {far_checks['gfs_384_tcc']}")
if not {"0","18000","9000","25500"} <= {x[1] for x in far_checks["gfs_384_cape"]}:
    raise SystemExit(f"GFS f384 CAPE layers lost: {far_checks['gfs_384_cape']}")
if far_checks["gefs_246_tcc"]!=[("avg","240-246","%")]:
    raise SystemExit(f"GEFS f246 TCDC mismatch: {far_checks['gefs_246_tcc']}")
if far_checks["gefs_246_cape"]!=[("pressureFromGroundLayer","18000","instant","J kg**-1")]:
    raise SystemExit(f"GEFS f246 CAPE mismatch: {far_checks['gefs_246_cape']}")
# pgrb2b was optional/unpublished in the canary; DPT must therefore be absent,
# not zero-filled or fabricated.
if far_checks["gefs_246_dpt"]!=0:
    raise SystemExit("GEFS f246 DPT fabricated despite unavailable pgrb2b")

p2=provenance(b"second-transfer:")
m2=wa.persist_archive(ROOT,payload,p2)
if m2.get("manifest_persisted") or m2.get("files"):
    raise SystemExit(f"identical second transfer created physical archive files: {m2}")
if m2["delta"]["new_revision_records"]!=0:
    raise SystemExit(f"identical second transfer created revisions: {m2['delta']}")

summary={
    "method":"phase2c-noaa-archive-v3-storage-proof-v1",
    "private_writer_source_blob":"fde15264d5b3783249cc4915c0db3e02068b6e46",
    "private_contract_source_blob":"aa217b42e4b91cc3f5e09ff89aedff7df088916e",
    "source_live_run_id":36243288938,
    "source_payload_bytes":len(raw),
    "source_payload_sha256":hashlib.sha256(raw).hexdigest(),
    "first_persistence":{
        "delta":m1["delta"],
        "files":m1["files"],
        "coverage_status":m1["coverage"]["status"],
        "horizon_status":m1["coverage"]["horizon_status"],
    },
    "physical_retained_06z":physical,
    "metadata_checks":{
        "gfs_120_tcc":gfs_tcc,
        "gfs_120_cape":gfs_cape,
        "gefs_120_tcc":gefs_tcc,
        "gefs_120_cape":gefs_cape,
    },
    "all_acquired_roundtrip":{
        "forecast_records":len(records),
        "weather_values":len(values),
        "parquet_bytes":all_path.stat().st_size,
        "far_checks":far_checks,
    },
    "idempotence_second_transfer":m2["delta"],
    "result":"PASS",
}
out=Path("work/noaa_archive_v3_storage_proof.json")
out.write_text(json.dumps(summary,indent=2,default=list)+"\n")
print(json.dumps(summary,indent=2,default=list))
