"""Spotify -> YouTube one-way playlist mirror.

Spotify is the source of truth. See docs/ARCHITECTURE.md for the design.
"""

__version__ = "1.0.0"

# Schema version of the on-disk SQLite database. Bump when adding a migration.
DB_SCHEMA_VERSION = 1
