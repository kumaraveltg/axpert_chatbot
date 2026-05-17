import chromadb
import os
import platform
from dotenv import load_dotenv

load_dotenv()

def _get_chroma_path() -> str:
    # 1. If .env has CHROMA_PATH — use it (highest priority)
    env_path = os.getenv("CHROMA_PATH", "")
    if env_path:
        return env_path

    # 2. Auto-detect based on OS
    if platform.system() == "Windows":
        # Windows — relative to project root E:\axpert_chatbot
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, "vectorstore")
    else:
        # Linux/Ubuntu production — absolute path
        return "/var/www/axpert_chatbot/vectorstore"

CHROMA_PATH = _get_chroma_path()

def get_chroma():
    return chromadb.PersistentClient(path=CHROMA_PATH)

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
    return [c if isinstance(c, str) else c.name for c in cols]

def inspect_collection(name: str, sample: int = 3):
    """Debug helper — peek at stored chunks"""
    col = get_existing_collection(name)
    count = col.count()
    sample_data = col.peek(limit=sample)
    return {
        "collection":        name,
        "total_chunks":      count,
        "sample_documents":  sample_data["documents"],
        "sample_metadatas":  sample_data["metadatas"],
    }