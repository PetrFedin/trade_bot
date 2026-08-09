from __future__ import annotations

from pathlib import Path

from tools.verify_lock_freshness import compare_locks


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def write_lock(path: Path, blocks: list[tuple[str, str, tuple[str, ...]]], *, comment: str = "") -> None:
    lines: list[str] = []
    if comment:
        lines.append(f"# {comment}")
    for name, version, hashes in blocks:
        lines.append(f"{name}=={version} \\")
        for index, digest in enumerate(hashes):
            suffix = " \\" if index < len(hashes) - 1 else ""
            lines.append(f"    --hash=sha256:{digest}{suffix}")
        lines.append(f"    # via {comment or 'test'}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_comments_and_package_name_normalization_do_not_make_lock_stale(tmp_path: Path) -> None:
    expected = tmp_path / "expected.lock"
    generated = tmp_path / "generated.lock"
    write_lock(expected, [("demo_pkg", "1.0", (HASH_A, HASH_B))], comment="old comment")
    write_lock(generated, [("demo-pkg", "1.0", (HASH_B, HASH_A))], comment="new comment")
    assert compare_locks(expected, generated) == ()


def test_version_change_is_reported(tmp_path: Path) -> None:
    expected = tmp_path / "expected.lock"
    generated = tmp_path / "generated.lock"
    write_lock(expected, [("demo", "1.0", (HASH_A,))])
    write_lock(generated, [("demo", "1.1", (HASH_A,))])
    differences = compare_locks(expected, generated)
    assert len(differences) == 1
    assert differences[0].package == "demo"
    assert differences[0].expected_version == "1.0"
    assert differences[0].generated_version == "1.1"


def test_hash_change_is_reported(tmp_path: Path) -> None:
    expected = tmp_path / "expected.lock"
    generated = tmp_path / "generated.lock"
    write_lock(expected, [("demo", "1.0", (HASH_A,))])
    write_lock(generated, [("demo", "1.0", (HASH_C,))])
    differences = compare_locks(expected, generated)
    assert len(differences) == 1
    assert differences[0].expected_hashes == (HASH_A,)
    assert differences[0].generated_hashes == (HASH_C,)


def test_added_and_removed_packages_are_reported(tmp_path: Path) -> None:
    expected = tmp_path / "expected.lock"
    generated = tmp_path / "generated.lock"
    write_lock(expected, [("alpha", "1.0", (HASH_A,))])
    write_lock(generated, [("beta", "2.0", (HASH_B,))])
    differences = compare_locks(expected, generated)
    assert [difference.package for difference in differences] == ["alpha", "beta"]
