import json
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile

from audit_integrity import audit_models,audit_svg

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
                rows.append({
                    "model":model,"run_time_utc":run.isoformat(),
                    "forecast_lead_hours":lead,
                    "valid_time_utc":(run+timedelta(hours=lead)).isoformat(),
                    "derived":der,
                })
            models[model]=rows
        return {
            "schema_version":1,"mode":"test","retrieved_at_utc":now.isoformat(),
            "models":models,"quality":{"errors":[]}
        }

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

    def svg_bundle(self,gap=False):
        now=datetime.now(timezone.utc)
        times=[now-timedelta(minutes=5*i) for i in range(12)]
        times=sorted(times)
        if gap:times.pop(5)
        return {
            "retrieved_at_utc":now.isoformat(),
            "request_diagnostics":{
                "current":{"success":True,"http_status":200,"request_path":"/current/42374"},
                "historic":{"success":True,"http_status":200,"request_path":"/historic/42374"},
                "stations":{"success":True,"http_status":200,"request_path":"/stations/42374"},
            },
            "latest_observation":{"time_utc":(now-timedelta(minutes=3)).isoformat()},
            "recent_historic_observations":[{"time_utc":t.isoformat()} for t in times],
        }

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
