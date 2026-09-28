from pathlib import Path
import os
from dotenv import load_dotenv
load_dotenv()
CALIBRE_LIBRARY=Path(os.getenv("CALIBRE_LIBRARY",r"C:\path\to\calibre\library"))
DATABASE_PATH=Path(os.getenv("DATABASE_PATH",r"data\calibre.db"))
OLLAMA_URL=os.getenv("OLLAMA_URL", "http://localhost:11434/api/embed")
EMBEDDING_MODEL=os.getenv("EMBEDDING_MODEL", "qwen3-embedding")
QDRANT_URL=os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION=os.getenv("QDRANT_COLLECTION", "calibre_chunks_disk")
