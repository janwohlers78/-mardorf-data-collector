import hashlib
import json
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile

from audit_integrity import audit_models,audit_svg,audit_skm,audit_eps_hourly_source
from check_collection_due import evaluate_latest_success

POLICY=json.loads(Path("config/integrity_policy.json").read_text(encoding="utf-8"))
FAMILY_MODELS=("ICON-D2","GFS","ECMWF-IFS","GEFS-control","ICON-EU","ICON-D2-EPS")

class IntegrityAuditTests(unittest.TestCase):
    def model_bundle(self):
        now=datetime.now(timezone.utc)
        run=(now-timedelta(hours=1)).replace(minute=0,second=0,microsecond=0)
        models={}
        for model in FAMILY_MODELS:
            leads=[0,12,24,30,36,42,48] if model in ("ICON-D2","GFS") else [0,12,24,36,48]
            rows=[]
            for lead in leads:
                der={"wind_speed_ms":5.0}
                if model in POLICY["model_policy"]["required_gust_models"]:
                    der["gust_ms"]=7.0
                rec={
                    "model":model,"run_time_utc":run.isoformat(),
                    "forecast_lead_hours":lead,
                    "valid_time_utc":(run+timedelta(hours=lead)).isoformat(),
                    "derived":der,
                }
                if model=="ICON-D2-EPS":
                    ids=list(range(20))
                    rec["members"]=[{
                        "member":i,"wind_speed_ms":5.0,
                        "wind_direction_deg":270.0,"gust_ms":7.0
                    } for i in ids]
                    rec["ensemble_statistics"]={"member_count":20}
                    rec["derived"]["ensemble_member_count"]=20
                    meta={"last_run_initialisation_time_utc":run.isoformat(),
                          "last_run_availability_time_utc":(run-timedelta(minutes=20)).isoformat()}
                    rec["source_run_identity"]={
                        "verification_status":"verified_stable_metadata_and_dwd_cycle",
                        "run_time_utc":run.isoformat(),
                        "metadata_before":meta,"metadata_after":meta,
                        "settling_age_seconds_at_request":1200,
                        "expected_member_ids":ids,
                        "dwd_cycle_confirmation_url":"https://opendata.dwd.de/example"
                    }
                rows.append(rec)
            models[model]=rows
        return {
            "schema_version":1,"mode":"test","retrieved_at_utc":now.isoformat(),
            "spot":{"lat":52.4942,"lon":9.3418},
            "models":models,"quality":{"errors":[]}
        }


    def test_v15_hourly_source_requires_exact_hourly_20_member_core(self):
        run=datetime(2026,9,20,0,0,tzinfo=timezone.utc)
        times=[(run+timedelta(hours=h)).isoformat() for h in range(49)]
        columns={}
        for field,value in (("wind_speed_10m",5.0),("wind_direction_10m",270.0),("wind_gusts_10m",7.0)):
            columns[field]={str(m):[value]*49 for m in range(20)}
        source={
            "model":"dwd_icon_d2_eps",
            "cycle_evidence":"stable_provider_metadata_association",
            "run_time_utc":run.isoformat(),
            "retrieved_at_utc":(run+timedelta(hours=4)).isoformat(),
            "response_sha256":"abc",
            "times_utc":times,
            "columns":columns,
            "requested_coordinate":{"latitude":52.4942,"longitude":9.3418},
            "returned_coordinate":{"latitude":52.5,"longitude":9.34},
        }
        failures,summary=audit_eps_hourly_source(source,run,20)
        self.assertEqual(failures,[],failures)
        self.assertTrue(summary["required_run_through_48h_complete"])

        broken=json.loads(json.dumps(source))
        del broken["times_utc"][17]
        for members in broken["columns"].values():
            for values in members.values():del values[17]
        failures,_=audit_eps_hourly_source(broken,run,20)
        self.assertTrue(any(x["reason"]=="hourly_source_required_hours_missing" for x in failures),failures)

        broken=json.loads(json.dumps(source))
        del broken["columns"]["wind_gusts_10m"]["19"]
        failures,_=audit_eps_hourly_source(broken,run,20)
        self.assertTrue(any(x["reason"]=="hourly_source_member_identity_mismatch" for x in failures),failures)

    def test_complete_reduced_model_bundle_passes(self):
        now=datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"m.json";p.write_text(json.dumps(self.model_bundle()))
            r=audit_models(p,POLICY,now)
        self.assertEqual(r["error_count"],0,r["issues"])
        self.assertTrue(r["bundle_ready_for_private_revalidation"])

    def test_missing_model_lead_is_exact(self):
        now=datetime.now(timezone.utc);d=self.model_bundle()
        d["models"]["GFS"]=[x for x in d["models"]["GFS"] if x["forecast_lead_hours"]!=24]
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"m.json";p.write_text(json.dumps(d))
            r=audit_models(p,POLICY,now)
        xs=[x for x in r["issues"] if x["code"]=="EXPECTED_PROVIDER_LEADS_NOT_RECEIVED" and x["source"]=="GFS"]
        self.assertEqual(len(xs),1,r["issues"])
        self.assertEqual(xs[0]["details"]["missing_leads_hours"],[24])


    def test_short_icon_eu_cycle_excludes_out_of_horizon_placeholders(self):
        now=datetime.now(timezone.utc)
        run=(now-timedelta(hours=1)).replace(minute=0,second=0,microsecond=0)
        if run.hour in (0,6,12,18):
            run-=timedelta(hours=1)
        rows=[]
        for lead in POLICY["model_policy"]["project_desired_leads"]["ICON-EU"]:
            rec={
                "model":"ICON-EU","run_time_utc":run.isoformat(),
                "forecast_lead_hours":lead,
                "valid_time_utc":(run+timedelta(hours=lead)).isoformat(),
                "values":{}
            }
            if lead<=51:
                rec["derived"]={"wind_speed_ms":5.0,"gust_ms":7.0}
                if lead==33:
                    rec["values"]["tot_prec"]={"error":"file_not_published"}
            else:
                rec["values"]["u_10m"]={"error":"file_not_published"}
                rec["values"]["v_10m"]={"error":"file_not_published"}
                rec["values"]["vmax_10m"]={"error":"file_not_published"}
            rows.append(rec)
        d={"schema_version":1,"mode":"production","retrieved_at_utc":now.isoformat(),
           "spot":{"lat":52.4942,"lon":9.3418},
           "models":{"ICON-EU":rows},"quality":{"errors":[]}}
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"m.json";p.write_text(json.dumps(d))
            r=audit_models(p,POLICY,now)
        src=r["sources"]["ICON-EU"]
        self.assertTrue(src["provider_cycle_complete"],src)
        self.assertEqual(src["missing_expected_leads_hours"],[])
        self.assertEqual(src["provider_expected_max_horizon_hours"],51)
        icon_errors=[x for x in r["issues"] if x["source"]=="ICON-EU" and x["severity"]=="ERROR"]
        self.assertEqual(icon_errors,[],r["issues"])
        optional=[x for x in r["issues"] if x["code"]=="OPTIONAL_WEATHER_CONTEXT_FIELDS_UNAVAILABLE" and x["source"]=="ICON-EU"]
        self.assertEqual(len(optional),1,r["issues"])
        self.assertEqual(optional[0]["details"]["affected_fields"][0]["location"],"values.tot_prec")

    def test_eps_member_set_incomplete_is_exact_error(self):
        now=datetime.now(timezone.utc);d=self.model_bundle()
        rec=d["models"]["ICON-D2-EPS"][0]
        rec["members"]=rec["members"][:-1]
        rec["ensemble_statistics"]["member_count"]=19
        rec["derived"]["ensemble_member_count"]=19
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"m.json";p.write_text(json.dumps(d))
            r=audit_models(p,POLICY,now)
        xs=[x for x in r["issues"] if x["code"]=="ENSEMBLE_MEMBER_SET_INCOMPLETE"]
        self.assertEqual(len(xs),1,r["issues"])
        self.assertEqual(xs[0]["details"]["affected_leads"][0]["lead_hours"],0)

    def test_eps_unverified_run_identity_is_error(self):
        now=datetime.now(timezone.utc);d=self.model_bundle()
        for rec in d["models"]["ICON-D2-EPS"]:
            rec["source_run_identity"]["verification_status"]="unverified"
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"m.json";p.write_text(json.dumps(d))
            r=audit_models(p,POLICY,now)
        xs=[x for x in r["issues"] if x["code"]=="ENSEMBLE_RUN_IDENTITY_UNVERIFIED"]
        self.assertEqual(len(xs),1,r["issues"])

    def test_eps_member_missing_gust_is_hard_error(self):
        now=datetime.now(timezone.utc);d=self.model_bundle()
        del d["models"]["ICON-D2-EPS"][0]["members"][7]["gust_ms"]
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"m.json";p.write_text(json.dumps(d))
            r=audit_models(p,POLICY,now)
        xs=[x for x in r["issues"] if x["code"]=="ENSEMBLE_MEMBER_SET_INCOMPLETE"]
        self.assertEqual(len(xs),1,r["issues"])
        affected=xs[0]["details"]["affected_leads"][0]
        self.assertEqual(affected["lead_hours"],0)
        self.assertEqual(affected["incomplete_wind_core_members"][0]["member"],7)
        self.assertIn("gust_ms",affected["incomplete_wind_core_members"][0]["missing_or_invalid_fields"])

    def test_eps_identity_missing_on_one_lead_is_hard_error(self):
        now=datetime.now(timezone.utc);d=self.model_bundle()
        del d["models"]["ICON-D2-EPS"][0]["source_run_identity"]
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"m.json";p.write_text(json.dumps(d))
            r=audit_models(p,POLICY,now)
        xs=[x for x in r["issues"] if x["code"]=="ENSEMBLE_RUN_IDENTITY_UNVERIFIED"]
        self.assertEqual(len(xs),1,r["issues"])
        reasons=[x["reason"] for x in xs[0]["details"]["failures"]]
        self.assertIn("source_run_identity_missing_on_some_records",reasons)

    def test_model_spot_mismatch_is_hard_error(self):
        now=datetime.now(timezone.utc);d=self.model_bundle()
        d["spot"]["lat"]=53.0
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"m.json";p.write_text(json.dumps(d))
            r=audit_models(p,POLICY,now)
        self.assertTrue(any(x["code"]=="MODEL_SPOT_IDENTITY_MISMATCH" for x in r["issues"]))
        self.assertFalse(r["bundle_ready_for_private_revalidation"])

    def test_model_record_identity_mismatch_is_hard_error(self):
        now=datetime.now(timezone.utc);d=self.model_bundle()
        d["models"]["GFS"][0]["model"]="ECMWF-IFS"
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"m.json";p.write_text(json.dumps(d))
            r=audit_models(p,POLICY,now)
        self.assertTrue(any(x["code"]=="MODEL_RECORD_IDENTITY_MISMATCH" and x["source"]=="GFS" for x in r["issues"]))

    def test_model_audit_binds_exact_payload_hash(self):
        now=datetime.now(timezone.utc);d=self.model_bundle()
        raw=json.dumps(d).encode()
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"m.json";p.write_bytes(raw)
            r=audit_models(p,POLICY,now)
        self.assertEqual(r["input_payload_sha256"],hashlib.sha256(raw).hexdigest())
        self.assertEqual(r["input_payload_bytes"],len(raw))

    def test_invalid_json_still_records_exact_payload_hash(self):
        now=datetime.now(timezone.utc);raw=b"{not-json"
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"m.json";p.write_bytes(raw)
            r=audit_models(p,POLICY,now)
        self.assertTrue(any(x["code"]=="MODEL_BUNDLE_JSON_INVALID" for x in r["issues"]))
        self.assertEqual(r["input_payload_sha256"],hashlib.sha256(raw).hexdigest())
        self.assertEqual(r["input_payload_bytes"],len(raw))

    def test_failed_extension_is_not_masked_by_successful_base_stage(self):
        now=datetime.now(timezone.utc);d=self.model_bundle()
        d["provider_attempts"]=[
            {"model":"GFS","stage":"base","status":"success"},
            {"model":"GFS","stage":"extension","status":"failed","exception_type":"RuntimeError","exception_message":"boom"},
        ]
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"m.json";p.write_text(json.dumps(d))
            r=audit_models(p,POLICY,now)
        xs=[x for x in r["issues"] if x["code"]=="PROVIDER_STAGE_ATTEMPTS_FAILED" and x["source"]=="GFS"]
        self.assertEqual(len(xs),1,r["issues"])
        self.assertEqual(xs[0]["details"]["stage"],"extension")

    def test_due_check_future_success_fails_open(self):
        now=datetime(2026,9,20,8,0,tzinfo=timezone.utc)
        stamp=(now+timedelta(minutes=45)).isoformat()
        due,reason,age=evaluate_latest_success(stamp,now,150)
        self.assertTrue(due)
        self.assertEqual(reason,"latest_success_timestamp_future_fail_open")
        self.assertLess(age,-15)

    def test_due_check_small_clock_skew_can_still_skip(self):
        now=datetime(2026,9,20,8,0,tzinfo=timezone.utc)
        stamp=(now+timedelta(minutes=5)).isoformat()
        due,reason,age=evaluate_latest_success(stamp,now,150)
        self.assertFalse(due)
        self.assertEqual(reason,"last_success_within_threshold")
        self.assertEqual(age,0.0)

    def test_skm_stale_is_warning_not_primary_gate(self):
        now=datetime.now(timezone.utc)
        old=(now-timedelta(hours=5)).isoformat()
        d={
            "retrieved_at_utc":now.isoformat(),"provider":"MeteoMap.cloud",
            "source_timestamp_timezone":"UTC","role":"optional_legacy_north_shore_diagnostic_never_primary",
            "station":{"id":898},"requests":[
                {"kind":"wind","success":True,"http_status":200},
                {"kind":"gust","success":True,"http_status":200}],
            "endpoint_summary":{
                "wind":{"success":True,"last_time_utc":old,"observation_age_minutes":300},
                "gust":{"success":True,"last_time_utc":old,"observation_age_minutes":300}}
        }
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"s.json";p.write_text(json.dumps(d))
            r=audit_skm(p,POLICY,now)
        self.assertEqual(r["error_count"],0,r["issues"])
        self.assertGreaterEqual(r["warning_count"],2)
        self.assertTrue(r["bundle_ready_for_private_revalidation"])
        self.assertTrue(r["non_blocking"])

    def svg_bundle(self,gap=False):
        now=datetime.now(timezone.utc)
        times=[now-timedelta(minutes=5*i) for i in range(12)]
        times=sorted(times)
        if gap:times.pop(5)
        return {
            "retrieved_at_utc":now.isoformat(),
            "station":{"id":42374,"name":"SVG"},
            "requested_history_window":{"start_utc":(now-timedelta(minutes=60)).isoformat(),
                                        "end_utc":now.isoformat(),"hours":1},
            "request_diagnostics":{
                "current":{"success":True,"http_status":200,"request_path":"/current/42374"},
                "historic":{"success":True,"http_status":200,"request_path":"/historic/42374"},
                "stations":{"success":True,"http_status":200,"request_path":"/stations/42374"},
            },
            "latest_observation":{"time_utc":(now-timedelta(minutes=3)).isoformat()},
            "recent_historic_observations":[{"time_utc":t.isoformat()} for t in times],
        }

    def test_svg_station_mismatch_is_hard_error(self):
        now=datetime.now(timezone.utc);d=self.svg_bundle()
        d["station"]["id"]=999
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"s.json";p.write_text(json.dumps(d))
            r=audit_svg(p,POLICY,now)
        self.assertTrue(any(x["code"]=="SVG_STATION_ID_MISMATCH" for x in r["issues"]))
        self.assertFalse(r["bundle_ready_for_private_revalidation"])

    def test_svg_invalid_history_timestamp_is_hard_error(self):
        now=datetime.now(timezone.utc);d=self.svg_bundle()
        d["recent_historic_observations"][3]["time_utc"]="not-a-time"
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"s.json";p.write_text(json.dumps(d))
            r=audit_svg(p,POLICY,now)
        self.assertTrue(any(x["code"]=="SVG_HISTORIC_TIMESTAMP_INVALID" for x in r["issues"]))
        self.assertFalse(r["bundle_ready_for_private_revalidation"])

    def test_svg_out_of_window_history_timestamp_is_hard_error(self):
        now=datetime.now(timezone.utc);d=self.svg_bundle()
        d["recent_historic_observations"][0]["time_utc"]=(now-timedelta(hours=2)).isoformat()
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"s.json";p.write_text(json.dumps(d))
            r=audit_svg(p,POLICY,now)
        self.assertTrue(any(x["code"]=="SVG_HISTORIC_TIMESTAMP_OUTSIDE_REQUEST_WINDOW" for x in r["issues"]))
        self.assertFalse(r["bundle_ready_for_private_revalidation"])

    def test_skm_station_mismatch_is_hard_error_but_remains_nonblocking_source(self):
        now=datetime.now(timezone.utc);old=(now-timedelta(minutes=5)).isoformat()
        d={"retrieved_at_utc":now.isoformat(),"provider":"MeteoMap.cloud",
           "source_timestamp_timezone":"UTC","station":{"id":999},"requests":[],
           "endpoint_summary":{"wind":{"success":True,"last_time_utc":old},
                               "gust":{"success":True,"last_time_utc":old}}}
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"k.json";p.write_text(json.dumps(d))
            r=audit_skm(p,POLICY,now)
        self.assertTrue(any(x["code"]=="SKM_STATION_ID_MISMATCH" for x in r["issues"]))
        self.assertFalse(r["bundle_ready_for_private_revalidation"])
        self.assertTrue(r["non_blocking"])

    def test_svg_future_history_timestamp_is_hard_error(self):
        now=datetime.now(timezone.utc);d=self.svg_bundle()
        d["recent_historic_observations"][-1]["time_utc"]=(now+timedelta(minutes=30)).isoformat()
        d["requested_history_window"]["end_utc"]=(now+timedelta(hours=1)).isoformat()
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"s.json";p.write_text(json.dumps(d))
            r=audit_svg(p,POLICY,now)
        self.assertTrue(any(x["code"]=="SVG_HISTORIC_TIMESTAMP_TOO_FAR_IN_FUTURE" for x in r["issues"]))
        self.assertFalse(r["bundle_ready_for_private_revalidation"])

    def test_ecmwf_cycle_horizon_and_freshness_policy(self):
        from audit_integrity import provider_max
        self.assertEqual(provider_max("ECMWF-IFS",0,POLICY),120)
        self.assertEqual(provider_max("ECMWF-IFS",12,POLICY),120)
        self.assertEqual(provider_max("ECMWF-IFS",6,POLICY),90)
        self.assertEqual(provider_max("ECMWF-IFS",18,POLICY),90)
        self.assertEqual(POLICY["model_policy"]["maximum_run_age_hours"]["ECMWF-IFS"],14)

    def test_svg_exact_gap_is_reported(self):
        now=datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"s.json";p.write_text(json.dumps(self.svg_bundle(gap=True)))
            r=audit_svg(p,POLICY,now)
        xs=[x for x in r["issues"] if x["code"]=="SVG_HISTORIC_CADENCE_GAPS"]
        self.assertEqual(len(xs),1,r["issues"])
        self.assertEqual(xs[0]["details"]["total_estimated_missing_intervals"],1)

if __name__=="__main__":
    unittest.main()
