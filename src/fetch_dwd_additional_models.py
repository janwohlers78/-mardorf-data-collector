#!/usr/bin/env python3
"""Fetch DWD core models ICON-EU and ICON-D2-EPS for Mardorf."""
import argparse,bz2,json,math,re,statistics,subprocess,tempfile,os,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import urljoin
import requests
from grib_identity import assert_grib_run_time

LAT=52.4942; LON=9.3418
S=requests.Session(); S.headers.update({'User-Agent':'mardorf-data-collector/1.0 (+github-actions)'})
OPEN_METEO_D2_EPS_META='https://api.open-meteo.com/data/dwd_icon_d2_eps/static/meta.json'
OPEN_METEO_D2_EPS_API='https://ensemble-api.open-meteo.com/v1/ensemble'
EPS_EXPECTED_MEMBERS=20
EPS_SETTLING_SECONDS=600


def nearest(path):
    p=subprocess.run(['grib_ls','-l',f'{LAT},{LON},1','-p','shortName,stepRange',str(path)],capture_output=True,text=True,check=True)
    rows=[]
    for line in p.stdout.splitlines():
        x=line.strip().split()
        if len(x)>=3:
            try: rows.append((x[0],x[1],float(x[-1])))
            except Exception: pass
    if not rows: raise RuntimeError(f'No nearest values parsed: {p.stdout[:500]}')
    return rows


def derived_from_speed(speed,direction,gust=None):
    out={'wind_speed_ms':round(speed,3),'wind_speed_kt':round(speed*1.943844,2),'wind_direction_deg':round(direction%360,1)}
    if gust is not None: out.update(gust_ms=round(gust,3),gust_kt=round(gust*1.943844,2),gust_factor=round(gust/speed,2) if speed>.2 else None)
    return out


def derived(u,v,g=None):
    sp=math.hypot(u,v); direction=(270-math.degrees(math.atan2(v,u)))%360
    return derived_from_speed(sp,direction,g)


def directory_hrefs(model,hh,param):
    directory=f'https://opendata.dwd.de/weather/nwp/{model}/grib/{hh}/{param}/'
    r=S.get(directory,timeout=40); r.raise_for_status()
    hrefs=re.findall(r'href=["\']([^"\']+\.grib2\.bz2)["\']',r.text,re.I)
    return directory,[urljoin(directory,h) for h in hrefs]


def discover_cycle(model,required_lead=0):
    """Newest DWD cycle which really contains the requested farthest lead."""
    per_cycle={}; diagnostics=[]
    for hh in ['00','03','06','09','12','15','18','21']:
        try:
            _,hrefs=directory_hrefs(model,hh,'u_10m')
            for href in hrefs:
                m=re.search(r'_(20\d{8})_(\d{3})_',href)
                if m: per_cycle.setdefault(m.group(1),set()).add(int(m.group(2)))
        except Exception as e:
            diagnostics.append((hh,type(e).__name__))
    eligible=[cycle for cycle,ls in per_cycle.items() if required_lead in ls]
    if not eligible:
        summary=sorted((c,max(ls) if ls else -1) for c,ls in per_cycle.items())[-20:]
        raise RuntimeError(f'No {model} cycle with lead {required_lead} discovered; cycles={summary}; errors={diagnostics}')
    return max(eligible)


def find_dwd_file(model,cycle,lead,param):
    directory,hrefs=directory_hrefs(model,cycle[-2:],param)
    lead_token=f'_{lead:03d}_'
    candidates=[h for h in hrefs if cycle in h and lead_token in h and param in h]
    if not candidates:candidates=[h for h in hrefs if cycle in h and lead_token in h]
    if not candidates:raise RuntimeError(f'No DWD file for {model} cycle={cycle} lead={lead} param={param}; directory={directory}')
    return sorted(candidates)[0]


def fetch_icon_eu(leads,required_cycle_lead=None):
    model='icon-eu'
    requested=max(leads) if leads else 0
    selector=requested if required_cycle_lead is None else max(requested,int(required_cycle_lead))
    try:
        cycle=discover_cycle(model,selector)
        selection={'requested_cycle_lead_hours':selector,'fallback_used':False}
    except Exception as primary:
        if selector<=requested: raise
        cycle=discover_cycle(model,requested)
        selection={'requested_cycle_lead_hours':selector,'fallback_used':True,
                   'fallback_required_lead_hours':requested,
                   'primary_selection_error':f'{type(primary).__name__}: {primary}'}
    base=datetime.strptime(cycle,'%Y%m%d%H').replace(tzinfo=timezone.utc); out=[]
    params=['u_10m','v_10m','vmax_10m','tot_prec','cape_ml']
    with tempfile.TemporaryDirectory() as td:
        for lead in leads:
            vals={}; urls=[]
            for param in params:
                try:
                    u=find_dwd_file(model,cycle,lead,param); urls.append(u)
                    r=S.get(u,timeout=90); r.raise_for_status(); p=Path(td)/f'eu_{param}_{lead}.grib2'; p.write_bytes(bz2.decompress(r.content))
                    assert_grib_run_time(p,base,f'ICON-EU {param} lead {lead}')
                    vals[param]=[{'stepRange':s,'value':v} for _,s,v in nearest(p)]
                except Exception as e: vals[param]={'error':f'{type(e).__name__}: {e}'}
            one=lambda p: vals[p][0]['value'] if isinstance(vals.get(p),list) and vals[p] else None
            rec={'model':'ICON-EU','run_time_utc':base.isoformat(),'forecast_lead_hours':lead,'valid_time_utc':(base+timedelta(hours=lead)).isoformat(),'source':'DWD Open Data raw GRIB2','source_urls':urls,'values':vals,'cycle_selection':selection}
            if one('u_10m') is not None and one('v_10m') is not None: rec['derived']=derived(one('u_10m'),one('v_10m'),one('vmax_10m'))
            out.append(rec)
    return out


def percentile(values,p):
    x=sorted(values); k=(len(x)-1)*p; lo=int(math.floor(k)); hi=int(math.ceil(k))
    return x[lo] if lo==hi else x[lo]*(hi-k)+x[hi]*(k-lo)


def circular_mean(values):
    if not values:return None
    s=sum(math.sin(math.radians(v)) for v in values); c=sum(math.cos(math.radians(v)) for v in values)
    if abs(s)<1e-12 and abs(c)<1e-12:return None
    return math.degrees(math.atan2(s,c))%360


def eps_statistics(members):
    winds=[m['wind_speed_ms'] for m in members]; dirs=[m['wind_direction_deg'] for m in members if 'wind_direction_deg' in m]; gusts=[m['gust_ms'] for m in members if 'gust_ms' in m]; stats={}
    if winds:
        stats={'member_count':len(winds),'wind_speed_ms_median':round(statistics.median(winds),3),'wind_speed_ms_p10':round(percentile(winds,.10),3),'wind_speed_ms_p25':round(percentile(winds,.25),3),'wind_speed_ms_p75':round(percentile(winds,.75),3),'wind_speed_ms_p90':round(percentile(winds,.90),3),'wind_speed_ms_stddev':round(statistics.pstdev(winds),3)}
    cm=circular_mean(dirs)
    if cm is not None:stats['wind_direction_deg_circular_mean']=round(cm,1)
    if gusts:stats.update(gust_ms_median=round(statistics.median(gusts),3),gust_ms_p90=round(percentile(gusts,.90),3))
    return stats


def member_columns(hourly,prefix):
    out={0:hourly[prefix]} if prefix in hourly else {}
    for key,values in hourly.items():
        m=re.fullmatch(re.escape(prefix)+r'_member(\d+)',key)
        if m: out[int(m.group(1))]=values
    return out

def member_map(hourly,prefix):
    return member_columns(hourly,prefix)


def _epoch_utc(value,label):
    if value is None: raise RuntimeError(f'Open-Meteo EPS metadata missing {label}')
    return datetime.fromtimestamp(int(value),tz=timezone.utc)

def fetch_eps_metadata():
    r=S.get(OPEN_METEO_D2_EPS_META,timeout=30);r.raise_for_status();d=r.json()
    init=_epoch_utc(d.get('last_run_initialisation_time'),'last_run_initialisation_time')
    avail=_epoch_utc(d.get('last_run_availability_time'),'last_run_availability_time')
    return {
        'url':r.url,
        'http_status':r.status_code,
        'last_run_initialisation_time_utc':init.isoformat(),
        'last_run_availability_time_utc':avail.isoformat(),
        'last_run_modification_time_utc':_epoch_utc(d.get('last_run_modification_time'),'last_run_modification_time').isoformat() if d.get('last_run_modification_time') is not None else None,
        'temporal_resolution_seconds':d.get('temporal_resolution_seconds'),
        'update_interval_seconds':d.get('update_interval_seconds'),
    }

def _meta_dt(meta,key):
    return datetime.fromisoformat(meta[key]).astimezone(timezone.utc)

def fetch_icon_d2_eps(leads):
    """Fetch one stable, explicitly identified Open-Meteo DWD ICON-D2-EPS run.

    Run identity comes from Open-Meteo's model metadata. The API is queried only
    after the documented 10-minute settling period, metadata is re-read after the
    ensemble request, and the run must be unchanged. The corresponding DWD cycle
    and farthest requested lead must also exist in DWD Open Data.
    """
    meta_before=fetch_eps_metadata()
    base=_meta_dt(meta_before,'last_run_initialisation_time_utc')
    availability=_meta_dt(meta_before,'last_run_availability_time_utc')
    retrieved=datetime.now(timezone.utc)
    settle_age=(retrieved-availability).total_seconds()
    if settle_age < EPS_SETTLING_SECONDS:
        raise RuntimeError(
            f'Open-Meteo ICON-D2-EPS run not settled: run={base.isoformat()} '
            f'availability={availability.isoformat()} age_seconds={round(settle_age,1)} '
            f'required_seconds={EPS_SETTLING_SECONDS}')
    if base.minute or base.second or base.hour%3:
        raise RuntimeError(f'Open-Meteo ICON-D2-EPS metadata returned non-3-hour DWD cycle: {base.isoformat()}')

    cycle=base.strftime('%Y%m%d%H')
    farthest=max(leads) if leads else 0
    dwd_confirmation=find_dwd_file('icon-d2-eps',cycle,farthest,'u_10m')

    q={'latitude':LAT,'longitude':LON,
       'hourly':'wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation,cape',
       'models':'dwd_icon_d2_eps','past_days':1,'forecast_days':4,
       'wind_speed_unit':'ms','timezone':'GMT'}
    r=S.get(OPEN_METEO_D2_EPS_API,params=q,timeout=90);r.raise_for_status();payload=r.json()
    hourly=payload.get('hourly') or {};times=hourly.get('time') or []
    speed=member_map(hourly,'wind_speed_10m');direction=member_map(hourly,'wind_direction_10m')
    gust=member_map(hourly,'wind_gusts_10m');precip=member_map(hourly,'precipitation');cape=member_map(hourly,'cape')
    member_ids=sorted(set(speed)&set(direction))
    expected_ids=list(range(EPS_EXPECTED_MEMBERS))
    if member_ids!=expected_ids:
        raise RuntimeError(f'ICON-D2-EPS member identity mismatch: expected={expected_ids} received={member_ids}')

    meta_after=fetch_eps_metadata()
    if meta_after['last_run_initialisation_time_utc']!=meta_before['last_run_initialisation_time_utc']:
        raise RuntimeError(
            'Open-Meteo ICON-D2-EPS run changed during acquisition: '
            f"before={meta_before['last_run_initialisation_time_utc']} "
            f"after={meta_after['last_run_initialisation_time_utc']}")
    if _meta_dt(meta_after,'last_run_availability_time_utc')!=availability:
        raise RuntimeError(
            'Open-Meteo ICON-D2-EPS availability metadata changed during acquisition: '
            f"before={meta_before['last_run_availability_time_utc']} "
            f"after={meta_after['last_run_availability_time_utc']}")

    identity={
        'verification_status':'verified_stable_metadata_and_dwd_cycle',
        'model_id':'dwd_icon_d2_eps',
        'run_time_utc':base.isoformat(),
        'metadata_before':meta_before,
        'metadata_after':meta_after,
        'settling_age_seconds_at_request':round(settle_age,1),
        'minimum_settling_seconds':EPS_SETTLING_SECONDS,
        'dwd_cycle_confirmation_url':dwd_confirmation,
        'expected_member_ids':expected_ids,
        'source_timestamp_semantics':'Open-Meteo last_run_initialisation_time is the model reference/initialisation time',
    }

    index={t:i for i,t in enumerate(times)};out=[]
    for lead in leads:
        valid=base+timedelta(hours=lead);key=valid.strftime('%Y-%m-%dT%H:%M');i=index.get(key);members=[]
        rec_error=None
        if i is None:
            rec_error=f'valid timestamp {key} absent from Open-Meteo hourly time axis'
        else:
            for m in member_ids:
                if i>=len(speed[m]) or i>=len(direction[m]) or speed[m][i] is None or direction[m][i] is None:
                    continue
                g=gust[m][i] if m in gust and i<len(gust[m]) else None
                d=derived_from_speed(float(speed[m][i]),float(direction[m][i]),float(g) if g is not None else None);d['member']=m
                if m in precip and i<len(precip[m]) and precip[m][i] is not None:d['precipitation']=precip[m][i]
                if m in cape and i<len(cape[m]) and cape[m][i] is not None:d['cape']=cape[m][i]
                members.append(d)
            got_ids=sorted(m['member'] for m in members)
            if got_ids!=expected_ids:
                rec_error=f'ensemble member values incomplete for lead {lead}: expected={expected_ids} received={got_ids}'
        stats=eps_statistics(members)
        rec={'model':'ICON-D2-EPS','run_time_utc':base.isoformat(),'forecast_lead_hours':lead,
             'valid_time_utc':valid.isoformat(),
             'source':'Open-Meteo Ensemble API named model dwd_icon_d2_eps; DWD cycle independently confirmed',
             'source_url':r.url,'source_run_identity':identity,'members':members,'ensemble_statistics':stats}
        if rec_error:
            rec['error_type']='EnsembleCompletenessError';rec['error_message']=rec_error
        elif len(members)==EPS_EXPECTED_MEMBERS:
            der={'wind_speed_ms':stats['wind_speed_ms_median'],'wind_speed_kt':round(stats['wind_speed_ms_median']*1.943844,2),
                 'ensemble_member_count':len(members)}
            if 'wind_direction_deg_circular_mean' in stats:der['wind_direction_deg']=stats['wind_direction_deg_circular_mean']
            if 'gust_ms_median' in stats:
                der['gust_ms']=stats['gust_ms_median'];der['gust_kt']=round(stats['gust_ms_median']*1.943844,2)
                der['gust_factor']=round(stats['gust_ms_median']/stats['wind_speed_ms_median'],2) if stats['wind_speed_ms_median']>.2 else None
            rec['derived']=der
        out.append(rec)
    return out


def write_consolidated_report(rep,data,now):
    names=['ICON-D2','ICON-EU','ECMWF-IFS','GFS','GEFS-control','ICON-D2-EPS'];q=data.get('quality',{})
    lines=[f'# Model fetch consolidated {now:%Y-%m-%d %H:%M UTC}','',f'- Spot: {LAT}, {LON}',f'- Mode: {data.get("mode")}',f'- Requested leads: {len(data.get("leads_hours",[]))} ({min(data.get("leads_hours",[0]))}-{max(data.get("leads_hours",[0]))} h)',''];all_complete=True
    for name in names:
        x=q.get(name,{});complete=bool(x.get('success') and x.get('derived_records')==x.get('records') and x.get('records')==len(data.get('leads_hours',[])));all_complete=all_complete and complete;lines.append(f'- {name}: {x.get("derived_records",0)}/{x.get("records",0)} derived; complete={complete}')
    lines += ['',f'- All six model paths complete: {all_complete}',f'- Minimum two-model rule: {q.get("minimum_two_independent_models_met")}'];errs=q.get('errors',[])
    if errs:lines += ['','## Errors']+[f'- {e}' for e in errs]
    (rep/'latest.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--test',action='store_true');a=ap.parse_args();leads=[0,12,24,36,48] if a.test else list(range(0,49,3))
    p=Path(os.getenv('COLLECTOR_MODEL_FILE','work/model_snapshot.json'));data=json.loads(p.read_text());errors=[]
    for name,fn in [('ICON-EU',fetch_icon_eu),('ICON-D2-EPS',fetch_icon_d2_eps)]:
        try:data['models'][name]=fn(leads)
        except Exception as e:data['models'][name]=[];errors.append(f'{name}: {type(e).__name__}: {e}')
        good=sum('derived' in x for x in data['models'][name]);complete=(len(data['models'][name])==len(leads) and good==len(leads));data['quality'][name]={'records':len(data['models'][name]),'derived_records':good,'success':complete,'complete_requested_horizon':complete}
    data['quality']['successful_models']=[n for n,q in data['quality'].items() if isinstance(q,dict) and q.get('success')];data['quality'].setdefault('errors',[]);data['quality']['errors']+=errors;data['dwd_additional_retrieved_at_utc']=datetime.now(timezone.utc).isoformat()
    data['retrieved_at_utc']=data['dwd_additional_retrieved_at_utc']
    p.write_text(json.dumps(data,separators=(',',':'))+'\n',encoding='utf-8')
    print(json.dumps({'ICON-EU':data['quality']['ICON-EU'],'ICON-D2-EPS':data['quality']['ICON-D2-EPS'],'errors':errors,'output_bytes':p.stat().st_size},indent=2))
    if not data['quality']['ICON-EU']['success'] or not data['quality']['ICON-D2-EPS']['success']:raise SystemExit(2)

if __name__=='__main__':main()
