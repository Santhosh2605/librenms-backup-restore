"""End-to-end archive pipeline: tar -> compressor -> encryption -> file.

These are the highest-value tests in the suite: they exercise the real
production path used by every backup, including the subprocess plumbing that
previously deadlocked (compressor stdout must be drained concurrently) and
the streaming reader that must reject hostile member names.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile

import pytest

from br import archive, checksums, verify
from br.archive import ArchiveReader, ArchiveWriter
from br.common import Logger, IntegrityError
from br.writer import ArchiveBuild, ArchiveStream


def _logger(tmp_path):
    return Logger(os.path.join(str(tmp_path), "test.log"))


def _payload_bytes(size=4096):
    # Recognisable, compressible, and includes every byte value.
    return (bytes(range(256)) * (size // 256 + 1))[:size]


# -- raw tar layer ---------------------------------------------------------

def test_archive_writer_records_hashes():
    buf = io.BytesIO()
    data = _payload_bytes()
    with ArchiveWriter(buf) as writer:
        writer.add_bytes("database/librenms.sql.gz", data)
    digest = hashlib.sha256(data).hexdigest()
    assert writer.hashes["database/librenms.sql.gz"] == digest
    assert writer.files == 1


def test_archive_writer_prefixes_root():
    buf = io.BytesIO()
    with ArchiveWriter(buf) as writer:
        writer.add_bytes("a.txt", b"x")
    buf.seek(0)
    with ArchiveReader(buf) as reader:
        rels = [reader.relname(i) for i in reader.members()]
    assert rels == ["a.txt"]
    assert writer.files == 1


@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "/etc/passwd",
    "librenms-backup/../../escape",
])
def test_archive_writer_refuses_traversal(bad):
    buf = io.BytesIO()
    with ArchiveWriter(buf) as writer:
        with pytest.raises((ValueError, IntegrityError, Exception)):
            writer.add_bytes(bad, b"x")


def test_archive_reader_rejects_foreign_root():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("not-our-root/file.txt")
        blob = b"x"
        info.size = len(blob)
        tar.addfile(info, io.BytesIO(blob))
    buf.seek(0)
    with ArchiveReader(buf) as reader:
        with pytest.raises(IntegrityError, match="unexpected archive root"):
            list(reader.members())


def test_archive_reader_rejects_escaping_symlink():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("librenms-backup/evil")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../etc/shadow"
        tar.addfile(info)
    buf.seek(0)
    with ArchiveReader(buf) as reader:
        with pytest.raises(IntegrityError, match="symlink escapes root"):
            list(reader.members())


# -- full pipeline round-trip ----------------------------------------------

def test_full_pipeline_roundtrip(br_home, tmp_path):
    dest = str(tmp_path / "roundtrip.brarchive")
    log = _logger(tmp_path)
    members = {
        "database/librenms.sql.gz": b"-- dump\n" * 500,
        "application/config.php": b"<?php $config['x']=1;\n",
        "rrd/" + "x" * 90 + "/data.rrd": b"RRD" + b"\x00" * 64,
    }

    with ArchiveBuild(dest, br_home, log) as writer:
        for rel, data in members.items():
            writer.add_bytes(rel, data)
        writer.add_json("manifest.json", {"status": "complete"})

    assert os.path.getsize(dest) > 0

    with ArchiveStream(dest, br_home, log) as stream:
        with ArchiveReader(stream) as reader:
            recovered = {}
            for info in reader.members():
                if info.isfile():
                    recovered[reader.relname(info)] = reader.read_member(info)

    for rel, data in members.items():
        assert recovered[rel] == data, rel
    assert json.loads(recovered["manifest.json"])["status"] == "complete"


def test_gzip_compression_roundtrip(make_cfg, tmp_path):
    cfg = make_cfg(compression="gzip")
    dest = str(tmp_path / "gz.brarchive")
    log = _logger(tmp_path)
    with ArchiveBuild(dest, cfg, log) as writer:
        writer.add_bytes("a.txt", b"hello\n" * 100)
    with ArchiveStream(dest, cfg, log) as stream:
        with ArchiveReader(stream) as reader:
            got = {reader.relname(i): reader.read_member(i) for i in reader.members()}
    assert got["a.txt"] == b"hello\n" * 100


def test_pipeline_handles_multi_megabyte_member(make_cfg, tmp_path):
    """Regression guard: the compressor pipe must be drained concurrently,
    otherwise a member larger than the pipe buffer deadlocks the writer."""
    cfg = make_cfg(compression="zstd")
    dest = str(tmp_path / "big.brarchive")
    log = _logger(tmp_path)
    blob = os.urandom(6 * 1024 * 1024)

    with ArchiveBuild(dest, cfg, log) as writer:
        writer.add_bytes("database/big.sql.gz", blob)

    with ArchiveStream(dest, cfg, log) as stream:
        with ArchiveReader(stream) as reader:
            for info in reader.members():
                if reader.relname(info) == "database/big.sql.gz":
                    assert reader.read_member(info) == blob
                    break
            else:  # pragma: no cover
                pytest.fail("member not found")


def test_encrypted_pipeline_roundtrip(make_cfg, age_identity, tmp_path):
    cfg = make_cfg(encryption="age")
    cfg._cp.set("archive", "age_identity_file", age_identity["identity"])
    cfg._cp.set("archive", "age_recipients_file",
                str(tmp_path / "keys" / "recipients.txt"))
    dest = str(tmp_path / "enc.brarchive")
    log = _logger(tmp_path)
    secret = b"top-secret-config\n" * 200

    with ArchiveBuild(dest, cfg, log) as writer:
        writer.add_bytes("application/.env", secret)

    raw = open(dest, "rb").read()
    assert b"top-secret-config" not in raw, "plaintext leaked into container"

    with ArchiveStream(dest, cfg, log) as stream:
        with ArchiveReader(stream) as reader:
            got = {reader.relname(i): reader.read_member(i) for i in reader.members()}
    assert got["application/.env"] == secret


# -- checksums inside the archive -----------------------------------------

def test_checksums_cover_archived_tree(br_home, tmp_path):
    src = tmp_path / "tree"
    (src / "sub").mkdir(parents=True)
    (src / "one.txt").write_text("one\n")
    (src / "sub" / "two.txt").write_text("two\n")

    sumfile = checksums.build(str(src))
    parsed = checksums.parse(sumfile)
    assert set(parsed) == {"one.txt", "sub/two.txt"}

    dest = str(tmp_path / "tree.brarchive")
    log = _logger(tmp_path)
    extracted = tmp_path / "out"
    with ArchiveBuild(dest, br_home, log) as writer:
        for rel in parsed:
            writer.add_bytes("application/" + rel, (src / rel).read_bytes())

    with ArchiveStream(dest, br_home, log) as stream:
        with ArchiveReader(stream) as reader:
            for info in reader.members():
                rel = reader.relname(info)
                if not info.isfile() or not rel.startswith("application/"):
                    continue
                target = extracted / rel[len("application/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(reader.read_member(info))

    (extracted / "checksums.sha256").write_text(
        "\n".join(f"{v}  {k}" for k, v in parsed.items()) + "\n"
    )
    result = checksums.verify_tree(str(extracted))
    assert result["ok"] is True


def test_truncated_archive_is_detected(br_home, tmp_path):
    dest = str(tmp_path / "trunc.brarchive")
    log = _logger(tmp_path)
    with ArchiveBuild(dest, br_home, log) as writer:
        writer.add_bytes("a.txt", b"payload\n" * 500)

    size = os.path.getsize(dest)
    with open(dest, "r+b") as fh:
        fh.truncate(size // 2)

    with pytest.raises(Exception):
        with ArchiveStream(dest, br_home, log) as stream:
            with ArchiveReader(stream) as reader:
                for info in reader.members():
                    if info.isfile():
                        reader.read_member(info)
