#!/usr/bin/env python3
"""Extend eligible model paths for reproducible medium-range weekend guidance.

ICON-D2 and ICON-D2-EPS remain short-range 48 h paths. ICON-EU, ECMWF-IFS,
GFS and GEFS-control are extended at 3-hour cadence through 72 h and 6-hour
cadence from 78 through 120 h. Leads >72 h are synoptic guidance only and are
not treated as operational beginner kite clearance.
"""
import bz2,json,math,re,subprocess,tempfile,os
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import urlencode,urljoin
import requests
from ecmwf.opendata import Client
from grib_identity import assert_grib_run_time,grib_run_times

LAT=52.4942;LON=9.3418;SNAP=Path(os.getenv('COLLECTOR_MODEL_FILE','work/model_snapshot.json'))
TARGET_LEADS=list(range(51,73,3))+list(range(78,121,6))
EXPECTED={'ICON-D2':48,'ICON-D2-EPS':48,'ICON-EU':120,'ECMWF-IFS':120,'GFS':120,'GEFS-control':120}
ECMWF_SOURCE=os.getenv('ECMWF_OPEN_DATA_SOURCE','azure')
S=requests.Session();S.headers.update({'User-Agent':'mardorf-data-collector/1.0 (+github-actions)'})


def nearest(path):
    p=subprocess.run(['grib_ls','-l',f'{LAT},{LON},1','-p','shortName,stepRange',str(path)],capture_output=True,text=True,check=True);rows=[]
    for line in p.stdout.splitlines():
        x=line.strip().split()
        if len(x)>=3:
            try:rows.append((x[0],x[1],float(x[-1])))
            except Exception:pass
    if not rows:raise RuntimeError('no nearest-grid values parsed')
    return rows


def derived(u,v,g=None):
    sp=math.hypot(u,v);d=(270-math.degrees(math.atan2(v,u)))%360;z={'wind_speed_ms':round(sp,3),'wind_speed_kt':round(sp*1.943844,2),'wind_direction_deg':round(d,1)}
    if g is not None:z.update(gust_ms=round(g,3),gust_kt=round(g*1.943844,2),gust_factor=round(g/sp,2) if sp>.2 else None)
    return z


def cycle_from_existing(data,model):
    recs=data.get('models',{}).get(model,[])
    if not recs:raise RuntimeError(f'no existing {model} records')
    runs={r.get('run_time_utc') for r in recs if r.get('run_time_utc')}
    if len(runs)!=1:raise RuntimeError(f'{model} base snapshot has {len(runs)} run identities: {sorted(runs)}')
    return datetime.fromisoformat(next(iter(runs))).astimezone(timezone.utc)

def cycle_horizon(model,base):
    if model in ('ICON-D2','ICON-D2-EPS'): return 48
    if model=='ICON-EU': return 120 if base.hour in (0,6,12,18) else 51
    if model=='ECMWF-IFS': return 120
    return 120

def leads_for_cycle(model,base):
    limit=cycle_horizon(model,base)
    return [lead for lead in TARGET_LEADS if lead<=limit]

def grib_run_time(path):
    observed=grib_run_times(path)
    if len(observed)!=1:
        raise RuntimeError(
            f'ECMWF GRIB batch contains multiple model reference times: '
            f'{[x.isoformat() for x in observed]}')
    return observed[0]

def gfs_url(base,lead,gefs=False):
    cyc=base.strftime('%Y%m%d%H');ymd,hh=cyc[:8],cyc[8:]
    if gefs:
        q={'file':f'gec00.t{hh}z.pgrb2s.0p25.f{lead:03d}','lev_10_m_above_ground':'on','lev_surface':'on','var_UGRD':'on','var_VGRD':'on','var_GUST':'on','var_APCP':'on','subregion':'','leftlon':f'{LON-.3:.3f}','rightlon':f'{LON+.3:.3f}','toplat':f'{LAT+.3:.3f}','bottomlat':f'{LAT-.3:.3f}','dir':f'/gefs.{ymd}/{hh}/atmos/pgrb2sp25'}
        return 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gefs_atmos_0p25s.pl?'+urlencode(q)
    q={'file':f'gfs.t{hh}z.pgrb2.0p25.f{lead:03d}','lev_10_m_above_ground':'on','lev_surface':'on','var_UGRD':'on','var_VGRD':'on','var_GUST':'on','var_APCP':'on','subregion':'','leftlon':f'{LON-.3:.3f}','rightlon':f'{LON+.3:.3f}','toplat':f'{LAT+.3:.3f}','bottomlat':f'{LAT-.3:.3f}','dir':f'/gfs.{ymd}/{hh}/atmos'}
    return 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?'+urlencode(q)


def fetch_noaa(data,model,gefs=False):
    base=cycle_from_existing(data,model);out=[]
    with tempfile.TemporaryDirectory() as td:
        for lead in leads_for_cycle(model,base):
            url=gfs_url(base,lead,gefs);r=S.get(url,timeout=90);r.raise_for_status()
            if r.content[:4]!=b'GRIB':raise RuntimeError(f'{model} lead {lead}: non-GRIB response')
            p=Path(td)/f'{model}_{lead}.grib2';p.write_bytes(r.content);assert_grib_run_time(p,base,f'{model} extension lead {lead}');vals={}
            for n,s,v in nearest(p):vals.setdefault(n,[]).append({'stepRange':s,'value':v})
            def one(*ns):
                for n in ns:
                    if vals.get(n):return vals[n][0]['value']
                return None
            u=one('10u','u');v=one('10v','v');g=one('gust','10fg');rec={'model':model,'run_time_utc':base.isoformat(),'forecast_lead_hours':lead,'valid_time_utc':(base+timedelta(hours=lead)).isoformat(),'source':'NOAA/NCEP NOMADS raw GRIB2','source_urls':[url],'values':vals}
            if u is not None and v is not None:rec['derived']=derived(u,v,g)
            out.append(rec)
    return out


def step_end(step_range):
    nums=re.findall(r'\d+',str(step_range))
    return int(nums[-1]) if nums else None

def fetch_ifs(data):
    base=cycle_from_existing(data,'ECMWF-IFS');out=[]
    leads=leads_for_cycle('ECMWF-IFS',base)
    client=Client(source=ECMWF_SOURCE,model='ifs',resol='0p25')
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'ifs_medium_range_batch.grib2'
        client.retrieve(
            date=base.strftime('%Y%m%d'),time=base.hour,stream='oper',type='fc',
            step=leads,param=['10u','10v','10fg','tp','mucape'],target=str(p))
        actual=grib_run_time(p)
        if actual!=base:
            raise RuntimeError(f'ECMWF run identity mismatch: expected {base.isoformat()} got {actual.isoformat()}')
        bylead={int(x):{} for x in leads}
        for n,s,v in nearest(p):
            lead=step_end(s)
            if lead not in bylead:continue
            bylead[lead].setdefault(n,[]).append({'stepRange':s,'value':v})
        for lead in leads:
            vals=bylead[int(lead)]
            def one(*ns):
                for n in ns:
                    if vals.get(n):return vals[n][0]['value']
                return None
            u=one('10u');v=one('10v');g=one('10fg','10fg3','10fg6')
            rec={'model':'ECMWF-IFS','run_time_utc':actual.isoformat(),'forecast_lead_hours':lead,
                 'valid_time_utc':(actual+timedelta(hours=lead)).isoformat(),
                 'source':f'ECMWF Open Data via {ECMWF_SOURCE} mirror raw GRIB2','values':vals,
                 'source_request':{'date':base.strftime('%Y%m%d'),'time':base.hour,'steps':leads}}
            if u is not None and v is not None:rec['derived']=derived(u,v,g)
            out.append(rec)
    return out


def dwd_files(base,param):
    hh=base.strftime('%H');directory=f'https://opendata.dwd.de/weather/nwp/icon-eu/grib/{hh}/{param}/';r=S.get(directory,timeout=45);r.raise_for_status();hrefs=re.findall(r'href=["\']([^"\']+\.grib2\.bz2)["\']',r.text,re.I);return [urljoin(directory,h) for h in hrefs]


def fetch_icon_eu(data):
    base=cycle_from_existing(data,'ICON-EU');cycle=base.strftime('%Y%m%d%H');out=[];cache={}
    with tempfile.TemporaryDirectory() as td:
        for lead in leads_for_cycle('ICON-EU',base):
            vals={};urls=[]
            for param in ['u_10m','v_10m','vmax_10m','tot_prec','cape_ml']:
                if param not in cache:cache[param]=dwd_files(base,param)
                tok=f'_{lead:03d}_';cand=[u for u in cache[param] if cycle in u and tok in u and param in u]
                if not cand:vals[param]={'error':'file_not_published'};continue
                url=sorted(cand)[0];urls.append(url)
                try:
                    r=S.get(url,timeout=90);r.raise_for_status();p=Path(td)/f'eu_{param}_{lead}.grib2';p.write_bytes(bz2.decompress(r.content));assert_grib_run_time(p,base,f'ICON-EU extension {param} lead {lead}');vals[param]=[{'stepRange':s,'value':v} for _,s,v in nearest(p)]
                except Exception as e:vals[param]={'error':f'{type(e).__name__}: {e}'}
            def one(name):
                x=vals.get(name);return x[0]['value'] if isinstance(x,list) and x else None
            rec={'model':'ICON-EU','run_time_utc':base.isoformat(),'forecast_lead_hours':lead,'valid_time_utc':(base+timedelta(hours=lead)).isoformat(),'source':'DWD Open Data raw GRIB2','source_urls':urls,'values':vals}
            if one('u_10m') is not None and one('v_10m') is not None:rec['derived']=derived(one('u_10m'),one('v_10m'),one('vmax_10m'))
            out.append(rec)
    return out


def expected_leads(model,recs):
    if not recs:return []
    base=datetime.fromisoformat(recs[0]['run_time_utc']).astimezone(timezone.utc)
    horizon=cycle_horizon(model,base)
    if horizon<=48:return list(range(0,horizon+1,3))
    return list(range(0,min(72,horizon)+1,3))+list(range(78,horizon+1,6))


def quality(data):
    q=data.setdefault('quality',{});successful=[]
    for model,recs in data.get('models',{}).items():
        exp=expected_leads(model,recs)
        got={int(r['forecast_lead_hours']) for r in recs if r.get('derived') and r.get('forecast_lead_hours') is not None}
        complete=bool(exp) and all(h in got for h in exp)
        horizon=max(exp) if exp else None
        q[model]={'records':len(recs),'derived_records':sum(bool(r.get('derived')) for r in recs),
                  'success':complete,'complete_requested_horizon':complete,
                  'expected_horizon_hours':horizon,'max_derived_lead_hours':max(got) if got else None,
                  'expected_lead_count':len(exp)}
        if complete:successful.append(model)
    independent=[m for m in successful if m!='GEFS-control']
    q['successful_models']=successful
    q['minimum_two_independent_models_met']=len({('GFS' if m in ('GFS','GEFS-control') else 'DWD-ICON' if m.startswith('ICON-') else m) for m in independent})>=2
    q['horizon_policy']='provider_cycle_specific_medium_range_v3'
    q['operational_max_horizon_hours']=72;q['synoptic_guidance_max_horizon_hours']=120


def main():
    data=json.loads(SNAP.read_text(encoding='utf-8'));errors=[]
    jobs=[('GFS',lambda:fetch_noaa(data,'GFS')),('GEFS-control',lambda:fetch_noaa(data,'GEFS-control',True)),('ECMWF-IFS',lambda:fetch_ifs(data)),('ICON-EU',lambda:fetch_icon_eu(data))]
    for model,fn in jobs:
        try:data['models'][model]=[r for r in data['models'].get(model,[]) if int(r.get('forecast_lead_hours',999))<=48]+fn()
        except Exception as e:errors.append(f'{model}: {type(e).__name__}: {e}')
    data['leads_hours']=sorted(set(list(range(0,73,3))+list(range(78,121,6))));data['horizon_extension_retrieved_at_utc']=datetime.now(timezone.utc).isoformat();data['retrieved_at_utc']=data['horizon_extension_retrieved_at_utc'];data.setdefault('quality',{}).setdefault('errors',[]);data['quality']['errors']+=errors;quality(data);SNAP.write_text(json.dumps(data,separators=(',',':'))+'\n',encoding='utf-8')
    print(json.dumps({'errors':errors,'quality':data['quality'],'output_bytes':SNAP.stat().st_size},indent=2))
if __name__=='__main__':main()
