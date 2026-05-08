"""Data access layer for stash.

Per spec § "Architecture · Layer responsibilities", this is the ONLY
thing that talks to SQLite.  Every read filters by ``actor.tenant_id``
and every mutation gates on ``actor.role`` — both layered on top of the
``current_actor`` middleware that resolves identity per request, so a
forgotten guard at one layer can't leak data across tenants.

The CI lint enforces this: ``conn.execute(`` must not appear outside
this package once the migration is complete.  Until then, untenanted
routes fall through the migration sweep in ``migrate_db()`` so any row
they create with NULL tenant_id gets rolled into the active tenant on
the next restart.

Public surface (one module per aggregate):

* :mod:`dao.boxes` — boxes + their item-mosaic read paths.
* :mod:`dao.items` — items, item_tags, item ingest hooks.
* :mod:`dao.locations`, :mod:`dao.floors`, :mod:`dao.rooms`.
* :mod:`dao.tags`.
* :mod:`dao.pending_items`, :mod:`dao.ingest_jobs` — sort queue.
* :mod:`dao.tenants` — tenant + member lookups.

Common helpers + error types live in :mod:`dao._base`.
"""

from dao._base import (
    Actor,
    ConflictError,
    DAOError,
    ForbiddenError,
    NotFoundError,
    require_membership,
    require_role,
)

__all__ = [
    "Actor",
    "ConflictError",
    "DAOError",
    "ForbiddenError",
    "NotFoundError",
    "require_membership",
    "require_role",
]
