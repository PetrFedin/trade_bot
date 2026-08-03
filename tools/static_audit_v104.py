from __future__ import annotations
import argparse,json
from pathlib import Path
from typing import Sequence
FORBIDDEN=("verify=False","verify = False","ssl.CERT_NONE","check_hostname = False","_create_unverified_context","https://api.alpaca.markets","wss://api.alpaca.markets","live_trading_allowed=True","external_order_routing_allowed=True","pickle.loads","yaml.load(","subprocess.Popen(")

def audit(root: Path):
    findings=[]; files=sorted((root/"app").rglob("*.py"))+sorted((root/"tools").rglob("*.py"))
    for path in files:
        if path.name.startswith("static_audit_v"): continue
        text=path.read_text()
        for token in FORBIDDEN:
            if token in text: findings.append(f"{path.relative_to(root)}:{token}")
    return {"schema":104,"status":"PASS" if not findings else "FAIL","python_files_checked":len(files),"findings":findings}

def main(argv: Sequence[str]|None=None):
    p=argparse.ArgumentParser();p.add_argument("root",nargs="?",type=Path,default=Path("."));a=p.parse_args(argv)
    result=audit(a.root.resolve());print(json.dumps(result,indent=2,sort_keys=True));return 0 if result["status"]=="PASS" else 2
if __name__=="__main__": raise SystemExit(main())
