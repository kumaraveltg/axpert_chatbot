# clear_chroma.py
import chromadb
import os
from dotenv import load_dotenv

load_dotenv()

client = chromadb.PersistentClient(
    path=os.getenv("CHROMA_PATH", "./vectorstore")
)

cols = client.list_collections()
print("Existing collections:", [c.name for c in cols])

for col in cols:
    client.delete_collection(col.name)
    print(f"Deleted: {col.name}")

print("Done — vectorstore is clean ✅")