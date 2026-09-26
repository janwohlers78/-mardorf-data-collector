#!/usr/bin/env python3
"""Fetch DWD core models ICON-EU and ICON-D2-EPS for Mardorf."""
import argparse,bz2,hashlib,json,math,re,statistics,subprocess,tempfile,os,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import urljoin
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from grib_identity import assert_grib_valid_time

LAT=52.4942; LON=9.3418
S=requests.Session(); S.headers.update({'User-Agent':'mardorf-data-collector/1.0 (+github-actions)'})
_retry=Retry(total=3,connect=3,read=3,status=3,backoff_factor=1.0,
             status_forcelist=(408,429,500,502,503,504),
             allowed_methods=frozenset(['GET']),raise_on_status=False,
             respect_retry_after_header=True)
S.mount('https://',HTTPAdapter(max_retries=_retry,pool_connections=4,pool_maxsize=4))
OPEN_METEO_D2_EPS_META='https://api.open-meteo.com/data/dwd_icon_d2_eps/static/meta.json'
OPEN_METEO_D2_EPS_API='https://ensemble-api.open-meteo.com/v1/ensemble'
EPS_EXPECTED_MEMBERS=20
EPS_SETTLING_SECONDS=600


class NearestRows(list):
    pass

def nearest(path):
    p=subprocess.run(['grib_ls','-l',f'{LAT},{LON},1','-p','shortName,stepRange',str(path)],capture_output=True,text=True,check=True)
    m=re.search(r'Grid Point chosen .*?latitude=([+-]?\d+(?:\.\d+)?) longitude=([+-]?\d+(?:\.\d+)?)',p.stdout)
    if not m: raise RuntimeError(f'Cannot identify ecCodes selected grid point for {path}: {p.stdout[:700]}')
    rows=NearestRows();rows.point={'latitude':float(m.group(1)),'longitude':float(m.group(2)),'selection':'ecCodes_nearest_grid_point'}
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
    """Newest DWD cycle with all wind-critical fields at the requested lead."""
    critical=('u_10m','v_10m','vmax_10m');coverage={};diagnostics=[]
    for hh in ['00','03','06','09','12','15','18','21']:
        for param in critical:
            try:
                _,hrefs=directory_hrefs(model,hh,param)
                for href in hrefs:
                    m=re.search(r'_(20\d{8})_(\d{3})_',href)
                    if m:
                        coverage.setdefault(m.group(1),{}).setdefault(param,set()).add(int(m.group(2)))
            except Exception as e:
                diagnostics.append((hh,param,type(e).__name__))
    eligible=[
        cycle for cycle,fields in coverage.items()
        if all(required_lead in fields.get(param,set()) for param in critical)
    ]
    if not eligible:
        summary={cycle:{p:max(v) if v else -1 for p,v in fields.items()} for cycle,fields in sorted(coverage.items())[-20:]}
        raise RuntimeError(f'No {model} cycle with all critical fields at lead {required_lead}; cycles={summary}; errors={diagnostics}')
    return max(eligible)


def find_dwd_file(model,cycle,lead,param):
    directory,hrefs=directory_hrefs(model,cycle[-2:],param)
    lead_token=f'_{lead:03d}_'
    candidates=[h for h in hrefs if cycle in h and lead_token in h and param in h]
    if not candidates:candidates=[h for h in hrefs if cycle in h and lead_token in h]
    if not candidates:raise RuntimeError(f'No DWD file for {model} cycle={cycle} lead={lead} param={param}; directory={directory}')
    return sorted(candidates)[0]


def find_dwd_regular_file(model,cycle,lead,param):
    directory,hrefs=directory_hrefs(model,cycle[-2:],param)
    lead_token=f'_{lead:03d}_'
    candidates=[
        h for h in hrefs
        if cycle in h and lead_token in h and param in h and 'regular-lat-lon' in h
    ]
    if not candidates:
        raise RuntimeError(
            f'No regular-lat-lon DWD file for {model} cycle={cycle} lead={lead} param={param}; directory={directory}')
    return sorted(candidates)[0]


def grib_grid_identity(path):
    keys='gridType,gridDefinitionTemplateNumber,numberOfGridUsed,uuidOfHGrid,numberOfDataPoints'
    p=subprocess.run(['grib_get','-p',keys,str(path)],capture_output=True,text=True,check=True)
    line=next((x.strip() for x in p.stdout.splitlines() if x.strip()),'')
    parts=line.split()
    if len(parts)<5:
        raise RuntimeError(f'Cannot parse DWD grid identity: {p.stdout[:700]} {p.stderr[:700]}')
    return {
        'grid_type':parts[0],
        'grid_definition_template_number':parts[1],
        'number_of_grid_used':parts[2],
        'uuid_of_horizontal_grid':parts[3],
        'number_of_data_points':parts[4],
    }


def verify_dwd_spatial_provenance(eps_url,regular_url,base,valid,returned):
    """Verify DWD EPS native-grid identity and DWD 0.02-degree extraction-grid parity.

    DWD publishes ICON-D2-EPS on the native triangular grid. ecCodes cannot
    perform a nearest-point lookup on that GRIB without the separate external
    native-grid definition. We therefore verify the EPS file's own grid identity
    directly, and independently compare the API-returned coordinate with DWD's
    operational regular-lat-lon ICON-D2 output grid for the same cycle/lead.
    """
    with tempfile.TemporaryDirectory() as td:
        eps_resp=S.get(eps_url,timeout=90);eps_resp.raise_for_status()
        eps_path=Path(td)/'eps_native.grib2'
        eps_path.write_bytes(bz2.decompress(eps_resp.content))
        assert_grib_valid_time(eps_path,base,valid,'ICON-D2-EPS native grid identity')
        native_identity=grib_grid_identity(eps_path)
        native_ok=(
            native_identity.get('grid_type') in ('unstructured_grid','unstructured')
            and str(native_identity.get('number_of_grid_used'))=='47'
            and bool(native_identity.get('uuid_of_horizontal_grid'))
        )

        reg_resp=S.get(regular_url,timeout=90);reg_resp.raise_for_status()
        reg_path=Path(td)/'d2_regular.grib2'
        reg_path.write_bytes(bz2.decompress(reg_resp.content))
        assert_grib_valid_time(reg_path,base,valid,'ICON-D2 regular grid parity')
        rows=nearest(reg_path)
        dwd_regular_point=rows.point

    rlat=float(returned['latitude']);rlon=float(returned['longitude'])
    nlat=float(dwd_regular_point['latitude']);nlon=float(dwd_regular_point['longitude'])
    dlat=abs(rlat-nlat);dlon=abs(rlon-nlon)
    tolerance=0.011
    coordinate_ok=dlat<=tolerance and dlon<=tolerance
    return {
        'verified':bool(native_ok and coordinate_ok),
        'method':'direct_eps_native_grid_identity_plus_dwd_regular_grid_coordinate_parity_v2',
        'eps_native_grid_identity_verified':bool(native_ok),
        'eps_native_grid_identity':native_identity,
        'eps_native_source_url':eps_url,
        'dwd_regular_grid_coordinate_parity_verified':bool(coordinate_ok),
        'dwd_regular_source_url':regular_url,
        'dwd_regular_grid_point':dwd_regular_point,
        'open_meteo_returned_coordinate':{'latitude':rlat,'longitude':rlon},
        'absolute_difference_degrees':{'latitude':round(dlat,6),'longitude':round(dlon,6)},
        'tolerance_degrees_each_axis':tolerance,
        'valid_time_utc':valid.isoformat(),
        'native_coordinate_parity_claimed':False,
        'native_coordinate_parity_limitation':'DWD ICON-D2-EPS Open Data is native triangular grid and requires the external DWD grid-definition file for direct native-point localization; no native-coordinate equality is claimed.',
    }


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
    params=['u_10m','v_10m','vmax_10m']
    with tempfile.TemporaryDirectory() as td:
        for lead in leads:
            vals={}; urls=[]; point=None
            for param in params:
                try:
                    u=find_dwd_file(model,cycle,lead,param); urls.append(u)
                    r=S.get(u,timeout=90); r.raise_for_status(); p=Path(td)/f'eu_{param}_{lead}.grib2'; p.write_bytes(bz2.decompress(r.content))
                    assert_grib_valid_time(p,base,base+timedelta(hours=lead),f'ICON-EU {param} lead {lead}')
                    rows=nearest(p); point=point or rows.point
                    vals[param]=[{'stepRange':s,'value':v} for _,s,v in rows]
                except Exception as e: vals[param]={'error':f'{type(e).__name__}: {e}'}
            one=lambda p: vals[p][0]['value'] if isinstance(vals.get(p),list) and vals[p] else None
            rec={'model':'ICON-EU','run_time_utc':base.isoformat(),'forecast_lead_hours':lead,'valid_time_utc':(base+timedelta(hours=lead)).isoformat(),'source':'DWD Open Data raw GRIB2','source_urls':urls,'values':vals,'cycle_selection':selection,'forecast_coordinate_or_grid_point':point}
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

def _explicit_utc_times(times):
    out=[]
    for value in times:
        x=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        x=x.astimezone(timezone.utc) if x.tzinfo else x.replace(tzinfo=timezone.utc)
        if x.minute or x.second or x.microsecond:
            raise RuntimeError(f'ICON-D2-EPS non-hourly timestamp: {value}')
        out.append(x.isoformat())
    if out!=sorted(out) or len(out)!=len(set(out)):
        raise RuntimeError('ICON-D2-EPS hourly time axis is unordered or contains duplicates')
    return out


def _hourly_source(payload,r,meta_before,meta_after,identity,base,response_retrieved,speed,direction,gust,precip,cape):
    if payload.get('utc_offset_seconds')!=0:
        raise RuntimeError(f'ICON-D2-EPS source must be UTC; utc_offset_seconds={payload.get("utc_offset_seconds")}')
    units=payload.get('hourly_units') or {}
    if units.get('wind_speed_10m')!='m/s' or units.get('wind_gusts_10m')!='m/s':
        raise RuntimeError(f'ICON-D2-EPS unexpected wind units: {units}')
    lat=payload.get('latitude');lon=payload.get('longitude')
    if not isinstance(lat,(int,float)) or not isinstance(lon,(int,float)):
        raise RuntimeError('ICON-D2-EPS returned coordinate missing')
    if abs(float(lat)-LAT)>.05 or abs(float(lon)-LON)>.08:
        raise RuntimeError(f'ICON-D2-EPS returned coordinate implausible: {(lat,lon)}')
    times=_explicit_utc_times((payload.get('hourly') or {}).get('time') or [])
    columns={
        'wind_speed_10m':speed,'wind_gusts_10m':gust,'wind_direction_10m':direction,
        'precipitation':precip,'cape':cape,
    }
    if not times:
        raise RuntimeError('ICON-D2-EPS hourly time axis empty')
    for field,members in columns.items():
        for member,values in members.items():
            if len(values)!=len(times):
                raise RuntimeError(
                    f'ICON-D2-EPS hourly length mismatch field={field} member={member}: '
                    f'{len(values)} != {len(times)}')
    time_index={t:i for i,t in enumerate(times)}
    expected_ids=list(range(EPS_EXPECTED_MEMBERS))
    for hour in range(0,49):
        key=(base+timedelta(hours=hour)).isoformat()
        i=time_index.get(key)
        if i is None:
            raise RuntimeError(f'ICON-D2-EPS v15 hourly timestamp missing: {key}')
        for field,members in (('wind_speed_10m',speed),('wind_direction_10m',direction),('wind_gusts_10m',gust)):
            if sorted(members)!=expected_ids:
                raise RuntimeError(f'ICON-D2-EPS v15 member set mismatch for {field}: {sorted(members)}')
            for member in expected_ids:
                value=members[member][i]
                if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(float(value)):
                    raise RuntimeError(
                        f'ICON-D2-EPS v15 non-finite {field} member={member} time={key}: {value}')
                if field!='wind_direction_10m' and float(value)<0:
                    raise RuntimeError(
                        f'ICON-D2-EPS v15 negative {field} member={member} time={key}: {value}')
    response_hash=hashlib.sha256(
        json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    ).hexdigest()
    return {
        'schema_version':1,
        'method_version':'hourly-member-ecc-window-v15',
        'model':'dwd_icon_d2_eps',
        'source_class':'Open-Meteo named-model ensemble extraction',
        'source_url':r.url,
        'retrieved_at_utc':response_retrieved.isoformat(),
        'response_sha256':response_hash,
        'requested_coordinate':{'latitude':LAT,'longitude':LON},
        'returned_coordinate':{'latitude':float(lat),'longitude':float(lon)},
        'provider_metadata_before':meta_before,
        'provider_metadata_after':meta_after,
        'cycle_evidence':'stable_provider_metadata_association',
        'response_bound_run_identity_verified':False,
        'response_run_binding':identity['response_run_binding'],
        'spatial_provenance_verified':bool(identity['spatial_provenance'].get('verified')),
        'spatial_provenance_evidence':identity['spatial_provenance'],
        'dwd_eps_native_grid_identity_verified':bool(identity['spatial_provenance'].get('eps_native_grid_identity_verified')),
        'dwd_regular_grid_coordinate_parity_verified':bool(identity['spatial_provenance'].get('dwd_regular_grid_coordinate_parity_verified')),
        'native_grid_parity_verified':False,
        'native_grid_parity_limitation':identity['spatial_provenance'].get('native_coordinate_parity_limitation'),
        'run_time_utc':base.isoformat(),
        'times_utc':times,
        'columns':columns,
        'member_identity':'provider_member_number_in_one_response_zero_is_ensemble_member_not_control',
        'dwd_cycle_confirmation_url':identity['dwd_cycle_confirmation_url'],
        'source_run_identity':identity,
        'semantics':{
            'wind_speed_10m':'instantaneous',
            'wind_gusts_10m':'preceding_hour_max',
            'precipitation':'preceding_hour_sum',
            'cape':'instantaneous_not_thunder_probability',
        },
    }


def fetch_icon_d2_eps_bundle(leads):
    """Fetch one stable ICON-D2-EPS response and retain both 3-hour records and v15 hourly trajectories."""
    meta_before=fetch_eps_metadata()
    base=_meta_dt(meta_before,'last_run_initialisation_time_utc')
    availability=_meta_dt(meta_before,'last_run_availability_time_utc')
    request_started=datetime.now(timezone.utc)
    settle_age=(request_started-availability).total_seconds()
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
    response_retrieved=datetime.now(timezone.utc)
    hourly=payload.get('hourly') or {};times=hourly.get('time') or []
    speed=member_map(hourly,'wind_speed_10m');direction=member_map(hourly,'wind_direction_10m')
    gust=member_map(hourly,'wind_gusts_10m');precip=member_map(hourly,'precipitation');cape=member_map(hourly,'cape')
    member_ids=sorted(set(speed)&set(direction)&set(gust))
    expected_ids=list(range(EPS_EXPECTED_MEMBERS))
    if member_ids!=expected_ids:
        raise RuntimeError(
            f'ICON-D2-EPS wind-core member identity mismatch: expected={expected_ids} received={member_ids}; '
            f'speed={sorted(speed)} direction={sorted(direction)} gust={sorted(gust)}')

    meta_after=fetch_eps_metadata()
    stable_keys=(
        'last_run_initialisation_time_utc',
        'last_run_availability_time_utc',
        'last_run_modification_time_utc',
    )
    changed={k:{'before':meta_before.get(k),'after':meta_after.get(k)}
             for k in stable_keys if meta_before.get(k)!=meta_after.get(k)}
    if changed:
        raise RuntimeError(f'Open-Meteo ICON-D2-EPS run metadata changed during acquisition: {changed}')

    returned={'latitude':float(payload.get('latitude')),'longitude':float(payload.get('longitude'))}
    dwd_regular_confirmation=find_dwd_regular_file('icon-d2',cycle,farthest,'u_10m')
    spatial_provenance=verify_dwd_spatial_provenance(
        dwd_confirmation,dwd_regular_confirmation,base,base+timedelta(hours=farthest),returned)
    if not spatial_provenance['verified']:
        raise RuntimeError(f'ICON-D2-EPS spatial provenance verification failed: {spatial_provenance}')

    response_hash=hashlib.sha256(
        json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    ).hexdigest()
    response_run_binding={
        'status':'strong_indirect_bracketed_not_provider_embedded',
        'provider_response_embeds_run_time':False,
        'response_sha256':response_hash,
        'metadata_run_time_before_utc':meta_before['last_run_initialisation_time_utc'],
        'metadata_run_time_after_utc':meta_after['last_run_initialisation_time_utc'],
        'metadata_stable_across_response':True,
        'direct_dwd_cycle_confirmation_url':dwd_confirmation,
        'limitation':'The live Ensemble API response schema does not embed the initialization time; operational promotion remains blocked until provider-bound run identity is available.',
    }

    identity={
        'verification_status':'verified_stable_metadata_dwd_cycle_and_spatial_provenance',
        'model_id':'dwd_icon_d2_eps',
        'run_time_utc':base.isoformat(),
        'metadata_before':meta_before,
        'metadata_after':meta_after,
        'settling_age_seconds_at_request':round(settle_age,1),
        'minimum_settling_seconds':EPS_SETTLING_SECONDS,
        'dwd_cycle_confirmation_url':dwd_confirmation,
        'spatial_provenance':spatial_provenance,
        'response_run_binding':response_run_binding,
        'expected_member_ids':expected_ids,
        'source_timestamp_semantics':'Open-Meteo last_run_initialisation_time is the model reference/initialisation time',
    }
    hourly_source=_hourly_source(
        payload,r,meta_before,meta_after,identity,base,response_retrieved,
        speed,direction,gust,precip,cape)

    index={t:i for i,t in enumerate(times)};out=[]
    for lead in leads:
        valid=base+timedelta(hours=lead);key=valid.strftime('%Y-%m-%dT%H:%M');i=index.get(key);members=[]
        rec_error=None
        if i is None:
            rec_error=f'valid timestamp {key} absent from Open-Meteo hourly time axis'
        else:
            for m in member_ids:
                if (i>=len(speed[m]) or i>=len(direction[m]) or i>=len(gust[m])
                        or speed[m][i] is None or direction[m][i] is None or gust[m][i] is None):
                    continue
                g=gust[m][i]
                d=derived_from_speed(float(speed[m][i]),float(direction[m][i]),float(g));d['member']=m
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
             'source_url':r.url,'source_run_identity':identity,
             'forecast_coordinate_or_grid_point':hourly_source['returned_coordinate'],
             'members':members,'ensemble_statistics':stats}
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
    return out,hourly_source


def fetch_icon_d2_eps(leads):
    return fetch_icon_d2_eps_bundle(leads)[0]

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
        try:
            if name=='ICON-D2-EPS':
                rows,hourly_source=fetch_icon_d2_eps_bundle(leads)
                data['models'][name]=rows
                data['ensemble_hourly_source']=hourly_source
            else:
                data['models'][name]=fn(leads)
        except Exception as e:
            data['models'][name]=[]
            if name=='ICON-D2-EPS':data.pop('ensemble_hourly_source',None)
            errors.append(f'{name}: {type(e).__name__}: {e}')
        good=sum('derived' in x for x in data['models'][name]);complete=(len(data['models'][name])==len(leads) and good==len(leads));data['quality'][name]={'records':len(data['models'][name]),'derived_records':good,'success':complete,'complete_requested_horizon':complete}
    data['quality']['successful_models']=[n for n,q in data['quality'].items() if isinstance(q,dict) and q.get('success')];data['quality'].setdefault('errors',[]);data['quality']['errors']+=errors;data['dwd_additional_retrieved_at_utc']=datetime.now(timezone.utc).isoformat()
    data['retrieved_at_utc']=data['dwd_additional_retrieved_at_utc']
    p.write_text(json.dumps(data,separators=(',',':'))+'\n',encoding='utf-8')
    print(json.dumps({'ICON-EU':data['quality']['ICON-EU'],'ICON-D2-EPS':data['quality']['ICON-D2-EPS'],'errors':errors,'output_bytes':p.stat().st_size},indent=2))
    if not data['quality']['ICON-EU']['success'] or not data['quality']['ICON-D2-EPS']['success']:raise SystemExit(2)

if __name__=='__main__':main()
