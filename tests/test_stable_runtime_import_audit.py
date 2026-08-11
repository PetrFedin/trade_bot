from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.stable_runtime_import_audit import (
    VersionedRuntimeImport,
    build_report,
    compare_imports,
    load_baseline,
    scan_versioned_runtime_imports,
)


def test_scan_finds_versioned_runtime_imports_only_in_stable_files(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "execution").mkdir(parents=True)
    (app / "runtime").mkdir(parents=True)
    (app / "execution" / "stable.py").write_text(
        "from app.runtime.paper_broker_contract_v99 import PaperBrokerV99\n",
        encoding="utf-8",
    )
    (app / "runtime" / "compat_v100.py").write_text(
        "from app.runtime.paper_broker_contract_v99 import PaperBrokerV99\n",
        encoding="utf-8",
    )
    (app / "execution" / "other.py").write_text(
        "from app.runtime.paper_broker_contract import PaperBrokerV99\n",
        encoding="utf-8",
    )

    assert scan_versioned_runtime_imports(app) == (
        VersionedRuntimeImport(
            "execution/stable.py",
            "app.runtime.paper_broker_contract_v99",
        ),
    )


def test_scan_supports_plain_import_syntax(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "module.py").write_text(
        "import app.runtime.platform_common_v90\n",
        encoding="utf-8",
    )
    assert scan_versioned_runtime_imports(app) == (
        VersionedRuntimeImport("module.py", "app.runtime.platform_common_v90"),
    )


def test_exact_baseline_passes_and_drift_fails() -> None:
    first = VersionedRuntimeImport("a.py", "app.runtime.platform_common_v90")
    second = VersionedRuntimeImport("b.py", "app.runtime.paper_broker_contract_v99")
    assert compare_imports((first,), (first,)) == ((), ())
    assert compare_imports((first, second), (first,)) == ((second,), ())
    assert compare_imports((first,), (first, second)) == ((), (second,))
    assert build_report(observed=(first,), baseline=(first,))["status"] == "PASS"
    assert build_report(observed=(first, second), baseline=(first,))["status"] == "FAIL"


def test_baseline_rejects_invalid_and_duplicate_items(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "imports": [
                    {
                        "path": "a.py",
                        "module": "app.runtime.platform_common_v90",
                    },
                    {
                        "path": "a.py",
                        "module": "app.runtime.platform_common_v90",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicates"):
        load_baseline(path)

    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "imports": [
                    {"path": "a.py", "module": "app.runtime.platform_common"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="module is invalid"):
        load_baseline(path)
