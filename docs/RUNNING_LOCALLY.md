# Local developer setup

**This is not a verified one-click demo.** The commands below follow the repository's package layouts and may require additional environment-specific configuration.

## Backend
Python 3.12 or later:

```bash
cd backend
python -m venv .venv
# Activate the virtual environment for your operating system
pip install -e .
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## Frontend
Use a Node.js version compatible with this version of Vite:

```bash
cd frontend
npm ci
npm run dev
```

Inspect `frontend/vite.config.ts` and `backend/app/core/config.py` for proxy and application settings. Provider integrations may require your own credentials that must **never** be committed. Some flows require local database fixtures, authenticated user state or provider availability.

Test source is provided, but this export has **not** been end-to-end validated. `experiments/revolution/` is independent and should not be copied into the mainline without integration and testing.
