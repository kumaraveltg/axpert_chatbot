"""
sync_service/main.py
"""

from fastapi import FastAPI, HTTPException 
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import os
from dotenv import load_dotenv
import time
from datetime import datetime
import requests as http_requests
from shared.database import (
    get_db,
    init_db,
    get_schema_connection
)
from shared.models import (
    CompanyRegistry,
    IndustryMaster,
    PracticeMaster,
    GeneratedDocument,
    SyncModuleConfig
)
from sync_service.detector import (
    detect_modules,
    detect_practice_chains
)
from sync_service.extractor import (
    extract_form_metadata
)
from sync_service.generator import (
    generate_document
)
from sync_service.chromadb_store import (
    save_to_chromadb,
    save_to_shared,
    delete_shared_entry,
    query_shared
)

load_dotenv()

app = FastAPI(
    title="Axpert Sync Service",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"]
)


# ── Retry wrapper ─────────────────────────────────────────────

def generate_with_retry(max_retries=3, wait=120, **kwargs):
    for attempt in range(max_retries):
        try:
            return generate_document(**kwargs)
        except Exception as e:
            if '429' in str(e) or 'rate_limit' in str(e).lower():
                print(f"Rate limit hit — waiting {wait}s before retry {attempt+1}/{max_retries}")
                time.sleep(wait)
            else:
                raise e
    raise Exception("Max retries exceeded")


# ── Health ────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "service": "sync_service",
        "status":  "running ✅"
    }


# ══════════════════════════════════════════════════════════════
# SYNC
# ══════════════════════════════════════════════════════════════

@app.post("/sync/{schema_name}")
async def sync_schema(schema_name: str):
    db_gen = get_db()
    db = next(db_gen)
    try:
        company = db.query(CompanyRegistry)\
                    .filter_by(
                        schema_name = schema_name,
                        is_active   = 'Y'
                    ).first()

        if not company:
            raise HTTPException(
                status_code = 404,
                detail      = f"Schema {schema_name} not found in registry"
            )

        industry_obj = db.query(IndustryMaster)\
                         .filter_by(id=company.industry_id).first()

        industry = industry_obj.industry

        print(f"Syncing: {schema_name} | {industry}")

        module_tree = detect_modules(schema_name)
        print(f"Modules detected: {list(module_tree.keys())}")

        results = []

        all_configs = db.query(SyncModuleConfig).filter_by(
            schema_name = schema_name
        ).all()

        if all_configs:
            enabled_set = {
                (c.root_module, c.sub_module)
                for c in all_configs
                if c.is_enabled == 'Y'
            }
        else:
            enabled_set = None

        for root_mod, sub_mods in module_tree.items():
            for sub_mod, content in sub_mods.items():

                if enabled_set is not None and \
                   (root_mod, sub_mod) not in enabled_set:
                    print(f"⏭ Skipping: {root_mod} → {sub_mod}")
                    continue

                transids = content.get('forms', [])
                if not transids:
                    continue

                print(f"Processing: {root_mod} → {sub_mod} | Forms: {transids}")

                chains = detect_practice_chains(schema_name, transids)
                if not chains:
                    chains = [transids]

                for i, chain in enumerate(chains):
                    practice_name = (
                        f"{sub_mod} Practice {i+1}"
                        if len(chains) > 1
                        else sub_mod
                    )

                    forms_metadata = [
                        extract_form_metadata(schema_name, tid)
                        for tid in chain
                    ]

                    document = generate_document(
                        industry       = industry,
                        module         = root_mod,
                        sub_module     = sub_mod,
                        practice_name  = practice_name,
                        forms_metadata = forms_metadata,
                        schema         = schema_name
                    )

                    existing = db.query(PracticeMaster).filter_by(
                        module        = root_mod,
                        practice_name = practice_name,
                        schema_ref    = schema_name
                    ).first()

                    LEVELS = ['form', 'dc', 'field', 'genmap', 'mdmap', 'fillgrid']

                    if existing:
                        print(f"⏭ Already exists: {practice_name} — running auto-generate only")
                        for tid in chain:
                            for lvl in LEVELS:
                                try:
                                    http_requests.post(
                                        f"http://127.0.0.1:8007/auto-generate/{schema_name}/{tid}/{lvl}",
                                        timeout=60
                                    )
                                    print(f"  ✅ Auto-generated: {tid} / {lvl}")
                                except Exception as e:
                                    print(f"  ⚠ Auto-generate skipped {tid}/{lvl}: {e}")
                        continue

                    practice = PracticeMaster(
                        industry_id   = company.industry_id,
                        module        = root_mod,
                        practice_name = practice_name,
                        schema_ref    = schema_name,
                        transid_chain = ",".join(chain),
                        is_active     = 'Y'
                    )
                    db.add(practice)
                    db.flush()

                    doc = GeneratedDocument(
                        practice_id   = practice.id,
                        industry      = industry,
                        module        = root_mod,
                        practice_name = practice_name,
                        document      = document,
                        status        = "ready"
                    )
                    db.add(doc)
                    db.flush()

                    chroma_id = (
                        f"{schema_name}_{root_mod}_{practice_name}"
                        .replace(" ", "_")
                        .lower()
                    )

                    save_to_chromadb(
                        schema_name = schema_name,
                        chroma_id   = chroma_id,
                        document    = document,
                        metadata    = {
                            "industry":      industry,
                            "module":        root_mod,
                            "sub_module":    sub_mod,
                            "practice":      practice_name,
                            "schema":        schema_name,
                            "transid_chain": ",".join(chain)
                        }
                    )

                    doc.chroma_id = chroma_id
                    db.commit()

                    for tid in chain:
                        for lvl in LEVELS:
                            try:
                                http_requests.post(
                                    f"http://127.0.0.1:8007/auto-generate/{schema_name}/{tid}/{lvl}",
                                    timeout=60
                                )
                                print(f"  ✅ Auto-generated: {tid} / {lvl}")
                            except Exception as e:
                                print(f"  ⚠ Auto-generate skipped {tid}/{lvl}: {e}")

                    results.append({
                        "module":     root_mod,
                        "sub_module": sub_mod,
                        "practice":   practice_name,
                        "forms":      chain,
                        "status":     "✅ synced"
                    })

                    print(f"✅ Synced: {practice_name}")

        return {
            "schema":   schema_name,
            "industry": industry,
            "synced":   len(results),
            "details":  results
        }

    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass

# ══════════════════════════════════════════════════════════════
# MODULE MANAGEMENT
# ══════════════════════════════════════════════════════════════

@app.post("/sync/{schema_name}/register-modules")
async def register_modules(schema_name: str):
    """
    Scans axpages and registers all root_module + sub_module combos
    into sync_module_config table. Default: all enabled.
    """
    db = next(get_db())
    module_tree = detect_modules(schema_name)

    added = []
    for root_mod, sub_mods in module_tree.items():
        for sub_mod in sub_mods.keys():
            exists = db.query(SyncModuleConfig).filter_by(
                schema_name = schema_name,
                root_module = root_mod,
                sub_module  = sub_mod
            ).first()

            if not exists:
                db.add(SyncModuleConfig(
                    schema_name = schema_name,
                    root_module = root_mod,
                    sub_module  = sub_mod,
                    is_enabled  = 'Y'
                ))
                added.append(f"{root_mod} → {sub_mod}")

    db.commit()
    return {"registered": added}


@app.get("/status/{schema_name}")
async def sync_status(schema_name: str):
    """Check sync status for a schema"""
    db   = next(get_db())
    docs = db.query(GeneratedDocument)\
             .join(PracticeMaster)\
             .filter(PracticeMaster.schema_ref == schema_name).all()

    return {
        "schema":    schema_name,
        "documents": len(docs),
        "practices": [
            {
                "module":   d.module,
                "practice": d.practice_name,
                "status":   d.status
            }
            for d in docs
        ]
    }


@app.get("/sync/{schema_name}/modules")
async def list_modules(schema_name: str):
    """List all submodules with enabled status"""
    db = next(get_db())
    configs = db.query(SyncModuleConfig).filter_by(
        schema_name = schema_name
    ).all()
    return [
        {
            "root_module": c.root_module,
            "sub_module":  c.sub_module,
            "is_enabled":  c.is_enabled
        }
        for c in configs
    ]


@app.patch("/sync/{schema_name}/modules")
async def toggle_module(
    schema_name: str,
    root_module: str,
    sub_module:  str,
    enable:      bool
):
    """Enable or disable a specific submodule"""
    db = next(get_db())
    config = db.query(SyncModuleConfig).filter_by(
        schema_name = schema_name,
        root_module = root_module,
        sub_module  = sub_module
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Module not found")

    config.is_enabled = 'Y' if enable else 'N'
    config.updated_at = datetime.now()
    db.commit()

    return {
        "updated":    f"{root_module} → {sub_module}",
        "is_enabled": config.is_enabled
    }


# ══════════════════════════════════════════════════════════════
# SHARED KNOWLEDGE
# ══════════════════════════════════════════════════════════════

class SharedKnowledgeInput(BaseModel):
    doc_id:   str
    document: str
    caption:  str
    source:   str
    table:    Optional[str] = "none"


@app.get("/shared/list")
async def list_shared_knowledge():
    """
    List all entries in axpert_shared collection.
    Used by BasicKnowledge page to show existing entries.
    """
    try:
        from shared.chroma_config import get_existing_collection
        col   = get_existing_collection("axpert_shared")
        count = col.count()

        if count == 0:
            return {"total": 0, "entries": []}

        # Get all entries
        results = col.get(
            include = ["documents", "metadatas"]
        )

        entries = [
            {
                "doc_id":   results["metadatas"][i].get("doc_id",  ids),
                "caption":  results["metadatas"][i].get("caption", ""),
                "source":   results["metadatas"][i].get("source",  "manual"),
                "table":    results["metadatas"][i].get("table",   "none"),
                "document": results["documents"][i]
            }
            for i, ids in enumerate(results["ids"])
        ]

        # Sort — manual first, then db_sync
        entries.sort(key=lambda x: (x["source"] != "manual", x["doc_id"]))

        return {
            "total":   count,
            "entries": entries
        }

    except ValueError:
        # Collection doesn't exist yet — return empty
        return {"total": 0, "entries": []}

    except Exception as e:
        raise HTTPException(
            status_code = 500,
            detail      = f"Could not list shared knowledge: {str(e)}"
        )


@app.post("/shared/add")
async def add_shared_knowledge(req: SharedKnowledgeInput):
    """Add or update a shared knowledge entry."""
    save_to_shared(
        doc_id   = req.doc_id,
        document = req.document,
        metadata = {
            "doc_id":  req.doc_id,
            "caption": req.caption,
            "source":  req.source,
            "table":   req.table or "none"
        }
    )
    return {"status": "saved", "doc_id": req.doc_id}


@app.delete("/shared/{doc_id}")
async def delete_shared_knowledge(doc_id: str):
    """Delete a shared knowledge entry by doc_id."""
    delete_shared_entry(doc_id)
    return {"status": "deleted", "doc_id": doc_id}


@app.get("/shared/search")
async def search_shared(question: str, n_results: int = 3):
    """Test search on shared collection — for debugging."""
    results = query_shared(question, n_results)
    return {"results": results}