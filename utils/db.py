"""
Helper de conexão com PostgreSQL.
"""

import os

import psycopg2
import psycopg2.extras


def get_connection(cursor_factory=psycopg2.extras.RealDictCursor):
    """Retorna uma conexão com o PostgreSQL usando variáveis de ambiente."""
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", 5432)),
        dbname=os.getenv("PG_DATABASE", "n8n_queue"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASSWORD", ""),
        sslmode="disable",
        cursor_factory=cursor_factory,
    )
