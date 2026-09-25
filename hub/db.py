import os
from pathlib import Path
from psycopg_pool import ConnectionPool

pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=5, open=True)

def initialize():
    with pool.connection() as conn:
        conn.execute(Path("migrations/001_initial.sql").read_text())
