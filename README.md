# calibre-ai

Scanner iniziale della libreria Calibre.

## Regole

- La libreria configurata con `CALIBRE_LIBRARY` viene aperta in modalità SQLite **read-only**.
- Non viene modificato alcun file della libreria Calibre.
- Il database locale predefinito viene creato in `data\calibre.db`.
- Per ogni formato viene calcolato SHA-256 quando il file esiste.
- Vengono rilevati file mancanti e gruppi di duplicati per hash.

## Installazione

```powershell
cd C:\path\to\calibre-ai
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copia `.env.example` in `.env` se vuoi personalizzare i percorsi.

## Esecuzione

Dalla cartella del progetto:

```powershell
.\.venv\Scripts\python .\scripts\scan.py
```

Il report viene scritto in:

```text
data\scan-report.json
```

Il database locale in:

```text
data\calibre.db
```
