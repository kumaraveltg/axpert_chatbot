import chromadb
import os
from dotenv import load_dotenv

load_dotenv()

def get_chroma():
    return chromadb.PersistentClient(
        path=os.getenv(
            "CHROMA_PATH", 
            "./vectorstore"
        )
    )

def get_collection(name: str):
    client = get_chroma()
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"}
    )

def get_existing_collection(name: str):
    """Use this in CHAT SERVICE — raises error if collection missing"""
    client = get_chroma()
    try:
        return client.get_collection(name=name)
    except Exception:
        raise ValueError(
            f"Collection '{name}' not found in vectorstore. "
            f"Run sync first for this schema."
        )

def list_collections():
    """Debug helper — see what's actually in ChromaDB"""
    client = get_chroma()
    cols = client.list_collections()
    return [c.name for c in cols]

def inspect_collection(name: str, sample: int = 3):
    """Debug helper — peek at stored chunks"""
    col = get_existing_collection(name)
    count = col.count()
    sample_data = col.peek(limit=sample)
    return {
        "collection": name,
        "total_chunks": count,
        "sample_documents": sample_data["documents"],
        "sample_metadatas": sample_data["metadatas"],
    }