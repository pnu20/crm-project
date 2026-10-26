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

### Optional: AI chat endpoint

`POST /api/chat` answers questions with the local `Qwen/Qwen2.5-1.5B-Instruct` model. It needs the extra packages (several GB) and about 4 GB of free RAM:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-ai.txt
```

The model downloads on the first start and loads at startup. Send `{"message": "..."}` with the usual `Authorization: Bearer <token>` header; the response is `{"reply": "..."}`. Without these packages the API runs normally and `/api/chat` returns 503.

On a server too small for the local model (such as the 1 GB Lightsail instance), use a hosted Qwen model instead: set `CHAT_API_KEY`, `CHAT_API_BASE_URL` and `CHAT_API_MODEL` in `.env` (see `.env.example`) and restart the API. No extra packages are needed. When none of these work, the chat button is hidden.

To run the Node API separately:

```powershell
npm start
```

It listens on `http://127.0.0.1:3000`.

## withaqChat

The **withaqChat** item in the sidebar opens a guided chat that carries out CRM tasks step by step: adding contacts, deals, and tasks, completing a task, moving a deal to another stage, deleting a contact, and showing a summary. It asks one question at a time, offers the valid answers as buttons, and asks for confirmation before saving or deleting.

It runs entirely in the browser without AI or any external service: it recognizes Arabic and English keywords to pick the task, and saves through the same API routes as the site's forms.

## Sign in

On first launch, create the administrator account. Passwords must be at least 12 characters. After setup, the CRM requires that account to sign in; public account creation is disabled. Sessions expire after 12 hours, and signing out revokes the current session.