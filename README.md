# Withaq CRM

The project is split into a static frontend and a FastAPI backend. The API stores accounts, contacts, deals, tasks, and sessions in PostgreSQL, using the `PG*` settings in `.env` (copy `.env.example` to start). The browser stores the language preference and the current tab's session token.

In production, everything runs on the AWS Lightsail server behind Nginx at `https://withaq.me`: Nginx serves `frontend/` and forwards `/api/` to the FastAPI service, which uses a PostgreSQL database on the same server. The frontend calls `/api` on its own domain, and `http://127.0.0.1:8001/api` when opened on localhost.

## Deploy to Lightsail

On the server, from the project directory:

```bash
git pull
sudo bash scripts/deploy_lightsail.sh
```

The script installs the required packages, creates the local database (copying the data from the previous remote database if `.env` pointed at one), runs the API as the `withaq-api` systemd service on `127.0.0.1:8001`, and configures Nginx. Run it again after each `git pull` to apply updates.

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