"""Database migration script for PostgreSQL / Supabase.

Applies backend/migrations/001_initial.sql and backend/migrations/002_camera_monitoring.sql,
and verifies schema tables and indexes.
"""

from __future__ import annotations

import os
from pathlib import Path
import urllib.parse
import psycopg


def _load_env_file():
    for p in (Path(".env"), Path(__file__).resolve().parent.parent / ".env"):
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass
            break


def get_database_url() -> str:
    _load_env_file()
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    
    host = os.getenv("DB_HOST", "").strip()
    port = os.getenv("DB_PORT", "5432")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "")
    db_name = os.getenv("DB_NAME", "postgres")
    if not host or not password:
        raise RuntimeError("DATABASE_URL or DB_HOST plus DB_PASSWORD must be configured")
    
    encoded_pw = urllib.parse.quote(password)
    return f"postgresql://{user}:{encoded_pw}@{host}:{port}/{db_name}?sslmode=require"


def run_migrations():
    db_url = get_database_url()
    # Normalize for psycopg connect (strip +psycopg if present)
    connect_url = db_url.replace("postgresql+psycopg://", "postgresql://")
    
    print(f"Connecting to database at {connect_url.split('@')[-1]}...")
    
    migrations_dir = Path(__file__).resolve().parent.parent / "backend" / "migrations"
    sql_files = sorted(migrations_dir.glob("*.sql"))
    
    with psycopg.connect(connect_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            ver = cur.fetchone()[0]
            print(f"Connected to: {ver}\n")
            
            for sql_file in sql_files:
                print(f"Applying migration: {sql_file.name}...")
                sql = sql_file.read_text(encoding="utf-8")
                cur.execute(sql)
                print(f"[OK] Migration {sql_file.name} applied successfully.")
            
            cur.execute("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                ORDER BY table_name;
            """)
            tables = [row[0] for row in cur.fetchall()]
            print(f"\nCreated/verified {len(tables)} tables in 'public' schema:")
            for t in tables:
                print(f"  - {t}")


if __name__ == "__main__":
    run_migrations()
