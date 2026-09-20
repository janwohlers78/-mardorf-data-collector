#!/usr/bin/env python3
"""Strict GRIB model-reference-time identity checks."""
from datetime import datetime,timezone
import subprocess

def grib_run_times(path):
    p=subprocess.run(
        ["grib_get","-p","dataDate,dataTime",str(path)],
        capture_output=True,text=True,check=True)
    out=set()
    for line in p.stdout.splitlines():
        parts=line.strip().split()
        if len(parts)>=2 and parts[0].isdigit() and parts[1].isdigit():
            out.add(datetime.strptime(parts[0]+parts[1].zfill(4),"%Y%m%d%H%M").replace(tzinfo=timezone.utc))
    if not out:
        raise RuntimeError(f"no parseable GRIB dataDate/dataTime in {path}: {p.stdout[:500]!r}")
    return sorted(out)

def assert_grib_run_time(path,expected,context):
    expected=expected.astimezone(timezone.utc)
    observed=grib_run_times(path)
    if observed != [expected]:
        raise RuntimeError(
            f"{context}: GRIB run identity mismatch expected={expected.isoformat()} "
            f"observed={[x.isoformat() for x in observed]}")
    return observed[0]
