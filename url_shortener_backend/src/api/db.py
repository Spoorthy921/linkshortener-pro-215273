import os
import re
import secrets
import string
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


_SLUG_ALLOWED_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


@dataclass(frozen=True)
class DbConfig:
    """Database configuration resolved from environment variables."""
    dsn: str


# PUBLIC_INTERFACE
def get_db_config() -> DbConfig:
    """Resolve PostgreSQL connection DSN from environment variables.

    Uses the database container env var names:
      - POSTGRES_URL (preferred, full DSN)
      - POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, POSTGRES_PORT (fallback)

    Returns:
        DbConfig: Resolved configuration including DSN.
    """
    postgres_url = os.getenv("POSTGRES_URL")
    if postgres_url:
        return DbConfig(dsn=postgres_url)

    # Fallback: construct DSN from parts (do not assume they exist).
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    db = os.getenv("POSTGRES_DB")
    port = os.getenv("POSTGRES_PORT")

    missing = [name for name, val in [
        ("POSTGRES_URL", postgres_url),
        ("POSTGRES_USER", user),
        ("POSTGRES_PASSWORD", password),
        ("POSTGRES_DB", db),
        ("POSTGRES_PORT", port),
    ] if not val]

    if missing:
        raise RuntimeError(
            "Database is not configured. Set POSTGRES_URL (recommended) or "
            "POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB/POSTGRES_PORT. "
            f"Missing: {', '.join(missing)}"
        )

    # Host is typically localhost in this dev container topology.
    # If needed, set POSTGRES_URL instead to fully control host.
    dsn = f"postgresql://{user}:{password}@localhost:{port}/{db}"
    return DbConfig(dsn=dsn)


# PUBLIC_INTERFACE
def create_pool() -> ConnectionPool:
    """Create a psycopg connection pool.

    Returns:
        ConnectionPool: A pool ready to be opened on app startup.
    """
    cfg = get_db_config()
    return ConnectionPool(
        conninfo=cfg.dsn,
        min_size=1,
        max_size=10,
        timeout=10,
        kwargs={"row_factory": dict_row},
        open=False,
    )


# PUBLIC_INTERFACE
def init_schema(pool: ConnectionPool) -> None:
    """Create required tables/indexes if they do not exist.

    This is intentionally lightweight (no external migration tool).
    """
    ddl_statements = [
        """
        CREATE TABLE IF NOT EXISTS links (
            id BIGSERIAL PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            long_url TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            click_count BIGINT NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS click_events (
            id BIGSERIAL PRIMARY KEY,
            link_id BIGINT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
            clicked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            user_agent TEXT,
            referer TEXT,
            ip TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_click_events_link_id ON click_events(link_id)",
        "CREATE INDEX IF NOT EXISTS idx_click_events_clicked_at ON click_events(clicked_at)",
    ]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            for stmt in ddl_statements:
                cur.execute(stmt)
        conn.commit()


def _validate_custom_slug(slug: str) -> None:
    if not _SLUG_ALLOWED_RE.match(slug):
        raise ValueError(
            "custom_slug must match ^[A-Za-z0-9_-]{3,64}$ (letters, numbers, _ or -, length 3-64)"
        )


def _generate_slug(length: int = 6) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# PUBLIC_INTERFACE
def create_link(
    pool: ConnectionPool,
    long_url: str,
    custom_slug: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new short link.

    Args:
        pool: Database connection pool.
        long_url: Destination URL.
        custom_slug: Optional desired slug.

    Returns:
        dict: Link row fields.

    Raises:
        ValueError: If custom slug is invalid.
        RuntimeError: If unable to generate a unique slug after retries.
    """
    if custom_slug:
        _validate_custom_slug(custom_slug)

    with pool.connection() as conn:
        with conn.cursor() as cur:
            if custom_slug:
                cur.execute(
                    """
                    INSERT INTO links (slug, long_url)
                    VALUES (%s, %s)
                    RETURNING id, slug, long_url, created_at, updated_at, click_count
                    """,
                    (custom_slug, long_url),
                )
                row = cur.fetchone()
                conn.commit()
                return row

            # Generate slug with collision checks.
            for _ in range(10):
                slug = _generate_slug()
                try:
                    cur.execute(
                        """
                        INSERT INTO links (slug, long_url)
                        VALUES (%s, %s)
                        RETURNING id, slug, long_url, created_at, updated_at, click_count
                        """,
                        (slug, long_url),
                    )
                    row = cur.fetchone()
                    conn.commit()
                    return row
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
                    continue

            raise RuntimeError("Unable to generate a unique slug after multiple attempts.")


# PUBLIC_INTERFACE
def list_links(
    pool: ConnectionPool,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """List links with pagination.

    Returns:
        (links, total_count)
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM links")
            total = int(cur.fetchone()["count"])

            cur.execute(
                """
                SELECT id, slug, long_url, created_at, updated_at, click_count
                FROM links
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
            return rows, total


# PUBLIC_INTERFACE
def get_link_by_slug(pool: ConnectionPool, slug: str) -> Optional[Dict[str, Any]]:
    """Fetch a link by slug."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, slug, long_url, created_at, updated_at, click_count
                FROM links
                WHERE slug = %s
                """,
                (slug,),
            )
            return cur.fetchone()


# PUBLIC_INTERFACE
def update_link(pool: ConnectionPool, slug: str, new_long_url: str) -> Optional[Dict[str, Any]]:
    """Update a link's destination URL (slug remains stable)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE links
                SET long_url = %s, updated_at = NOW()
                WHERE slug = %s
                RETURNING id, slug, long_url, created_at, updated_at, click_count
                """,
                (new_long_url, slug),
            )
            row = cur.fetchone()
            conn.commit()
            return row


# PUBLIC_INTERFACE
def delete_link(pool: ConnectionPool, slug: str) -> bool:
    """Delete a link by slug. Returns True if deleted."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM links WHERE slug = %s", (slug,))
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted


# PUBLIC_INTERFACE
def record_click_and_get_destination(
    pool: ConnectionPool,
    slug: str,
    user_agent: Optional[str],
    referer: Optional[str],
    ip: Optional[str],
) -> Optional[str]:
    """Increment click count and add a click event for the link identified by slug.

    Returns:
        The destination long_url if slug exists, else None.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, long_url FROM links WHERE slug = %s",
                (slug,),
            )
            row = cur.fetchone()
            if not row:
                return None

            link_id = row["id"]
            long_url = row["long_url"]

            cur.execute(
                """
                INSERT INTO click_events (link_id, user_agent, referer, ip)
                VALUES (%s, %s, %s, %s)
                """,
                (link_id, user_agent, referer, ip),
            )
            cur.execute(
                """
                UPDATE links
                SET click_count = click_count + 1, updated_at = NOW()
                WHERE id = %s
                """,
                (link_id,),
            )
            conn.commit()
            return long_url


# PUBLIC_INTERFACE
def get_analytics(pool: ConnectionPool, slug: str) -> Optional[Dict[str, Any]]:
    """Return basic analytics for a link: click_count and last_clicked_at."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, click_count FROM links WHERE slug = %s",
                (slug,),
            )
            link = cur.fetchone()
            if not link:
                return None

            cur.execute(
                """
                SELECT MAX(clicked_at) AS last_clicked_at
                FROM click_events
                WHERE link_id = %s
                """,
                (link["id"],),
            )
            last = cur.fetchone()["last_clicked_at"]
            return {"click_count": int(link["click_count"]), "last_clicked_at": last}
