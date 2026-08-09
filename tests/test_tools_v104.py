from pathlib import Path
import json
from tools.architecture_audit_v104 import audit as architecture_audit
from tools.static_audit_v104 import audit as static_audit
from tools.platform_v104 import main
from app.runtime.worker_execution_plane_v104 import DeadLetterQueueV104, EvidenceSpoolV104, WorkerEventJournalV104


def test_architecture_audit_passes():
    result=architecture_audit(Path(__file__).resolve().parents[1]); assert result["status"]=="PASS", result


def test_static_audit_passes():
    result=static_audit(Path(__file__).resolve().parents[1]); assert result["status"]=="PASS", result


def test_platform_cli_empty_stores(tmp_path, capsys):
    assert main(["verify-journal",str(tmp_path/"journal")])==0
    assert json.loads(capsys.readouterr().out)["events"]==0
    assert main(["verify-spool",str(tmp_path/"spool")])==0
    assert json.loads(capsys.readouterr().out)["records"]==0
    assert main(["verify-dlq",str(tmp_path/"dlq")])==0
    assert json.loads(capsys.readouterr().out)["records"]==0
