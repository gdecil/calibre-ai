# Calibre AI — Indexer V1

Pipeline:

SQLite `extracted_chunks` -> Ollama `qwen3-embedding` -> Qdrant

## 1. Dipendenze

Nel virtualenv del progetto:

    pip install -r requirements-indexer.txt

## 2. Modello embedding

Con Ollama:

    ollama pull qwen3-embedding

## 3. Qdrant

Da `C:\sw\calibre-ai`:

    docker compose -f docker-compose.qdrant.yml up -d

Verifica:

    Invoke-WebRequest http://localhost:6333/collections

## 4. Configurazione

Le variabili possono essere impostate nel terminale oppure nel `.env` del progetto, se `app.config` le carica già.

Minimo:

    QDRANT_URL=http://localhost:6333
    QDRANT_COLLECTION=calibre_chunks_disk
    EMBEDDING_MODEL=qwen3-embedding

    La collection `calibre_chunks_disk` e' nuova rispetto a quella precedente:
    consente di ripartire senza cancellare i checkpoint SQLite, che sono
    associati al nome della collection. I vettori e l'indice HNSW vengono
    configurati su disco; anche il payload viene mantenuto su disco.

    I payload Qdrant contengono solo gli ID e i metadati. Il testo completo
    resta in SQLite e viene caricato solo per i risultati della ricerca.
    L'indexer aspetta la conferma sincrona di Qdrant prima di registrare il
    checkpoint in SQLite.

## 5. Test iniziale

Non partire subito con 1,3 milioni di chunk.

Prima:

    python -m scripts.index --limit 1000 --batch-size 16 --qdrant-batch-size 16

Poi controllare:

    python -m scripts.index --limit 1000 --batch-size 16 --qdrant-batch-size 16

La seconda esecuzione deve risultare quasi interamente `cache hit`.

## Aggiornamento metadati Qdrant

Dopo l'indicizzazione completa, aggiornare i payload dei punti già presenti
senza ricalcolare gli embedding:

    python -m scripts.backfill_qdrant_payload

Lo script richiede Qdrant attivo, ma non Ollama. Aggiunge o aggiorna
`book_id`, `chapter_id`, titolo del capitolo, titolo del libro e autore.

## 6. Indicizzazione completa

Dopo il test:

    python -m scripts.index --batch-size 16 --qdrant-batch-size 16

L'indice è riprendibile: i chunk con lo stesso `chunk_hash`, modello e collection vengono saltati.

Nota: la V1 non elimina automaticamente da Qdrant punti relativi a chunk cancellati da SQLite. La sincronizzazione distruttiva verrà aggiunta in una fase successiva.
