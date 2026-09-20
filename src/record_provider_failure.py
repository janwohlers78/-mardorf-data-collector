#!/usr/bin/env python3
"""Record a provider process failure that occurred outside Python (e.g. timeout)."""
import argparse,json,os
from datetime import datetime,timezone
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument("--model",required=True)
p.add_argument("--stage",required=True)
p.add_argument("--exit-code",required=True,type=int)
p.add_argument("--mirror")
p.add_argument("--reason",required=True)
a=p.parse_args()
path=Path(os.getenv("COLLECTOR_MODEL_FILE","work/model_snapshot.json"))
if path.exists():
    d=json.loads(path.read_text(encoding="utf-8"))
else:
    d={"schema_version":2,"retrieved_at_utc":datetime.now(timezone.utc).isoformat(),"mode":"production","models":{},"quality":{"errors":[]},"provider_attempts":[]}
d.setdefault("provider_attempts",[]).append({
    "model":a.model,"stage":a.stage,"status":"failed_external",
    "completed_at_utc":datetime.now(timezone.utc).isoformat(),
    "exit_code":a.exit_code,"mirror":a.mirror,"reason":a.reason,
})
d["retrieved_at_utc"]=datetime.now(timezone.utc).isoformat()
path.parent.mkdir(parents=True,exist_ok=True)
path.write_text(json.dumps(d,separators=(",",":"))+"\n",encoding="utf-8")
print(json.dumps(d["provider_attempts"][-1],ensure_ascii=False))
