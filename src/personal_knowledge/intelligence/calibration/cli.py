"""Read-only calibration product CLI."""
from __future__ import annotations
import argparse
from personal_knowledge.intelligence.analysis.schema import canonical_json
from .service import acceptance_report,explain

def main(argv=None)->int:
    p=argparse.ArgumentParser(); p.add_argument("--db",required=True); sub=p.add_subparsers(dest="cmd",required=True)
    e=sub.add_parser("explain"); e.add_argument("protocol_id")
    a=sub.add_parser("acceptance"); a.add_argument("protocol_id"); a.add_argument("--pilot-db",required=True); a.add_argument("--personal-db",required=True); a.add_argument("--external-db",required=True); a.add_argument("--analysis-db",required=True); a.add_argument("--metadata-only",action="store_true")
    x=p.parse_args(argv)
    try:
        if x.cmd=="explain": result=explain(x.db,x.protocol_id)
        else:
            if not x.metadata_only: raise ValueError("metadata_only_required")
            result=acceptance_report(db_path=x.db,protocol_id=x.protocol_id,source_paths={"pilot":x.pilot_db,"personal":x.personal_db,"external":x.external_db,"analysis":x.analysis_db})
    except Exception as exc:
        print(canonical_json({"ok":False,"error":str(exc)})); return 1
    print(canonical_json(result)); return 0 if result.get("ok",True) else 1

if __name__=="__main__": raise SystemExit(main())
