"""Archive production pipeline.

    tar members --> compressor subprocess --> EncryptedWriter --> .brarchive

Compression and encryption are separate stages so that:
  * the encryption layer only ever sees compressed bytes (fast),
  * verification can decompress without decrypting when testing structure,
  * a future S3 uploader can stream the finished file unchanged.

Compression runs as a subprocess (``zstd``/``gzip``) instead of a Python
library so memory stays flat on multi-GiB RRD trees; stdin/stdout are pipes
we own - no shell is involved.
"""

from __future__ import annotations

import os
import subprocess
import threading
import typing as t

from .archive import ArchiveWriter
from .common import BRException, Logger, which
from .crypto import EncryptedWriter


class _Compressor:
    """Compression subprocess: tar bytes in on stdin, compressed bytes out.

    stdout is a pipe that a pump thread drains into the encryption writer.
    Draining concurrently is mandatory - if we wrote to the pipe without
    reading it, a multi-GiB bundle would block once the 64 KiB pipe buffer
    filled.
    """

    def __init__(self, argv: list[str], sink, logger: Logger):
        self.logger = logger
        self._sink = sink
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0,
        )
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            while True:
                block = self._proc.stdout.read(262144)
                if not block:
                    break
                self._sink.write(block)
        except Exception as exc:  # pragma: no cover - surfaced by finish()
            self.logger.error("compression pump failed", error=str(exc))

    @property
    def stdin(self):
        return self._proc.stdin

    def finish(self) -> None:
        try:
            self._proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        self._thread.join(timeout=600)
        code = self._proc.wait()
        err = (self._proc.stderr.read() or b"")[:1500].decode("utf-8", "replace") if self._proc.stderr else ""
        if self._proc.stderr:
            self._proc.stderr.close()
        if code != 0:
            raise BRException(f"compression failed (exit {code}): {err}")


class _PipeToEncryption:
    """File-like object handed to ArchiveWriter."""

    def __init__(self, stdin):
        self._stdin = stdin

    def write(self, data: bytes) -> int:
        self._stdin.write(data)
        return len(data)


def compress_argv(cfg, out_path: str) -> list[str]:
    compression = str(cfg.get("archive", "compression", "zstd")).lower()
    if compression == "zstd":
        binary = which("zstd", ("/usr/bin/zstd",))
        level = cfg.get_int("archive", "zstd_level", 3)
        return [binary, "-q", f"-{level}", "--threads=0", "-c"]
    if compression == "gzip":
        binary = which("gzip", ("/usr/bin/gzip",))
        level = cfg.get_int("archive", "gzip_level", 6)
        return [binary, f"-{level}", "-c"]
    raise BRException(f"unsupported compression {compression!r}")


class ArchiveBuild:
    """Context manager: tar -> zstd/gzip -> encryption -> file."""

    def __init__(self, dest_path: str, cfg, logger: Logger):
        self.dest_path = dest_path
        self.cfg = cfg
        self.logger = logger
        self.writer: ArchiveWriter | None = None
        self._enc: EncryptedWriter | None = None
        self._comp: _Compressor | None = None
        self.compressed_bytes = 0

    def __enter__(self) -> ArchiveWriter:
        self._enc = EncryptedWriter(self.dest_path, self.cfg).__enter__()
        self._comp = _Compressor(compress_argv(self.cfg, self.dest_path),
                                 _CountingSink(self._enc, self), self.logger)
        self.writer = ArchiveWriter(_PipeToEncryption(self._comp.stdin)).__enter__()
        self.logger.info(
            "archive stream opened",
            file=os.path.basename(self.dest_path),
            compression=self.cfg.get("archive", "compression", "zstd"),
            encryption=self.cfg.encryption,
        )
        return self.writer

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if self.writer is not None:
                self.writer.__exit__(exc_type, exc, tb)
        finally:
            try:
                if self._comp is not None:
                    if exc_type is None:
                        self._comp.finish()
                    else:  # do not raise a second exception while unwinding
                        try:
                            self._comp.stdin.close()
                        except Exception:
                            pass
            finally:
                if self._enc is not None:
                    self._enc.__exit__(exc_type, exc, tb)
        return False


class _CountingSink:
    def __init__(self, enc: EncryptedWriter, build: "ArchiveBuild"):
        self._enc = enc
        self._build = build

    def write(self, data: bytes) -> int:
        self._build.compressed_bytes += len(data)
        return self._enc.write(data)


# ---------------------------------------------------------------------------
# Reading side
# ---------------------------------------------------------------------------

class ArchiveStream:
    """Decompress + decrypt an archive and hand out a tar stream."""

    def __init__(self, src_path: str, cfg, logger: Logger | None = None):
        self.src_path = src_path
        self.cfg = cfg
        self.logger = logger or Logger()
        self._dec = None
        self._proc = None

    def __enter__(self):
        self._dec = EncryptedWriter  # documentation aid
        from .crypto import EncryptedReader

        self._dec = EncryptedReader(self.src_path, self.cfg, logger=self.logger).__enter__()
        # Decompressor: read encrypted-plaintext from dec, write plain tar to stdout.
        compression = self._detect_compression()
        if compression == "zstd":
            argv = [which("zstd", ("/usr/bin/zstd",)), "-d", "-q", "-c"]
        elif compression == "gzip":
            argv = [which("gzip", ("/usr/bin/gzip",)), "-d", "-c"]
        else:
            argv = None
        if argv is None:
            self._proc = None
            return _Passthrough(self._dec)
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
        def pump():
            try:
                while True:
                    block = self._dec.read(1024 * 1024)
                    if not block:
                        break
                    self._proc.stdin.write(block)
            except (BrokenPipeError, ValueError):
                pass
            finally:
                try:
                    self._proc.stdin.close()
                except (BrokenPipeError, ValueError):
                    pass

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()
        return self._proc.stdout

    def _detect_compression(self) -> str:
        """Compression is declared in the manifest; default to zstd on magic sniff."""
        declared = str(self.cfg.get("archive", "compression", "zstd")).lower()
        return declared

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._proc is not None:
            try:
                self._proc.wait(timeout=30)
            except Exception:  # pragma: no cover
                self._proc.kill()
        if self._dec is not None:
            self._dec.__exit__(exc_type, exc, tb)
        return False


class _Passthrough:
    def __init__(self, inner):
        self._inner = inner

    def read(self, size: int = -1) -> bytes:
        return self._inner.read(size)
