# Stash

Tiny LAN webapp for organizing stuff into boxes with photos.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

Then on your phone, open `http://<host-lan-ip>:8000`.

Data: `stash.db` (SQLite) and `uploads/` (photos), both alongside `app.py`.
