"""
chat_service/main.py
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import os
from groq import Groq
from dotenv import load_dotenv

from sync_service.chromadb_store import query_combined
from shared.chroma_config import get_chroma

load_dotenv()

os.environ["TRANSFORMERS_OFFLINE"] = "1"

app = FastAPI(
    title="Axpert Chat Service",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"]
)

# Groq client
groq_client = Groq(
    api_key=os.getenv("GROQ_API_KEY")
)


# ── Threshold ─────────────────────────────────────────────────

def get_threshold(distances: list) -> float:
    """
    Adaptive threshold — allows anything within 20% of best match.
    """
    if not distances:
        return 0.7
    best = min(distances)
    if best > 1.5:
        return 0.0
    return best * 1.2


# ── Multi-query generation ────────────────────────────────────

def generate_queries(
    question: str,
    module:   Optional[str],
    industry: str
) -> list[str]:
    """
    Use Groq to generate 3 query variations
    to improve ChromaDB retrieval.
    """
    prompt = f"""Generate 3 different search queries 
for this question about {industry} ERP system.
Module: {module or 'General'}
Question: {question}

Rules:
- Each query must be different phrasing
- Keep each query under 10 words
- Focus on Axpert ERP terminology
- Return ONLY 3 lines, no numbering, no explanation

Example output:
attendance recording form steps
rcatt uattm employee attendance entry
mark employee present payroll module"""

    response = groq_client.chat.completions.create(
        model    = "meta-llama/llama-4-scout-17b-16e-instruct",
        messages = [{"role": "user", "content": prompt}],
        temperature = 0.5,
        max_tokens  = 60
    )

    raw = response.choices[0].message.content
    queries = [
        q.strip()
        for q in raw.strip().split("\n")
        if q.strip() and len(q.strip()) > 3
    ][:3]

    if question not in queries:
        queries.insert(0, question)

    print(f"🔍 Queries generated: {queries}")
    return queries


# ── Multi-query search ────────────────────────────────────────

def multi_query_search(
    schema_name:  str,
    queries:      list[str],
    where_filter: Optional[dict],
    n_results:    int = 3
) -> tuple:
    """
    Search ChromaDB with multiple queries using query_combined.
    Merges and deduplicates results across schema + shared collections.
    """
    seen_ids  = set()
    all_docs  = []
    all_metas = []
    all_dists = []

    for query in queries:
        try:
            results = query_combined(
                schema_name      = schema_name,
                question         = query,
                n_results_schema = n_results,
                n_results_shared = 2
            )

            for item in results:
                # Apply module where_filter if set
                if where_filter:
                    source = item["metadata"].get("source", "")
                    if source in ("manual", "db_sync"):
                        pass  # always include shared entries
                    else:
                        meta_module = item["metadata"].get("module", "")
                        if meta_module != where_filter.get("module", ""):
                            continue

                item_id = item["id"]
                if item_id not in seen_ids:
                    seen_ids.add(item_id)
                    all_docs.append(item["document"])
                    all_metas.append(item["metadata"])
                    all_dists.append(item["distance"])

        except Exception as e:
            print(f"⚠️ Query failed: {query} | {e}")
            continue

    if not all_docs:
        return [], [], []

    sorted_results = sorted(
        zip(all_docs, all_metas, all_dists),
        key=lambda x: x[2]
    )

    docs, metas, dists = zip(*sorted_results)
    return list(docs), list(metas), list(dists)


# ── Pydantic models ───────────────────────────────────────────

class ChatRequest(BaseModel):
    question:    str
    industry:    str
    module:      Optional[str] = None
    mode:        Optional[str] = "explain"
    history:     Optional[list] = []
    schema_name: str

class ChatResponse(BaseModel):
    answer:       str
    sources:      list
    practice:     Optional[str] = None
    chunks_used:  Optional[int] = None
    min_distance: Optional[float] = None


# ── Endpoints ─────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "service": "chat_service",
        "status":  "running ✅"
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):

    # Step 1 — build where filter
    where_filter = {"module": req.module} if req.module else None

    # Step 2 — generate multiple queries
    queries = generate_queries(
        question = req.question,
        module   = req.module,
        industry = req.industry
    )

    # Step 3 — multi query search (schema + shared combined)
    raw_docs, raw_metas, raw_distances = multi_query_search(
        schema_name  = req.schema_name,
        queries      = queries,
        where_filter = where_filter,
        n_results    = 3
    )

    if not raw_docs:
        return ChatResponse(
            answer=(
                "I don't have specific information "
                "about that in the knowledge base. "
                "Please make sure the relevant module "
                "has been synced, or rephrase your question."
            ),
            sources      = [],
            practice     = None,
            chunks_used  = 0,
            min_distance = None
        )

    # Step 4 — adaptive threshold filter
    threshold = get_threshold(raw_distances)
    filtered = [
        (doc, meta, dist)
        for doc, meta, dist
        in zip(raw_docs, raw_metas, raw_distances)
        if dist < threshold
    ]

    if not filtered:
        return ChatResponse(
            answer=(
                "I found some results but none were "
                "relevant enough to answer confidently. "
                "Please rephrase or specify the module."
            ),
            sources      = [],
            practice     = None,
            chunks_used  = 0,
            min_distance = round(raw_distances[0], 4)
        )

    docs, metas, distances = zip(*filtered)

    # Step 5 — build context
    context = "\n\n---\n\n".join(docs)

    # Step 6 — system prompt
    if req.mode == "guide":
            system_prompt = """You are an Axpert ERP implementation expert.
        RULES:
        1. Use the KNOWLEDGE BASE below as your primary source.
        2. If knowledge base has relevant info use it to answer fully.
        3. If knowledge base has partial info combine with your 
        general Axpert ERP knowledge to complete the answer.
        4. Do NOT invent TransIDs or field names specific to this system.
        5. Always give clear numbered steps for non-technical users.
        6. If truly no info available say so briefly and suggest 
        checking Axpert admin settings."""
    else:
            system_prompt = """You are an Axpert ERP expert assistant.
        RULES:
        1. Use the KNOWLEDGE BASE below as your primary source.
        2. If knowledge base has relevant info use it to answer fully.
        3. If knowledge base has partial info combine with your
        general Axpert ERP knowledge to complete the answer.
        4. Do NOT invent TransIDs or field names specific to this system.
        5. Use simple non-technical language.
        6. Always give step by step answer when user asks how to do something.
        7. If truly no info available say so briefly and suggest
        checking Axpert admin settings."""

    prompt = f"""Industry : {req.industry}
Module   : {req.module or 'General'}
Question : {req.question}

KNOWLEDGE BASE (answer only from this):
{context}
"""

    # Step 7 — build messages with history
    messages = [
        {"role": "system", "content": system_prompt}
    ]

    for h in (req.history or [])[-6:]:
        messages.append({
            "role":    h["role"],
            "content": h["content"]
        })

    messages.append({
        "role":    "user",
        "content": prompt
    })

    # Step 8 — call Groq
    response = groq_client.chat.completions.create(
        model       = "meta-llama/llama-4-scout-17b-16e-instruct",
        messages    = messages,
        temperature = 0.1,
        max_tokens  = 1000
    )

    usage = response.usage
    print(f"📊 Chat tokens — "
          f"in: {usage.prompt_tokens} | "
          f"out: {usage.completion_tokens} | "
          f"total: {usage.total_tokens}")

    answer = response.choices[0].message.content

    sources = list({
        f"{m.get('module', '')} → {m.get('practice', m.get('caption', ''))}"
        for m in metas
    })
    practice = metas[0].get("practice", "") if metas else None

    return ChatResponse(
        answer       = answer,
        sources      = sources,
        practice     = practice,
        chunks_used  = len(docs),
        min_distance = round(min(distances), 4)
    )


@app.get("/collections")
async def list_collections():
    """List all available knowledge collections"""
    client      = get_chroma()
    collections = client.list_collections()
    return {
        "collections": [c.name for c in collections]
    }


@app.get("/debug/query")
async def debug_query(
    industry:    str,
    question:    str,
    module:      Optional[str] = None,
    schema_name: Optional[str] = None
):
    """
    See exactly what chunks ChromaDB returns and their distances.
    Helps tune DISTANCE_THRESHOLD.
    """
    if not schema_name:
        raise HTTPException(
            status_code = 400,
            detail      = "schema_name is required"
        )

    where_filter = {"module": module} if module else None

    results = query_combined(
        schema_name      = schema_name,
        question         = f"{module} {question}" if module else question,
        n_results_schema = 5,
        n_results_shared = 3
    )

    if where_filter:
        results = [
            r for r in results
            if r["metadata"].get("module") == where_filter["module"]
        ]

    distances  = [r["distance"] for r in results]
    threshold  = get_threshold(distances)

    return {
        "schema_name": schema_name,
        "threshold":   threshold,
        "results": [
            {
                "rank":             i + 1,
                "id":               r["id"],
                "distance":         round(r["distance"], 4),
                "passed_threshold": r["distance"] < threshold,
                "metadata":         r["metadata"],
                "chunk_preview":    r["document"][:200] + "..."
            }
            for i, r in enumerate(results)
        ]
    }


@app.get("/instructions")
async def get_instructions(schema: str, transid: str):
    from shared.database import get_db
    db   = next(get_db())
    rows = db.execute("""
        SELECT fieldname, instruction, created_by
        FROM axpert_chatbot.field_instructions
        WHERE schema_name = :schema
        AND lower(transid) = lower(:transid)
        ORDER BY fieldname
    """, {"schema": schema, "transid": transid}).fetchall()
    return {"instructions": [dict(r) for r in rows]}