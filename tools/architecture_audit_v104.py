from __future__ import annotations
import argparse, json, re
from pathlib import Path
from typing import Sequence
REQUIRED=(
"app/runtime/worker_execution_plane_v104.py","app/runtime/postgres_worker_plane_v104.py",
"migrations/v104/001_production_worker_execution_plane.sql","tools/platform_v104.py","tools/static_audit_v104.py",
"tools/stress_v104.py","tests/test_worker_execution_plane_v104.py","tests/test_postgres_worker_plane_v104.py",
".github/workflows/schema104-production-worker-execution-plane.yml","ENGINEERING_REPORT_V104.md","OPERATOR_RUNBOOK_V104.md","LIVE_EXECUTION_STATUS_V104.json")

def audit(root: Path):
    findings=[]
    for item in REQUIRED:
        if not (root/item).is_file(): findings.append(f"missing:{item}")
    py=(root/"pyproject.toml").read_text()
    name=re.search(r'^name\s*=\s*"astra-schema(?P<n>\d+)[^"]*"$',py,re.M)
    version=re.search(r'^version\s*=\s*"(?P<a>\d+)\.(?P<b>\d+)\.(?P<c>\d+)"$',py,re.M)
    if not name or int(name.group("n"))<104: findings.append("package_identity")
    if not version or tuple(map(int,(version.group("a"),version.group("b"),version.group("c")))) < (7,34,0): findings.append("package_version")
    runtime=(root/"app/runtime/worker_execution_plane_v104.py").read_text()
    for token in ("SignedWorkClaimV104","WorkerAttestationV104","ReplayLedgerV104","EvidenceSpoolV104","ResumableUploaderV104","DeadLetterQueueV104","PAPER_REST_BASE","mutations are prohibited","external_order_routing_allowed","live_trading_allowed"):
        if token not in runtime: findings.append(f"runtime_boundary:{token}")
    sql=(root/"migrations/v104/001_production_worker_execution_plane.sql").read_text()
    for token in ("FOR UPDATE SKIP LOCKED","worker_event_append_only","worker_dead_letter_append_only","REVOKE ALL"):
        if token not in sql: findings.append(f"migration_boundary:{token}")
    return {"schema":104,"status":"PASS" if not findings else "FAIL","required_files":len(REQUIRED),"findings":findings}

def main(argv: Sequence[str]|None=None):
    p=argparse.ArgumentParser();p.add_argument("root",nargs="?",type=Path,default=Path("."));a=p.parse_args(argv)
    result=audit(a.root.resolve());print(json.dumps(result,indent=2,sort_keys=True));return 0 if result["status"]=="PASS" else 2
if __name__=="__main__": raise SystemExit(main())
