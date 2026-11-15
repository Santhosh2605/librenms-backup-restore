"""Checksum manifest generation and tree verification."""

from __future__ import annotations

import os

import pytest

from br import checksums
from br.common import CHECKSUM_NAME


def _tree(root):
    os.makedirs(os.path.join(root, "sub", "deep"), exist_ok=True)
    open(os.path.join(root, "a.txt"), "w").write("alpha\n")
    open(os.path.join(root, "sub", "b.txt"), "w").write("bravo\n")
    open(os.path.join(root, "sub", "deep", "c.bin"), "wb").write(b"\x00\x01\x02")
    os.symlink("a.txt", os.path.join(root, "link.txt"))
    return root


def test_build_hashes_every_regular_file(tmp_path):
    root = _tree(str(tmp_path / "tree"))
    out = checksums.build(root)
    assert out.endswith(CHECKSUM_NAME)

    parsed = checksums.parse(out)
    assert "a.txt" in parsed
    assert "sub/b.txt" in parsed
    assert "sub/deep/c.bin" in parsed
    assert "link.txt" not in parsed, "symlinks are recorded separately, not hashed"


def test_checksum_file_does_not_hash_itself(tmp_path):
    root = _tree(str(tmp_path / "tree"))
    checksums.build(root)
    parsed = checksums.parse(os.path.join(root, CHECKSUM_NAME))
    assert CHECKSUM_NAME not in parsed


def test_build_is_deterministic(tmp_path):
    root = _tree(str(tmp_path / "tree"))
    first = checksums.parse(checksums.build(root))
    second = checksums.parse(checksums.build(root))
    assert first == second


def test_verify_tree_detects_modification(tmp_path):
    root = _tree(str(tmp_path / "tree"))
    out = checksums.build(root)
    with open(os.path.join(root, "a.txt"), "w") as fh:
        fh.write("tampered\n")

    result = checksums.verify_tree(root, out)
    assert result["ok"] is False
    assert "a.txt" in result["mismatched"]


def test_verify_tree_detects_deletion(tmp_path):
    root = _tree(str(tmp_path / "tree"))
    out = checksums.build(root)
    os.unlink(os.path.join(root, "sub", "b.txt"))

    result = checksums.verify_tree(root, out)
    assert result["ok"] is False


def test_verify_tree_detects_addition(tmp_path):
    root = _tree(str(tmp_path / "tree"))
    out = checksums.build(root)
    open(os.path.join(root, "sneaky.txt"), "w").write("extra\n")

    result = checksums.verify_tree(root, out)
    assert result["unexpected_count"] == 1
    assert "sneaky.txt" in result["unexpected"]
    # Additions alone do not fail the tree: a restore may legitimately add
    # runtime files. They are surfaced for the operator to judge.
    assert result["ok"] is True


def test_clean_tree_verifies(tmp_path):
    root = _tree(str(tmp_path / "tree"))
    out = checksums.build(root)
    result = checksums.verify_tree(root, out)
    assert result["ok"] is True


def test_verify_member_accepts_known_hash():
    expected = {"x/y.txt": "abc123"}
    assert checksums.verify_member("x/y.txt", "abc123", expected) is True
    assert checksums.verify_member("x/y.txt", "wrong", expected) is False
    assert checksums.verify_member("absent.txt", "abc123", expected) is False


def test_parse_tolerates_blank_lines(tmp_path):
    path = tmp_path / "c.sha256"
    path.write_text("\n\n" + "a" * 64 + "  one.txt\n\n", encoding="utf-8")
    parsed = checksums.parse(str(path))
    assert parsed == {"one.txt": "a" * 64}
