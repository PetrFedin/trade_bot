"""Crash-recoverable, fail-closed checkpointing for ASTRA *research audit*.

This does not resume trading or the strategy backtester. It checkpoints
independent verification of already generated 36-model historical evidence.
Completed model audits can be skipped only after digest/source/code validation.
The final receipt is admitted only after full independent recomputation.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from tools.publish_research_receipt import ReceiptPublicationError, publish_new_json_receipt
from tools.verify_funded_portfolio_money import audit_portfolio_model
from tools.verify_funded_replay_integration import (
    MAX_RECEIPT_BYTES,
    MAX_REPORT_BYTES,
    HistoricalMoneyIntegrationError,
    _read,
    audit_real_historical_money,
)

SCHEMA = "astra-research-money-audit-checkpoint-v1"
CODE_PATHS = (
    "tools/verify_funded_portfolio_money.py",
    "tools/verify_funded_position_lifecycle.py",
    "tools/verify_funded_replay_integration.py",
    "tools/recover_funded_money_audit.py",
)


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                   allow_nan=False).encode("utf-8")
    ).hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".astra-audit-", suffix=".json", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _exclusive_checkpoint_lock(checkpoint_path: Path):
    """Serialize verification writers; never trust a symlink or stale owner lock.

    A kernel flock is automatically released if the process terminates. File
    modes and inode identity are checked before operating on checkpoint data.
    """
    if any(part.is_symlink() for part in (checkpoint_path.parent, *checkpoint_path.parent.parents)):
        raise HistoricalMoneyIntegrationError("symlinked checkpoint parent forbidden")
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = checkpoint_path.with_name(checkpoint_path.name + ".lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise HistoricalMoneyIntegrationError("checkpoint lock unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise HistoricalMoneyIntegrationError("unsafe checkpoint lock inode")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HistoricalMoneyIntegrationError("checkpoint already being audited") from exc
        target_info = os.stat(lock_path, follow_symlinks=False)
        if (target_info.st_ino, target_info.st_dev) != (info.st_ino, info.st_dev):
            raise HistoricalMoneyIntegrationError("checkpoint lock changed while acquiring")
        yield
    finally:
        os.close(fd)


def _audit_with_recovery_unlocked(
    *, report: dict, verifier: dict, report_bytes: bytes,
    verifier_bytes: bytes, historical_head_sha: str,
    checkpoint_path: Path, code_root: Path,
    interrupt_after: int | None = None,
) -> dict:
    """Replay or resume deterministic audits; always independently revalidate."""
    if checkpoint_path.is_symlink():
        raise HistoricalMoneyIntegrationError("symlinked audit checkpoint forbidden")
    code_fingerprints = {}
    for name in CODE_PATHS:
        path = code_root / name
        if not path.is_file() or path.is_symlink():
            raise HistoricalMoneyIntegrationError("missing or symlinked auditor code")
        code_fingerprints[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    identity = {
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "verifier_sha256": hashlib.sha256(verifier_bytes).hexdigest(),
        "historical_head_sha": historical_head_sha,
        "code_sha256": _digest(code_fingerprints),
    }
    if checkpoint_path.exists():
        state, _ = _read(checkpoint_path, MAX_RECEIPT_BYTES)
        if (
            state.get("schema_version") != SCHEMA
            or state.get("identity") != identity
            or state.get("checkpoint_sha256") != _digest(
                {k: v for k, v in state.items() if k != "checkpoint_sha256"}
            )
            or not isinstance(state.get("models"), dict)
        ):
            raise HistoricalMoneyIntegrationError("corrupt or mismatched audit checkpoint")
        completed = state["models"]
    else:
        completed = {}
    encountered = set()
    audited_count = 0

    def on_model(key: str, model: dict, parameters: dict) -> dict:
        nonlocal audited_count
        if key in encountered:
            raise HistoricalMoneyIntegrationError("repeated historical model key")
        encountered.add(key)
        model_identity = _digest({"model": model, "parameters": parameters})
        previous = completed.get(key)
        if previous is not None:
            if (
                not isinstance(previous, dict)
                or previous.get("model_sha256") != model_identity
                or previous.get("audit_sha256") != _digest(previous.get("result"))
                or not isinstance(previous.get("result"), dict)
            ):
                raise HistoricalMoneyIntegrationError("checkpoint model tamper or input drift")
            return previous["result"]
        result = audit_portfolio_model(model, **parameters)
        if type(result) is not dict:
            raise HistoricalMoneyIntegrationError("invalid model audit output")
        completed[key] = {
            "model_sha256": model_identity,
            "audit_sha256": _digest(result),
            "result": result,
        }
        stage = {"schema_version": SCHEMA, "identity": identity, "models": completed}
        stage["checkpoint_sha256"] = _digest(stage)
        _atomic_json(checkpoint_path, stage)
        audited_count += 1
        if interrupt_after is not None and audited_count >= interrupt_after:
            raise InterruptedError("deliberate test interruption after committed model")
        return result

    first = audit_real_historical_money(
        report, verifier, report_bytes=report_bytes,
        verifier_bytes=verifier_bytes, historical_head_sha=historical_head_sha,
        model_auditor=on_model,
    )
    if set(completed) != encountered or len(completed) != 36:
        raise HistoricalMoneyIntegrationError("incomplete/extra checkpoint model identities")
    # Recompute all 36 without checkpoint to ensure reused summaries never
    # replace the independent actual numerical checks.
    independent = audit_real_historical_money(
        report, verifier, report_bytes=report_bytes,
        verifier_bytes=verifier_bytes, historical_head_sha=historical_head_sha,
    )
    if first != independent:
        raise HistoricalMoneyIntegrationError("resumed audit differs from full clean replay")
    return {
        "schema_version": "astra-resumed-research-money-receipt-v1",
        "completed_models": len(completed),
        "checkpoint_identity_sha256": _digest(identity),
        "money_audit_sha256": independent["money_audit_sha256"],
        "clean_replay_identical": True,
        "live_trading_allowed": False,
        "profitability_proven": False,
    }



def audit_with_recovery(
    *,
    report: dict, verifier: dict, report_bytes: bytes,
    verifier_bytes: bytes, historical_head_sha: str,
    checkpoint_path: Path, code_root: Path,
    interrupt_after: int | None = None,
) -> dict:
    """Exclusively hold the checkpoint until full independent revalidation."""
    with _exclusive_checkpoint_lock(Path(checkpoint_path)):
        return _audit_with_recovery_unlocked(
            report=report, verifier=verifier, report_bytes=report_bytes,
            verifier_bytes=verifier_bytes, historical_head_sha=historical_head_sha,
            checkpoint_path=checkpoint_path, code_root=code_root,
            interrupt_after=interrupt_after,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--independent-verifier", type=Path, required=True)
    parser.add_argument("--historical-head-sha", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report, report_bytes = _read(args.report, MAX_REPORT_BYTES)
    verifier, verifier_bytes = _read(args.independent_verifier, MAX_RECEIPT_BYTES)
    result = audit_with_recovery(
        report=report, verifier=verifier, report_bytes=report_bytes,
        verifier_bytes=verifier_bytes, historical_head_sha=args.historical_head_sha,
        checkpoint_path=args.checkpoint, code_root=Path(__file__).resolve().parent.parent,
    )
    try:
        publish_new_json_receipt(args.output, result)
    except ReceiptPublicationError as exc:
        raise HistoricalMoneyIntegrationError(
            f"immutable recovery receipt refused: {exc}"
        ) from exc
    print("ASTRA_RECOVERED_MONEY_AUDIT", result["money_audit_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
