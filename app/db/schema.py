"""
Database schema definitions — all CREATE TABLE IF NOT EXISTS statements.

Extracted from app/database.py to reduce monolith size.
Called once on startup via init_db().
"""

import logging


async def create_tables(db_query):
    """Create all application tables if they don't exist."""

    logging.info("Skipping legacy inline python schema creation. Relying solely on DDL migration scripts.")
