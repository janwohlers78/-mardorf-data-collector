#!/usr/bin/env python3
"""Product-aware NOAA GFS/GEFS weather-context request and metadata helpers.

All additional fields are optional to the wind-critical contract. This module
adds only product-supported variable/level flags and preserves every returned
native GRIB message distinctly.
"""
from __future__ import annotations

import json
import re
import subprocess

META_KEYS=(
    "shortName","name","paramId","typeOfLevel","level","stepType","stepRange",
    "units","dataDate","dataTime","validityDate","validityTime","totalLength",
    "gridType","Ni","Nj",
)

PRODUCTS={
    "gfs_0p25":{
        "variables":("TMP","DPT","RH","PRMSL","PRES","TCDC","CAPE","CIN","DSWRF"),
        "levels":("2_m_above_ground","mean_sea_level","surface","entire_atmosphere",
                  "180-0_mb_above_ground","90-0_mb_above_ground","255-0_mb_above_ground"),
    },
    "gefs_0p25s":{
        "variables":("TMP","DPT","RH","PRMSL","PRES","TCDC","CAPE","CIN","DSWRF"),
        "levels":("2_m_above_ground","mean_sea_level","surface","entire_atmosphere",
                  "180-0_mb_above_ground"),
    },
    "gefs_0p50a":{
        "variables":("TMP","RH","PRMSL","PRES","TCDC","CAPE","CIN","DSWRF"),
        "levels":("2_m_above_ground","mean_sea_level","surface","entire_atmosphere",
                  "180-0_mb_above_ground"),
    },
    "gefs_0p50b":{
        "variables":("DPT","CAPE","CIN"),
        "levels":("2_m_above_ground","surface","255-0_mb_above_ground"),
    },
}

def add_weather_flags(query,product):
    spec=PRODUCTS[product]
    for variable in spec["variables"]:
        query["var_"+variable]="on"
    for level in spec["levels"]:
        query["lev_"+level]="on"
    return query

def expected_weather_variables(product):
    return tuple(PRODUCTS[product]["variables"])

def _metadata(path):
    p=subprocess.run(
        ["grib_ls","-j","-p",",".join(META_KEYS),str(path)],
        capture_output=True,text=True,check=True,
    )
    parsed=json.loads(p.stdout)
    rows=parsed.get("messages",[]) if isinstance(parsed,dict) else parsed
    if not isinstance(rows,list):
        raise RuntimeError("unexpected grib_ls JSON metadata")
    return rows

def _nearest(path,lat,lon):
    p=subprocess.run(
        ["grib_ls","-l",f"{lat},{lon},1","-p","shortName,stepRange",str(path)],
        capture_output=True,text=True,check=True,
    )
    m=re.search(
        r"Grid Point chosen .*?latitude=([+-]?\d+(?:\.\d+)?) longitude=([+-]?\d+(?:\.\d+)?)",
        p.stdout,
    )
    if not m:
        raise RuntimeError(f"cannot identify NOAA selected grid point: {p.stdout[:700]}")
    point={
        "latitude":float(m.group(1)),"longitude":float(m.group(2)),
        "selection":"ecCodes_nearest_grid_point",
    }
    rows=[]
    for line in p.stdout.splitlines():
        s=line.strip()
        if (not s or s.startswith(("edition","shortName")) or "messages in" in s
                or "total messages" in s or "Input Point:" in s or "Grid Point" in s
                or s.startswith(("Other grid","- "))):
            continue
        parts=s.split()
        if len(parts)>=3:
            try:
                rows.append({"shortName":parts[0],"stepRange":parts[1],"value":float(parts[-1])})
            except ValueError:
                pass
    if not rows:
        raise RuntimeError("no NOAA nearest-grid values parsed")
    return rows,point

def extract_native_values(path,lat,lon,source_sha256=None,product=None):
    meta=_metadata(path)
    nearest,point=_nearest(path,lat,lon)
    if len(meta)!=len(nearest):
        raise RuntimeError(f"NOAA metadata/value count mismatch metadata={len(meta)} nearest={len(nearest)}")
    values={}
    for m,p in zip(meta,nearest):
        if str(m.get("shortName"))!=str(p.get("shortName")):
            raise RuntimeError(f"NOAA metadata/value shortName mismatch {m.get('shortName')} != {p.get('shortName')}")
        item={
            **m,
            "value":p["value"],
            "latitude":point["latitude"],
            "longitude":point["longitude"],
            "availability_status":"received",
            "availability_evidence_type":"nomads_filtered_grib_exact_run_valid",
        }
        if source_sha256 is not None:
            item["source_sha256"]=source_sha256
        if product is not None:
            item["provider_product"]=product
        values.setdefault(str(m["shortName"]),[]).append(item)
    return values,point

def weather_availability(product,values):
    requested=set(expected_weather_variables(product))
    present={}
    for name,items in (values or {}).items():
        if not isinstance(items,list):
            continue
        for item in items:
            if not isinstance(item,dict):
                continue
            # Native short names returned by ecCodes differ from NOMADS filter names.
            native=str(item.get("shortName",name)).lower()
            label=str(item.get("name","")).lower()
            for variable in requested:
                v=variable.lower()
                matched=(
                    (variable=="TMP" and ("temperature" in label and "dew" not in label) and str(item.get("typeOfLevel"))=="heightAboveGround" and str(item.get("level"))=="2")
                    or (variable=="DPT" and ("dew point" in label or native in ("2d","dpt")))
                    or (variable=="RH" and ("relative humidity" in label or native=="2r"))
                    or (variable=="PRMSL" and (native in ("prmsl","msl") or "pressure reduced to msl" in label))
                    or (variable=="PRES" and (native=="sp" or label=="surface pressure"))
                    or (variable=="TCDC" and (native=="tcc" or "total cloud cover" in label))
                    or (variable=="CAPE" and (native=="cape" or "convective available potential energy" in label))
                    or (variable=="CIN" and (native=="cin" or "convective inhibition" in label))
                    or (variable=="DSWRF" and (native in ("dswrf","sdswrf") or "downward short-wave radiation flux" in label or "downward shortwave radiation flux" in label))
                )
                if matched:
                    present[variable]=True
    return {variable:bool(present.get(variable)) for variable in requested}
