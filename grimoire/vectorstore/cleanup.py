"""Removing a deleted document's vectors.

One contract for every delete path (REST, MCP, watcher), so they can't
disagree again (audit findings C-3/C-4: the REST route deleted vectors first
and failed closed, the MCP tool deleted them last and reported an error):

1. Delete the document's rows and **commit** first. The database is the
   source of truth; once the commit lands, the document is gone.
2. Then call :func:`purge_document_vectors`. It never raises. If the vector
   store is down, the vectors are left behind as orphans and logged at ERROR
   with the document id and the vector ids, so an operator can remove them
   (``grimoire status --detailed`` shows the chunk/vector count drift).
3. Orphans are never served: hybrid search drops hits whose document no
   longer exists, so a failed cleanup leaves dead index entries, not
   searchable deleted content.
"""

from __future__ import annotations

from loguru import logger

from grimoire.vectorstore.factory import get_maintenance_store

# Cap on how many orphaned ids go into one log line.
_MAX_LOGGED_IDS = 20


async def purge_document_vectors(document_id: str, vector_ids: list[str]) -> bool:
    """Delete a document's vectors after its rows have been committed.

    Args:
        document_id: The deleted document's id, for logs.
        vector_ids: Vector ids collected from the document's chunks *before*
            the rows were deleted (they can't be read afterwards).

    Returns:
        True if the vectors were removed (or there were none), False if they
        were left behind as orphans. Never raises.
    """
    if not vector_ids:
        return True
    try:
        store = await get_maintenance_store()
        await store.delete(vector_ids)
    except Exception as e:
        shown = vector_ids[:_MAX_LOGGED_IDS]
        more = len(vector_ids) - len(shown)
        logger.error(
            f"Vector cleanup failed for deleted document {document_id}; "
            f"{len(vector_ids)} orphaned vector(s) left in the store "
            f"(hidden from search): {shown}"
            + (f" and {more} more" if more else "")
            + f". Cause: {type(e).__name__}: {e}"
        )
        return False
    logger.info(f"Deleted {len(vector_ids)} vectors for document {document_id}")
    return True
