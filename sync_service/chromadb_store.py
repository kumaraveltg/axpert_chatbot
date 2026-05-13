"""
sync_service/chromadb_store.py

Handles all ChromaDB read/write operations.
Uses shared/chroma_config for the client — single source of truth.

Collections:
  axpert_<schema_name>  → per-company form knowledge
  axpert_shared         → shared across all companies
                          (axusr, axglo, axrol, manual instructions)
"""

from shared.chroma_config import get_collection, get_existing_collection


# ══════════════════════════════════════════════════════════════
# SCHEMA-SPECIFIC (per company)
# ══════════════════════════════════════════════════════════════

def save_to_chromadb(
    schema_name: str,
    chroma_id:   str,
    document:    str,
    metadata:    dict
):
    """
    Save one document to the company-specific collection.
    Collection name: axpert_<schema_name>
    Uses upsert — re-saving updates the existing document.
    """
    col = get_collection(f"axpert_{schema_name}")
    col.upsert(
        ids       = [chroma_id],
        documents = [document],
        metadatas = [metadata]
    )


def query_chromadb(
    schema_name: str,
    question:    str,
    n_results:   int = 5
) -> list[dict]:
    """
    Search the company-specific collection.
    Returns list of {id, document, metadata, distance}
    """
    try:
        col = get_existing_collection(f"axpert_{schema_name}")
    except ValueError:
        return []

    if col.count() == 0:
        return []

    results = col.query(
        query_texts = [question],
        n_results   = min(n_results, col.count()),
        include     = ["documents", "metadatas", "distances"]
    )

    return [
        {
            "id":       results["ids"][0][i],
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": results["distances"][0][i]
        }
        for i in range(len(results["documents"][0]))
    ]


def delete_schema_collection(schema_name: str):
    """Delete all documents for a company (used when connection is deleted)"""
    from shared.chroma_config import get_chroma
    client = get_chroma()
    try:
        client.delete_collection(f"axpert_{schema_name}")
        print(f"[chromadb] Deleted collection: axpert_{schema_name}")
    except Exception as e:
        print(f"[chromadb] Could not delete collection axpert_{schema_name}: {e}")


# ══════════════════════════════════════════════════════════════
# SHARED (all companies)
# ══════════════════════════════════════════════════════════════

def save_to_shared(
    doc_id:   str,
    document: str,
    metadata: dict
):
    """
    Save one document to the shared collection (axpert_shared).

    For DB table sync:
      doc_id   = "<table>_<record_id>"  e.g. "axusr_U001", "axglo_VAT"
      metadata = { source: "db_sync", table: "axusr", ... }

    For manual instructions:
      doc_id   = "manual_<topic>"       e.g. "manual_debug_form"
      metadata = { source: "manual", table: "none", ... }
    """
    col = get_collection("axpert_shared")
    col.upsert(
        ids       = [doc_id],
        documents = [document],
        metadatas = [metadata]
    )


def query_shared(
    question:  str,
    n_results: int = 3
) -> list[dict]:
    """
    Search the shared collection (axpert_shared).
    Returns list of {id, document, metadata, distance}
    """
    try:
        col = get_existing_collection("axpert_shared")
    except ValueError:
        return []

    if col.count() == 0:
        return []

    results = col.query(
        query_texts = [question],
        n_results   = min(n_results, col.count()),
        include     = ["documents", "metadatas", "distances"]
    )

    return [
        {
            "id":       results["ids"][0][i],
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": results["distances"][0][i]
        }
        for i in range(len(results["documents"][0]))
    ]


def delete_shared_entry(doc_id: str):
    """Delete one entry from the shared collection by doc_id"""
    try:
        col = get_existing_collection("axpert_shared")
        col.delete(ids=[doc_id])
        print(f"[chromadb] Deleted shared entry: {doc_id}")
    except Exception as e:
        print(f"[chromadb] Could not delete shared entry {doc_id}: {e}")


# ══════════════════════════════════════════════════════════════
# COMBINED (used by chat service)
# ══════════════════════════════════════════════════════════════

def query_combined(
    schema_name:      str,
    question:         str,
    n_results_schema: int = 5,
    n_results_shared: int = 3
) -> list[dict]:
    """
    Search both company-specific and shared collections.
    Merges results, deduplicates by id, sorts by distance.
    Returns unified list of {id, document, metadata, distance}
    """
    schema_results = query_chromadb(schema_name, question, n_results_schema)
    shared_results = query_shared(question, n_results_shared)

    # Merge and deduplicate by id
    seen = set()
    merged = []
    for item in schema_results + shared_results:
        if item["id"] not in seen:
            seen.add(item["id"])
            merged.append(item)

    # Sort by distance — best match first
    merged.sort(key=lambda x: x["distance"])

    return merged