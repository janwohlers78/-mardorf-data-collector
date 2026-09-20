#!/usr/bin/env python3
"""Add ECMWF IFS Open Data and NOAA GEFS control raw point forecasts to latest snapshot."""
import argparse,json,math,re,subprocess,tempfile,os
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import urlencode
import requests
from ecmwf.opendata import Client
from grib_identity import _step_end_hours,assert_grib_batch_leads,assert_grib_valid_time,grib_run_times
LAT=52.4942; LON=9.3418
ECMWF_SOURCE=os.getenv('ECMWF_OPEN_DATA_SOURCE','azure')
ECMWF_PARAMS=['10u','10v','10fg','10fg3','tp','mucape']
S=requests.Session(); S.headers.update({'User-Agent':'mardorf-data-collector/1.0 (+github-actions)'})

class NearestRows(list):
 pass

def nearest(path):
 p=subprocess.run(['grib_ls','-l',f'{LAT},{LON},1','-p','shortName,stepRange',str(path)],capture_output=True,text=True,check=True)
 m=re.search(r'Grid Point chosen .*?latitude=([+-]?\d+(?:\.\d+)?) longitude=([+-]?\d+(?:\.\d+)?)',p.stdout)
 if not m:raise RuntimeError(f'Cannot identify ecCodes selected grid point for {path}: {p.stdout[:700]}')
 rows=NearestRows();rows.point={'latitude':float(m.group(1)),'longitude':float(m.group(2)),'selection':'ecCodes_nearest_grid_point'}
 for line in p.stdout.splitlines():
  x=line.strip().split()
  if len(x)>=3:
   try: rows.append((x[0],x[1],float(x[-1])))
   except: pass
 return rows

def derived(u,v,g=None):
 sp=math.hypot(u,v); d=(270-math.degrees(math.atan2(v,u)))%360
 z={'wind_speed_ms':round(sp,3),'wind_speed_kt':round(sp*1.943844,2),'wind_direction_deg':round(d,1)}
 if g is not None: z.update(gust_ms=round(g,3),gust_kt=round(g*1.943844,2),gust_factor=round(g/sp,2) if sp>.2 else None)
 return z

def grib_run_time(path):
    observed=grib_run_times(path)
    if len(observed)!=1:
        raise RuntimeError(
            f'ECMWF GRIB batch contains multiple model reference times: '
            f'{[x.isoformat() for x in observed]}')
    return observed[0]

def step_end(step_range):
 value=_step_end_hours(step_range)
 if value is None or abs(value-round(value))>1e-9:return None
 return int(round(value))

def fetch_ifs(leads):
 out=[]
 with tempfile.TemporaryDirectory() as td:
  target=Path(td)/'ifs_batch.grib2'
  client=Client(source=ECMWF_SOURCE,model='ifs',resol='0p25')
  result=client.retrieve(stream='oper',type='fc',step=leads,param=ECMWF_PARAMS,target=str(target))
  run=grib_run_time(target); assert_grib_batch_leads(target,run,leads,'ECMWF-IFS base batch'); bylead={int(x):{} for x in leads}
  rows=nearest(target);point=rows.point
  for n,s,v in rows:
   lead=step_end(s)
   if lead not in bylead: continue
   bylead[lead].setdefault(n,[]).append({'stepRange':s,'value':v})
  for lead in leads:
   vals=bylead[int(lead)]
   def one(*names):
    for n in names:
     if n in vals and vals[n]: return vals[n][0]['value']
    return None
   u=one('10u');v=one('10v');g=one('10fg','10fg3','10fg6')
   rec={'model':'ECMWF-IFS','run_time_utc':run.isoformat(),'forecast_lead_hours':lead,
        'valid_time_utc':(run+timedelta(hours=lead)).isoformat(),
        'source':f'ECMWF Open Data via {ECMWF_SOURCE} mirror raw GRIB2','values':vals,
        'forecast_coordinate_or_grid_point':point,
        'source_request':{'steps':leads,'retrieved_run_time_utc':run.isoformat()}}
   if u is not None and v is not None:rec['derived']=derived(u,v,g)
   out.append(rec)
 return out

def gefs_url(cycle,lead):
 ymd,hh=cycle[:8],cycle[8:]
 q={'file':f'gec00.t{hh}z.pgrb2s.0p25.f{lead:03d}','lev_10_m_above_ground':'on','lev_surface':'on','var_UGRD':'on','var_VGRD':'on','var_GUST':'on','var_APCP':'on','subregion':'','leftlon':f'{LON-.3:.3f}','rightlon':f'{LON+.3:.3f}','toplat':f'{LAT+.3:.3f}','bottomlat':f'{LAT-.3:.3f}','dir':f'/gefs.{ymd}/{hh}/atmos/pgrb2sp25'}
 return 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gefs_atmos_0p25s.pl?'+urlencode(q)

def discover_gefs(required_lead=0):
 now=datetime.now(timezone.utc); attempts=[]
 for dd in range(3):
  d=(now-timedelta(days=dd)).date()
  for hh in ['18','12','06','00']:
   cyc=f'{d:%Y%m%d}{hh}'
   try:r=S.get(gefs_url(cyc,required_lead),timeout=35)
   except Exception as e: attempts.append((cyc,'EXC',str(e)[:80])); continue
   attempts.append((cyc,r.status_code,len(r.content),r.content[:4]))
   if r.status_code==200 and r.content[:4]==b'GRIB': return cyc
 raise RuntimeError(f'No GEFS control cycle with lead {required_lead} discovered; attempts={attempts}')

def fetch_gefs(leads):
 cyc=discover_gefs(max(leads) if leads else 0); base=datetime.strptime(cyc,'%Y%m%d%H').replace(tzinfo=timezone.utc); out=[]
 with tempfile.TemporaryDirectory() as td:
  for lead in leads:
   url=gefs_url(cyc,lead); r=S.get(url,timeout=90); r.raise_for_status()
   if r.content[:4]!=b'GRIB': raise RuntimeError(f'GEFS non-GRIB lead {lead}')
   p=Path(td)/f'g_{lead}.grib2'; p.write_bytes(r.content); assert_grib_valid_time(p,base,base+timedelta(hours=lead),f'GEFS-control lead {lead}'); rows=nearest(p); vals={}
   for n,s,v in rows: vals.setdefault(n,[]).append({'stepRange':s,'value':v})
   def one(*ns):
    for n in ns:
     if n in vals and vals[n]: return vals[n][0]['value']
   u=one('10u','u'); v=one('10v','v'); g=one('gust','10fg')
   rec={'model':'GEFS-control','run_time_utc':base.isoformat(),'forecast_lead_hours':lead,'valid_time_utc':(base+timedelta(hours=lead)).isoformat(),'source':'NOAA/NCEP NOMADS GEFS raw GRIB2','source_urls':[url],'values':vals}
   if u is not None and v is not None: rec['derived']=derived(u,v,g)
   out.append(rec)
 return out

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--test',action='store_true'); a=ap.parse_args(); leads=[0,12,24,36,48] if a.test else list(range(0,49,3))
 p=Path(os.getenv('COLLECTOR_MODEL_FILE','work/model_snapshot.json')); data=json.loads(p.read_text()); errors=[]
 for name,fn in [('ECMWF-IFS',fetch_ifs),('GEFS-control',fetch_gefs)]:
  try:data['models'][name]=fn(leads)
  except Exception as e:data['models'][name]=[];errors.append(f'{name}: {type(e).__name__}: {e}')
  good=sum('derived' in x for x in data['models'][name]); complete=(len(data['models'][name])==len(leads) and good==len(leads))
  data['quality'][name]={'records':len(data['models'][name]),'derived_records':good,'success':complete,'complete_requested_horizon':complete}
 goodmodels=[n for n,q in data['quality'].items() if isinstance(q,dict) and q.get('success')]
 data['quality']['successful_models']=goodmodels;data['quality']['minimum_two_independent_models_met']=len(goodmodels)>=2;data['quality'].setdefault('errors',[]);data['quality']['errors']+=errors
 data['extended_retrieved_at_utc']=datetime.now(timezone.utc).isoformat()
 data['retrieved_at_utc']=data['extended_retrieved_at_utc']
 p.write_text(json.dumps(data,separators=(',',':'))+'\n',encoding='utf-8')
 print(json.dumps({'ECMWF-IFS':data['quality']['ECMWF-IFS'],'GEFS-control':data['quality']['GEFS-control'],'errors':errors,'output_bytes':p.stat().st_size},indent=2))
 if not data['quality']['ECMWF-IFS']['success'] or not data['quality']['GEFS-control']['success']: raise SystemExit(2)
if __name__=='__main__':main()
