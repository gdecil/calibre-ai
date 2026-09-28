import requests
from urllib.parse import urlsplit, urlunsplit

from .config import EMBEDDING_MODEL, OLLAMA_URL

EMBED_MODEL = EMBEDDING_MODEL

def embed_texts(texts, timeout=600):
    r = requests.post(
        OLLAMA_URL,
        json={"model": EMBED_MODEL, "input": texts},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["embeddings"]


def get_model_digest():
    ollama_url = urlsplit(OLLAMA_URL)
    api_path = ollama_url.path.rsplit("/", 1)[0]
    tags_url = urlunsplit(
        (ollama_url.scheme, ollama_url.netloc, f"{api_path}/tags", "", "")
    )
    response = requests.get(tags_url, timeout=30)
    response.raise_for_status()

    model_names = {EMBED_MODEL}
    if ":" not in EMBED_MODEL:
        model_names.add(f"{EMBED_MODEL}:latest")

    model = next(
        (
            item
            for item in response.json().get("models", [])
            if item.get("name") in model_names
        ),
        None,
    )
    if model is None or not model.get("digest"):
        raise RuntimeError(
            f"Digest Ollama non disponibile per il modello {EMBED_MODEL}"
        )

    return model["digest"]
