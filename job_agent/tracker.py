import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path


class ApplicationTracker:
    def __init__(self, path: str | Path = "applications.sqlite3") -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    job_url TEXT PRIMARY KEY,
                    company_name TEXT NOT NULL,
                    role_title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT ''
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record(
        self,
        *,
        job_url: str,
        company_name: str,
        role_title: str,
        status: str,
        notes: str = "",
    ) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO applications
                    (job_url, company_name, role_title, status, updated_at, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_url) DO UPDATE SET
                    company_name = excluded.company_name,
                    role_title = excluded.role_title,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    notes = excluded.notes
                """,
                (job_url, company_name, role_title, status, timestamp, notes),
            )

    def processed_urls(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_url FROM applications WHERE status IN ('SUBMITTED', 'SKIPPED', 'REVIEWED')"
            ).fetchall()
        return {row["job_url"] for row in rows}

    def list_applications(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_url, company_name, role_title, status, updated_at, notes
                FROM applications ORDER BY updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]
