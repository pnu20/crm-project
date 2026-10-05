# Fieldnote CRM

The project is split into a static frontend and a FastAPI backend. The API stores accounts, contacts, deals, tasks, and sessions in PostgreSQL, using the `PG*` settings in `.env` (copy `.env.example` to start). The browser stores the language preference and the current tab's session token.

The Node entry point is `backend/app.js`. It uses Express and the PostgreSQL pool in `backend/db.js`; `/api/health` checks the process and `/` (the root URL) verifies the PostgreSQL connection.

## Run locally on Windows

Create the virtual environment and install the backend requirements:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If you have data in the old SQLite database (`backend/crm.sqlite3`), copy it into PostgreSQL once:

```powershell
.\.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py
```

Start the API from the project root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

In a second terminal, serve the frontend:

```powershell
py -m http.server 8000 --bind 127.0.0.1 --directory frontend
```

Open `http://127.0.0.1:8000`. API documentation is available at `http://127.0.0.1:8001/docs`.

To run the Node API separately:

```powershell
npm start
```

It listens on `http://127.0.0.1:3000`.

## Sign in

On first launch, create the administrator account. Passwords must be at least 12 characters. After setup, the CRM requires that account to sign in; public account creation is disabled. Sessions expire after 12 hours, and signing out revokes the current session.