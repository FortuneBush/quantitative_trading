"""Online, integrity-checked backups for the local SQLite store.

The application keeps SQLite as a single-writer local store.  A backup must
therefore use SQLite's online backup API instead of copying the database file
while it may be in WAL mode.  The destination is written to a temporary file,
checked, flushed and atomically replaced only after ``integrity_check``
returns ``ok``.
"""

from __future__ import annotations

import os
import hashlib
import math
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
from urllib.parse import quote

from quant_monitor.storage.migrations import DEFAULT_MIGRATIONS, MigrationRunner


PathLike = Union[str, os.PathLike]


@dataclass(frozen=True)
class BackupResult:
    """Evidence produced by one completed SQLite backup."""

    source: str
    destination: str
    page_count: int
    integrity_check: str
    created_at: datetime
    # Content identity lets an operator pin the exact archive that passed the
    # SQLite checks.  It is intentionally a digest of the published file,
    # rather than of mutable WAL sidecars.
    sha256: str = ""

    def __post_init__(self) -> None:
        if not self.source or not self.destination:
            raise ValueError("backup paths cannot be empty")
        if isinstance(self.page_count, bool) or not isinstance(self.page_count, int):
            raise TypeError("page_count must be an integer")
        if self.page_count < 0:
            raise ValueError("page_count cannot be negative")
        if self.integrity_check != "ok":
            raise ValueError("backup integrity_check must be ok")
        _validate_sha256(self.sha256)
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise TypeError("created_at must be timezone-aware")

    def to_dict(self):
        return {
            "source": self.source,
            "destination": self.destination,
            "page_count": self.page_count,
            "integrity_check": self.integrity_check,
            "created_at": self.created_at.isoformat(),
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class BackupVerification:
    """Read-only evidence that a SQLite file can be used as a restore source.

    Verification deliberately opens the file in SQLite read-only mode and
    never runs migrations or contacts a market-data provider.  In addition to
    ``integrity_check`` it validates the application's checksummed schema
    ledger, so a syntactically valid but tampered database is rejected.
    """

    path: str
    page_count: int
    schema_version: int
    table_count: int
    integrity_check: str
    verified_at: datetime
    sha256: str = ""

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("verification path cannot be empty")
        for name in ("page_count", "schema_version", "table_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("{} must be an integer".format(name))
            if value < 0:
                raise ValueError("{} cannot be negative".format(name))
        if self.integrity_check != "ok":
            raise ValueError("verified integrity_check must be ok")
        _validate_sha256(self.sha256)
        if not isinstance(self.verified_at, datetime) or self.verified_at.tzinfo is None:
            raise TypeError("verified_at must be timezone-aware")

    def to_dict(self):
        return {
            "path": self.path,
            "page_count": self.page_count,
            "schema_version": self.schema_version,
            "table_count": self.table_count,
            "integrity_check": self.integrity_check,
            "verified_at": self.verified_at.isoformat(),
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class BackupFreshness:
    """Read-only RPO evidence for a local SQLite archive.

    ``backup_sqlite`` publishes an archive atomically, so the file mtime is a
    useful operator-facing lower bound for when the archive became available.
    This check deliberately does not open SQLite or contact a provider; callers
    can combine it with :func:`verify_sqlite` when integrity evidence is also
    required.  A future mtime is reported separately instead of being silently
    treated as fresh, which makes clock/restore mistakes visible to automation.
    """

    path: str
    modified_at: datetime
    checked_at: datetime
    age_seconds: float
    max_age_seconds: float
    state: str

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("freshness path cannot be empty")
        for name in ("modified_at", "checked_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise TypeError("{} must be timezone-aware".format(name))
        for name in ("age_seconds", "max_age_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("{} must be numeric".format(name))
            if not math.isfinite(float(value)):
                raise ValueError("{} must be finite".format(name))
        if self.max_age_seconds < 0:
            raise ValueError("max_age_seconds cannot be negative")
        if self.state not in {"fresh", "stale", "future"}:
            raise ValueError("unknown freshness state: {}".format(self.state))

    @property
    def fresh(self) -> bool:
        """Whether the archive is within the configured RPO window."""

        return self.state == "fresh"

    def to_dict(self):
        return {
            "path": self.path,
            "modified_at": self.modified_at.isoformat(),
            "checked_at": self.checked_at.isoformat(),
            "age_seconds": round(float(self.age_seconds), 3),
            "max_age_seconds": round(float(self.max_age_seconds), 3),
            "state": self.state,
            "fresh": self.fresh,
        }


@dataclass(frozen=True)
class RestoreDrillResult:
    """Evidence from restoring a backup into a previously empty directory."""

    source: str
    destination: str
    verification: BackupVerification
    restored_at: datetime

    def __post_init__(self) -> None:
        if not self.source or not self.destination:
            raise ValueError("restore paths cannot be empty")
        if not isinstance(self.verification, BackupVerification):
            raise TypeError("verification must be BackupVerification")
        if not isinstance(self.restored_at, datetime) or self.restored_at.tzinfo is None:
            raise TypeError("restored_at must be timezone-aware")

    def to_dict(self):
        payload = {
            "source": self.source,
            "destination": self.destination,
            "restored_at": self.restored_at.isoformat(),
        }
        payload["verification"] = self.verification.to_dict()
        return payload


def _validate_sha256(value: str) -> None:
    """Validate optional backwards-compatible content identity fields."""

    if not isinstance(value, str):
        raise TypeError("sha256 must be a string")
    if value and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError("sha256 must be a lowercase SHA-256 hex digest")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_timeout(timeout_seconds: float) -> float:
    value = float(timeout_seconds)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    return value


def check_backup_freshness(
    path: PathLike,
    *,
    max_age_seconds: float,
    now: Optional[datetime] = None,
) -> BackupFreshness:
    """Assess whether a backup file satisfies an operator RPO bound.

    The function is intentionally metadata-only: it reads the archive's
    filesystem modification time and never opens SQLite.  This keeps a
    scheduled freshness probe cheap while allowing a separate integrity check
    to be run when the probe reports a candidate archive.  ``now`` is injectable
    for deterministic tests and must be timezone-aware when supplied.
    """

    if isinstance(max_age_seconds, bool):
        raise ValueError("max_age_seconds must be finite and non-negative")
    try:
        max_age = float(max_age_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_age_seconds must be finite and non-negative") from exc
    if not math.isfinite(max_age) or max_age < 0:
        raise ValueError("max_age_seconds must be finite and non-negative")
    archive_path = Path(path).expanduser().resolve()
    if not archive_path.exists() or not archive_path.is_file():
        raise FileNotFoundError(str(archive_path))
    checked_at = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(checked_at, datetime) or checked_at.tzinfo is None:
        raise TypeError("now must be timezone-aware")
    checked_at = checked_at.astimezone(timezone.utc)
    try:
        modified_at = datetime.fromtimestamp(
            archive_path.stat().st_mtime, tz=timezone.utc
        )
    except (OSError, OverflowError, ValueError) as exc:
        raise OSError("cannot read backup modification time") from exc
    age_seconds = (checked_at - modified_at).total_seconds()
    if age_seconds < 0:
        state = "future"
    elif age_seconds > max_age:
        state = "stale"
    else:
        state = "fresh"
    return BackupFreshness(
        path=str(archive_path),
        modified_at=modified_at,
        checked_at=checked_at,
        age_seconds=age_seconds,
        max_age_seconds=max_age,
        state=state,
    )


def verify_sqlite(
    path: PathLike,
    *,
    timeout_seconds: float = 5.0,
) -> BackupVerification:
    """Verify a SQLite backup without changing it.

    The function is intentionally independent of the configured provider and
    does not instantiate :class:`SQLiteRepository`; this makes it safe to run
    offline while the service is stopped or against an archived copy.
    """

    timeout_seconds = _validate_timeout(timeout_seconds)
    database_path = Path(path).expanduser().resolve()
    if not database_path.exists() or not database_path.is_file():
        raise FileNotFoundError(str(database_path))
    # URI mode=ro avoids creating a new empty SQLite file for a typo and
    # prevents verification from mutating the archive.
    # ``immutable=1`` is important on Windows: opening a WAL database with a
    # normal read-only URI may create ``-shm`` sidecars.  A backup artifact is
    # expected to be self-contained (the online backup writer emits a
    # checkpointed file), so refuse to consult mutable WAL state here.
    # Percent-encode the complete filesystem component.  Merely escaping ``?``
    # is insufficient for archives whose Windows path contains ``#``, ``%``
    # or non-ASCII characters: SQLite would otherwise parse those as URI
    # syntax and could open a different file (or fail before verification).
    encoded_path = quote(str(database_path).replace("\\", "/"), safe="/:")
    uri = "file:{}?mode=ro&immutable=1".format(encoded_path)
    connection = sqlite3.connect(uri, uri=True, timeout=float(timeout_seconds))
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise sqlite3.DatabaseError(
                "backup integrity_check failed: {}".format(integrity)
            )
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        # Validate both the migration ledger checksums and the exact schema
        # contract.  These are immutable in-process definitions.
        MigrationRunner(DEFAULT_MIGRATIONS)._validate_current_state(  # noqa: SLF001
            connection, schema_version
        )
        table_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    return BackupVerification(
        path=str(database_path),
        page_count=page_count,
        schema_version=schema_version,
        table_count=table_count,
        integrity_check="ok",
        verified_at=datetime.now(timezone.utc),
        sha256=_sha256_file(database_path),
    )


def restore_sqlite_to_empty_dir(
    source: PathLike,
    destination_dir: PathLike,
    *,
    filename: str = "restored.sqlite3",
    timeout_seconds: float = 5.0,
) -> RestoreDrillResult:
    """Restore ``source`` into an empty directory and verify the result.

    Existing non-empty directories are rejected to make a recovery drill
    auditable and non-destructive.  A missing directory is created.  The
    source itself is verified before copying and the restored file is verified
    again after the online backup completes.
    """
    timeout_seconds = _validate_timeout(timeout_seconds)

    source_path = Path(source).expanduser().resolve()
    directory = Path(destination_dir).expanduser().resolve()
    if not filename or Path(filename).name != filename:
        raise ValueError("filename must be a simple file name")
    if directory.exists():
        if not directory.is_dir():
            raise ValueError("restore destination must be a directory")
        if any(directory.iterdir()):
            raise ValueError("restore destination directory must be empty")
    else:
        directory.mkdir(parents=True, exist_ok=False)
    source_verification = verify_sqlite(source_path, timeout_seconds=timeout_seconds)
    destination = directory / filename
    backup_result = backup_sqlite(source_path, destination, timeout_seconds=timeout_seconds)
    restored_verification = verify_sqlite(destination, timeout_seconds=timeout_seconds)
    if restored_verification.schema_version != source_verification.schema_version:
        raise sqlite3.DatabaseError("restored schema version differs from source")
    if restored_verification.sha256 != backup_result.sha256:
        raise sqlite3.DatabaseError("restored content digest differs from backup result")
    return RestoreDrillResult(
        source=str(source_path),
        destination=str(destination),
        verification=restored_verification,
        restored_at=datetime.now(timezone.utc),
    )


def backup_sqlite(
    source: PathLike,
    destination: PathLike,
    *,
    timeout_seconds: float = 5.0,
    pages_per_step: int = 256,
) -> BackupResult:
    """Create an online backup and atomically publish it.

    ``source`` must be a file-backed SQLite database.  The destination may be
    replaced intentionally, but it is never exposed until the copied database
    passes an integrity check.  Any failure removes the temporary artifact and
    leaves an existing destination untouched.
    """

    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    if str(source_path) == str(destination_path):
        raise ValueError("backup destination must differ from source")
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(str(source_path))
    # ``resolve()`` catches most aliases, but hard links and Windows short
    # names can still point at the same inode.  Refuse those explicitly so a
    # backup can never accidentally target the live database itself.
    if destination_path.exists() and destination_path.is_file():
        try:
            if os.path.samefile(str(source_path), str(destination_path)):
                raise ValueError("backup destination must not alias source")
        except (OSError, FileNotFoundError):
            # A destination may disappear between exists() and samefile();
            # the subsequent atomic replace remains safe in that case.
            pass
    timeout_seconds = _validate_timeout(timeout_seconds)
    if isinstance(pages_per_step, bool) or not isinstance(pages_per_step, int):
        raise TypeError("pages_per_step must be an integer")
    if pages_per_step <= 0:
        raise ValueError("pages_per_step must be positive")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    source_connection = sqlite3.connect(str(source_path), timeout=float(timeout_seconds))
    try:
        with tempfile.NamedTemporaryFile(
            prefix=destination_path.name + ".",
            suffix=".tmp",
            dir=str(destination_path.parent),
            delete=False,
        ) as handle:
            temporary_name = handle.name
        destination_connection = sqlite3.connect(
            temporary_name, timeout=float(timeout_seconds)
        )
        try:
            deadline = time.monotonic() + float(timeout_seconds)

            def _backup_progress(_status: int, _remaining: int, _total: int) -> None:
                # sqlite3's ``timeout`` bounds an individual lock wait, but a
                # busy source can otherwise make repeated backup steps exceed
                # the operator's requested copy deadline.  Abort before
                # publishing the temporary artifact; the caller can retry.
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "SQLite backup exceeded timeout_seconds={}".format(
                            float(timeout_seconds)
                        )
                    )

            source_connection.backup(
                destination_connection,
                pages=int(pages_per_step),
                progress=_backup_progress,
                sleep=min(0.25, max(0.001, float(timeout_seconds) / 20.0)),
            )
            destination_connection.commit()
            integrity = destination_connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError("backup integrity_check failed")
            page_count = int(
                destination_connection.execute("PRAGMA page_count").fetchone()[0]
            )
            schema_version = int(
                destination_connection.execute("PRAGMA user_version").fetchone()[0]
            )
            # A syntactically valid SQLite file is not necessarily a valid
            # quant-monitor archive.  Validate the immutable migration ledger
            # before publishing the destination, so callers never receive a
            # successful result for an unsupported/tampered schema.
            MigrationRunner(DEFAULT_MIGRATIONS)._validate_current_state(  # noqa: SLF001
                destination_connection, schema_version
            )
        finally:
            destination_connection.close()
        with open(temporary_name, "rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination_path)
        temporary_name = None
        digest = _sha256_file(destination_path)
        return BackupResult(
            source=str(source_path),
            destination=str(destination_path),
            page_count=page_count,
            integrity_check="ok",
            created_at=datetime.now(timezone.utc),
            sha256=digest,
        )
    finally:
        source_connection.close()
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


__all__ = [
    "BackupFreshness",
    "BackupResult",
    "BackupVerification",
    "RestoreDrillResult",
    "backup_sqlite",
    "check_backup_freshness",
    "restore_sqlite_to_empty_dir",
    "verify_sqlite",
]
