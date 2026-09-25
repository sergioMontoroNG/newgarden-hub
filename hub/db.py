import os
from pathlib import Path
from psycopg_pool import ConnectionPool

pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=5, open=True)

def initialize():
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(781232019)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        for migration in sorted(Path("migrations").glob("*.sql")):
            claimed=conn.execute("INSERT INTO schema_migrations(version) VALUES(%s) ON CONFLICT DO NOTHING RETURNING version",(migration.stem,)).fetchone()
            if claimed:
                conn.execute(migration.read_text())
