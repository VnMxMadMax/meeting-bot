"""Credentials and the one Neo4j driver Otto uses.

Read from the environment, or a .env beside this package, or rag/.env — whichever is
found first. Nothing is hard-coded, so this package carries no secrets.
"""

import logging
import os

logger = logging.getLogger("otto")

try:
    from dotenv import load_dotenv
    for _candidate in (
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "Ronald-Johnson-AI", "rag", ".env"),
        ".env",
    ):
        if os.path.isfile(_candidate):
            load_dotenv(_candidate)
            break
except ImportError:
    pass

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
NEO4J_URI      = os.getenv("NEO4J_URI", "")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM  = os.getenv("JWT_ALGORITHM", "HS256")

OPENAI_MODEL = os.getenv("OTTO_MODEL", "gpt-4o")
OPENAI_TEMPERATURE = 0.0          # extraction must be repeatable


def create_driver():
    """A Neo4j driver with the API's own connection settings.

    Aura recycles idle connections, so the lifetime is kept under the server-side
    timeout and the socket is kept alive — otherwise the pool hands out a half-dead
    connection.
    """
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
        connection_timeout=30,
        max_connection_lifetime=300,
        keep_alive=True,
        max_connection_pool_size=50,
        connection_acquisition_timeout=10,
    )


def missing_settings():
    """Which required settings are absent — so a caller can say so plainly."""
    return [name for name, value in [
        ("NEO4J_URI", NEO4J_URI), ("NEO4J_PASSWORD", NEO4J_PASSWORD)] if not value]
