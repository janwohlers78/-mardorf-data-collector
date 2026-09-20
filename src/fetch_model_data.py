#!/usr/bin/env python3
# Operational fetcher: official DWD ICON-D2 + NOAA/NCEP GFS point data for Mardorf.
import argparse, bz2, json, math, re, subprocess, sys, tempfile, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
import requests
from grib_identity import assert_grib_valid_time

LAT=52.4942
LON=9.3418
UA='mardorf-data-collector/1.0 (+github-actions)'
S=requests.Session(); S.headers.update({'User-Agent':UA})


def get(url, timeout=60):
    r=S.get(url, timeout=timeout)
    r.raise_for_status()
    return r


def latest_dwd_icon_d2_cycle(required_lead=0):
    """Return newest ICON-D2 cycle with all wind-critical fields at required lead."""
    critical=('u_10m','v_10m','vmax_10m');coverage={};diagnostics=[]
    for hh in ['00','03','06','09','12','15','18','21']:
        for param in critical:
            url=f'https://opendata.dwd.de/weather/nwp/icon-d2/grib/{hh}/{param}/'
            try:
                txt=get(url).text
                pattern=rf'icon-d2_germany_regular-lat-lon_single-level_(\d{{10}})_(\d{{3}})_2d_{re.escape(param)}\.grib2\.bz2'
                for cycle,lead in re.findall(pattern,txt):
                    coverage.setdefault(cycle,{}).setdefault(param,set()).add(int(lead))
            except Exception as e:
                diagnostics.append((hh,param,f'ERR:{type(e).__name__}'))
    eligible=[
        cycle for cycle,fields in coverage.items()
        if all(required_lead in fields.get(param,set()) for param in critical)
    ]
    if not eligible:
        summary={cycle:{p:max(v) if v else -1 for p,v in fields.items()} for cycle,fields in sorted(coverage.items())[-20:]}
        raise RuntimeError(f'No ICON-D2 cycle with all critical fields at lead {required_lead}; coverage={summary}; diagnostics={diagnostics[-20:]}')
    return max(eligible)


def dwd_url(cycle, lead, param):
    hh=cycle[-2:]
    return f'https://opendata.dwd.de/weather/nwp/icon-d2/grib/{hh}/{param}/icon-d2_germany_regular-lat-lon_single-level_{cycle}_{lead:03d}_2d_{param}.grib2.bz2'


def grib_nearest(path):
    cmd=['grib_ls','-l',f'{LAT},{LON},1','-p','shortName,stepRange',str(path)]
    p=subprocess.run(cmd,capture_output=True,text=True,check=True)
    chosen_lat=chosen_lon=None
    m=re.search(r'Grid Point chosen .*?latitude=([+-]?\d+(?:\.\d+)?) longitude=([+-]?\d+(?:\.\d+)?)',p.stdout)
    if m:
        chosen_lat=float(m.group(1)); chosen_lon=float(m.group(2))
    rows=[]
    for line in p.stdout.splitlines():
        s=line.strip()
        if not s or s.startswith(('edition','shortName')) or 'messages in' in s or 'total messages' in s or 'Input Point:' in s or 'Grid Point' in s or s.startswith(('Other grid','- ')):
            continue
        parts=s.split()
        if len(parts)>=3:
            try:
                value=float(parts[-1])
                rows.append({'shortName':parts[0],'stepRange':parts[1],'lat':chosen_lat,'lon':chosen_lon,'value':value})
            except ValueError:
                pass
    if not rows:
        raise RuntimeError(f'No nearest-point value parsed from {path}: {p.stdout[:700]}')
    return rows


def fetch_icon(leads):
    cycle=latest_dwd_icon_d2_cycle(max(leads) if leads else 0); out=[]
    params=['u_10m','v_10m','vmax_10m','tot_prec']
    with tempfile.TemporaryDirectory() as td:
        td=Path(td)
        for lead in leads:
            base=datetime.strptime(cycle,'%Y%m%d%H').replace(tzinfo=timezone.utc)
            rec={'model':'ICON-D2','run_time_utc':base.isoformat(),'forecast_lead_hours':lead,'valid_time_utc':(base+timedelta(hours=lead)).isoformat(),'source':'DWD Open Data','values':{},'source_urls':[]}
            for param in params:
                url=dwd_url(cycle,lead,param); rec['source_urls'].append(url)
                try:
                    grib=td/f'{param}_{lead}.grib2'
                    grib.write_bytes(bz2.decompress(get(url,90).content))
                    assert_grib_valid_time(grib,base,base+timedelta(hours=lead),f'ICON-D2 {param} lead {lead}')
                    rows=grib_nearest(grib)
                    rec['values'][param]=rows[0]
                    if len(rows)>1:
                        rec['values'][param+'_all_messages']=rows
                except Exception as e:
                    rec['values'][param]={'error_type':type(e).__name__,'error_message':str(e),'source_url':url}
            out.append(rec)
    return out


def gfs_url(cycle,lead,probe=False):
    ymd,hh=cycle[:8],cycle[8:]
    q={'file':f'gfs.t{hh}z.pgrb2.0p25.f{lead:03d}','lev_10_m_above_ground':'on','var_UGRD':'on','var_VGRD':'on','subregion':'','leftlon':f'{LON-0.3:.3f}','rightlon':f'{LON+0.3:.3f}','toplat':f'{LAT+0.3:.3f}','bottomlat':f'{LAT-0.3:.3f}','dir':f'/gfs.{ymd}/{hh}/atmos'}
    q.update({'lev_surface':'on','var_GUST':'on'})
    if not probe:
        q.update({'var_APCP':'on'})
    return 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?'+urlencode(q)


def discover_gfs_cycle(required_lead=0):
    now=datetime.now(timezone.utc)
    attempts=[]
    for d in [now.date(), (now-timedelta(days=1)).date(), (now-timedelta(days=2)).date()]:
        for hh in ['18','12','06','00']:
            cycle=f'{d:%Y%m%d}{hh}'
            # Probe the farthest required lead, not only f000, so incomplete cycles
            # are never selected for a 48 h production run.
            url=gfs_url(cycle,required_lead,probe=True)
            try:
                r=S.get(url,timeout=30)
                attempts.append((cycle,required_lead,r.status_code,len(r.content),r.content[:4]))
                if r.status_code==200 and r.content[:4]==b'GRIB':
                    return cycle
            except Exception as e:
                attempts.append((cycle,required_lead,'EXC',0,str(e)[:80]))
    raise RuntimeError(f'No GFS cycle with lead {required_lead} discovered; attempts={attempts}')


def fetch_gfs(leads):
    cycle=discover_gfs_cycle(max(leads) if leads else 0); out=[]
    with tempfile.TemporaryDirectory() as td:
        td=Path(td)
        for lead in leads:
            base=datetime.strptime(cycle,'%Y%m%d%H').replace(tzinfo=timezone.utc)
            url=gfs_url(cycle,lead)
            rec={'model':'GFS','run_time_utc':base.isoformat(),'forecast_lead_hours':lead,'valid_time_utc':(base+timedelta(hours=lead)).isoformat(),'source':'NOAA/NCEP NOMADS','source_urls':[url],'values':{}}
            try:
                p=td/f'gfs_{lead}.grib2'; raw=get(url,90).content
                if raw[:4] != b'GRIB': raise RuntimeError(f'NOMADS response is not GRIB, bytes={len(raw)}, head={raw[:100]!r}')
                p.write_bytes(raw)
                assert_grib_valid_time(p,base,base+timedelta(hours=lead),f'GFS lead {lead}')
                for row in grib_nearest(p):
                    rec['values'].setdefault(row['shortName'],[]).append(row)
            except Exception as e:
                rec['error_type']=type(e).__name__; rec['error_message']=str(e)
            out.append(rec)
    return out


def derive(records):
    for r in records:
        vals=r.get('values',{})
        try:
            if r['model']=='ICON-D2':
                u=vals['u_10m']['value']; v=vals['v_10m']['value']; gust=vals['vmax_10m']['value']
            else:
                def first(names):
                    for n in names:
                        if n in vals and vals[n]: return vals[n][0]['value']
                    raise KeyError(names)
                u=first(['10u','u']); v=first(['10v','v']); gust=first(['gust','10fg'])
            sp=math.hypot(u,v); direction=(270-math.degrees(math.atan2(v,u)))%360
            r['derived']={'wind_speed_ms':round(sp,3),'wind_speed_kt':round(sp*1.943844,2),'wind_direction_deg':round(direction,1),'gust_ms':round(gust,3),'gust_kt':round(gust*1.943844,2),'gust_factor':round(gust/sp,2) if sp>0.2 else None}
        except Exception as e:
            r['derive_error_type']=type(e).__name__; r['derive_error_message']=str(e)
    return records


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--test',action='store_true'); args=ap.parse_args()
    leads=[0,12,24,30,36,42,48] if args.test else list(range(0,49,3))
    started=datetime.now(timezone.utc)
    result={'schema_version':1,'retrieved_at_utc':started.isoformat(),'spot':{'lat':LAT,'lon':LON},'mode':'test' if args.test else 'production','leads_hours':leads,'models':{},'quality':{}}
    errors=[]
    for name,fn in [('ICON-D2',fetch_icon),('GFS',fetch_gfs)]:
        try:
            result['models'][name]=derive(fn(leads))
        except Exception as e:
            result['models'][name]=[]; errors.append(f'{name}: {type(e).__name__}: {e}')
    successful=[]
    for name,recs in result['models'].items():
        good=sum(1 for x in recs if 'derived' in x)
        # Compatibility means complete requested lead coverage, not merely >=2 records.
        complete=(len(recs)==len(leads) and good==len(leads))
        result['quality'][name]={'records':len(recs),'derived_records':good,'success':complete,'complete_requested_horizon':complete}
        if complete: successful.append(name)
    result['quality']['minimum_two_independent_models_met']=len(successful)>=2
    result['quality']['successful_models']=successful
    result['quality']['errors']=errors
    out=Path(os.getenv('COLLECTOR_MODEL_FILE','work/model_snapshot.json'))
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,separators=(',',':'))+'\n',encoding='utf-8')
    print(json.dumps({'quality':result['quality'],'output_bytes':out.stat().st_size},indent=2))
    if not result['quality']['minimum_two_independent_models_met']:
        sys.exit(2)

if __name__=='__main__': main()
