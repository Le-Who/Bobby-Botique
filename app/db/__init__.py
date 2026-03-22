# app/db/ — Database sub-package extracted from the database.py monolith.
#
# Modules:
#   schema.py      — Startup validation (verifies expected tables exist)
#   migrations.py  — SQL file runner + legacy inline migrations
#   rls.py         — Row Level Security setup / policy templates
#   seed.py        — Initial data inserts (keys, admin user, indexes)
