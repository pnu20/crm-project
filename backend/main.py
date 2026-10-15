from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import secrets
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google import genai
from google.genai import errors as genai_errors, types as genai_types
import httpx
import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from pydantic import BaseModel, Field, ValidationError


load_dotenv(Path(__file__).resolve().parent.parent / ".env")
DATABASE_SETTINGS = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "dbname": os.environ.get("PGDATABASE", "postgres"),
    "user": os.environ.get("PGUSER", "postgres"),
    "password": os.environ.get("PGPASSWORD"),
}
# Comma-separated deployed frontend origins, e.g. https://your-site.netlify.app
FRONTEND_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.environ.get("FRONTEND_ORIGINS", "").split(",")
    if origin.strip()
]
PASSWORD_HASH_ITERATIONS = 310_000
SESSION_LIFETIME = timedelta(hours=12)
bearer_scheme = HTTPBearer(auto_error=False)


# Request schema shared by contact creation and replacement.
class ContactInput(BaseModel):
    """Validate the contact fields accepted by create and update routes."""

    firstName: str = Field(min_length=1, max_length=100)
    lastName: str = Field(min_length=1, max_length=100)
    email: str = ""
    phone: str = ""
    company: str = ""
    role: str = ""
    status: Literal["Lead", "Customer", "Partner"] = "Lead"
    lastContact: str = ""
    owner: str = "JD"
    tone: str = "tone-green"


# Request schema for creating a sales opportunity.
class DealInput(BaseModel):
    """Validate deal fields accepted when creating an opportunity."""

    name: str = Field(min_length=1, max_length=200)
    contact: str = ""
    value: float = Field(ge=0)
    stage: Literal["Lead", "Qualified", "Proposal", "Won"] = "Lead"


# Request schema for moving a deal between pipeline stages.
class DealStageInput(BaseModel):
    """Validate a deal's next pipeline stage."""

    stage: Literal["Lead", "Qualified", "Proposal", "Won"]


# Request schema for creating a follow-up task.
class TaskInput(BaseModel):
    """Validate task fields accepted when creating a follow-up."""

    title: str = Field(min_length=1, max_length=300)
    contact: str = ""
    due: str
    done: bool = False


# Request schema for completing or reopening a task.
class TaskStatusInput(BaseModel):
    """Validate the completed state used to update a task."""

    done: bool


# Request data for setting up the first CRM administrator.
class AccountSetupInput(BaseModel):
    """Validate credentials and profile details for the first account."""

    fullName: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=128)


# Request data for authenticating an existing CRM account.
class LoginInput(BaseModel):
    """Validate the credentials used to sign in."""

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


# Request data for editing the signed-in account's profile.
class ProfileInput(BaseModel):
    """Validate the name and email shown in account settings."""

    fullName: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=254)


# Request data for replacing the signed-in account's password.
class PasswordChangeInput(BaseModel):
    """Validate the current password and its replacement."""

    currentPassword: str = Field(min_length=1, max_length=128)
    newPassword: str = Field(min_length=12, max_length=128)


# Hash passwords with a unique salt before storing them in PostgreSQL.
def hash_password(password: str) -> str:
    """Return a salted PBKDF2-SHA256 password hash."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS
    )
    return f"{salt.hex()}:{digest.hex()}"


# Verify credentials without storing or comparing plain-text passwords.
def verify_password(password: str, stored_hash: str) -> bool:
    """Compare a password with its salted PBKDF2-SHA256 hash."""
    try:
        salt_hex, digest_hex = stored_hash.split(":", maxsplit=1)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex),
            PASSWORD_HASH_ITERATIONS,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Create a random bearer token and persist only its SHA-256 digest.
def issue_session(user_id: int, full_name: str, email: str) -> dict[str, object]:
    """Create a time-limited session and return its one-time bearer token."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + SESSION_LIFETIME
    with connect() as connection:
        connection.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
            (token_hash, user_id, expires_at),
        )
    return {"accessToken": token, "tokenType": "bearer", "fullName": full_name, "email": email}


# Resolve and validate a bearer token for protected CRM routes.
def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, object]:
    """Return the signed-in user or reject missing, invalid, and expired sessions."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized

    token_hash = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
    with connect() as connection:
        row = connection.execute(
            """SELECT users.id, users.full_name AS "fullName", users.email,
                      sessions.expires_at
               FROM sessions JOIN users ON users.id = sessions.user_id
               WHERE sessions.token_hash = %s""",
            (token_hash,),
        ).fetchone()
        expired = row is None or row["expires_at"] <= datetime.now(timezone.utc)
        if expired:
            connection.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))
    if expired:
        raise unauthorized
    return {"id": row["id"], "fullName": row["fullName"], "email": row["email"]}


# Create a row-aware connection to the configured PostgreSQL database.
@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Open a PostgreSQL connection that returns rows as dictionaries."""
    connection = psycopg.connect(**DATABASE_SETTINGS, row_factory=dict_row)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# Calculate an ISO date relative to today for sample records.
def date_offset(days: int) -> str:
    """Return an ISO date offset from today for seeded sample records."""
    return (date.today() - timedelta(days=days)).isoformat()


# Ensure tables exist and seed each empty CRM collection.
def initialize_database() -> None:
    """Create the CRM tables and insert sample rows into empty tables."""
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id TEXT PRIMARY KEY,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                last_contact TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT 'JD',
                tone TEXT NOT NULL DEFAULT 'tone-green'
            );
            CREATE TABLE IF NOT EXISTS deals (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                contact TEXT NOT NULL DEFAULT '',
                value DOUBLE PRECISION NOT NULL,
                stage TEXT NOT NULL,
                seq BIGINT GENERATED ALWAYS AS IDENTITY
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                contact TEXT NOT NULL DEFAULT '',
                due TEXT NOT NULL,
                done BOOLEAN NOT NULL DEFAULT FALSE,
                seq BIGINT GENERATED ALWAYS AS IDENTITY
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx ON users (lower(email));
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TIMESTAMPTZ NOT NULL
            );
            """
        )
        if connection.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"] == 0:
            contacts = [
                ("Olivia", "Chen", "olivia@northstar.studio", "+1 (415) 555-0142", "Northstar Studio", "Creative Director", "Customer", 1, "tone-green"),
                ("Marcus", "Reed", "marcus@everwell.co", "+1 (312) 555-0198", "Everwell Health", "VP of Growth", "Lead", 2, "tone-coral"),
                ("Priya", "Shah", "priya@monocle.io", "+1 (646) 555-0180", "Monocle Labs", "Founder", "Partner", 4, "tone-lilac"),
                ("Theo", "Williams", "theo@fieldwork.agency", "+1 (503) 555-0126", "Fieldwork Agency", "Managing Partner", "Lead", 5, "tone-blue"),
                ("Sofia", "Martinez", "sofia@juniperhome.com", "+1 (512) 555-0155", "Juniper Home", "Head of Marketing", "Customer", 8, "tone-coral"),
                ("Ethan", "Brooks", "ethan@commonthread.com", "+1 (206) 555-0171", "Common Thread", "Co-founder", "Lead", 10, "tone-green"),
                ("Amara", "Okafor", "amara@brightpath.org", "+1 (617) 555-0133", "Brightpath", "Partnerships Lead", "Partner", 12, "tone-blue"),
            ]
            connection.cursor().executemany(
                """INSERT INTO contacts
                   (id, first_name, last_name, email, phone, company, role, status,
                    last_contact, owner, tone)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'JD', %s)""",
                [
                    (f"c{index}", *contact[:7], date_offset(contact[7]), contact[8])
                    for index, contact in enumerate(contacts, start=1)
                ],
            )

        if connection.execute("SELECT COUNT(*) AS n FROM deals").fetchone()["n"] == 0:
            connection.cursor().executemany(
                "INSERT INTO deals (id, name, contact, value, stage) VALUES (%s, %s, %s, %s, %s)",
                [
                    ("d1", "Brand refresh", "Olivia Chen", 24000, "Proposal"),
                    ("d2", "Growth strategy", "Marcus Reed", 18000, "Qualified"),
                    ("d3", "Website redesign", "Theo Williams", 32000, "Lead"),
                    ("d4", "Q4 campaign", "Sofia Martinez", 12500, "Won"),
                    ("d5", "Partnership launch", "Priya Shah", 28000, "Qualified"),
                    ("d6", "Content system", "Ethan Brooks", 15000, "Lead"),
                ],
            )

        if connection.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 0:
            connection.cursor().executemany(
                "INSERT INTO tasks (id, title, contact, due, done) VALUES (%s, %s, %s, %s, %s)",
                [
                    ("t1", "Send revised proposal to Olivia", "Olivia Chen", date_offset(0), False),
                    ("t2", "Follow up on discovery call", "Marcus Reed", date_offset(-1), False),
                    ("t3", "Share the Q4 campaign results", "Sofia Martinez", date_offset(0), True),
                    ("t4", "Book a partner catch-up", "Priya Shah", date_offset(-2), False),
                    ("t5", "Send introduction to the team", "Amara Okafor", date_offset(1), False),
                ],
            )


@asynccontextmanager
# Prepare persistent storage during FastAPI startup.
async def lifespan(_: FastAPI):
    """Initialize persistent storage before the API begins serving requests."""
    initialize_database()
    yield


app = FastAPI(title="Withaq CRM API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "https://stately-capybara-b8b8f1.netlify.app",
        "https://withaq.me",
        "https://www.withaq.me",
        *FRONTEND_ORIGINS,
    ],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/api/health")
# Expose a lightweight endpoint for server health checks.
def health() -> dict[str, str]:
    """Report that the CRM API process is available."""
    return {"status": "ok"}


@app.get("/api/auth/setup-status")
# Allow the frontend to choose first-run setup or the login form.
def auth_setup_status() -> dict[str, bool]:
    """Report whether the first administrator account still needs setup."""
    with connect() as connection:
        user_count = connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    return {"requiresSetup": user_count == 0}


@app.post("/api/auth/setup", status_code=status.HTTP_201_CREATED)
# Permit account creation only while the CRM has no users.
def setup_account(payload: AccountSetupInput) -> dict[str, object]:
    """Create the first administrator and issue its initial session."""
    email = payload.email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=422, detail="Enter a valid email address")

    try:
        with connect() as connection:
            connection.execute("LOCK TABLE users IN EXCLUSIVE MODE")
            user_count = connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            if user_count:
                raise HTTPException(status_code=409, detail="Account setup is complete")
            user_id = connection.execute(
                """INSERT INTO users (full_name, email, password_hash, created_at)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (
                    payload.fullName.strip(),
                    email,
                    hash_password(payload.password),
                    datetime.now(timezone.utc),
                ),
            ).fetchone()["id"]
    except psycopg.errors.UniqueViolation as error:
        raise HTTPException(status_code=409, detail="An account already exists") from error

    return issue_session(user_id, payload.fullName.strip(), email)


@app.post("/api/auth/login")
# Authenticate an existing account and issue a short-lived bearer token.
def login(payload: LoginInput) -> dict[str, object]:
    """Verify credentials and create a new authenticated session."""
    with connect() as connection:
        user = connection.execute(
            """SELECT id, full_name, email, password_hash FROM users
               WHERE lower(email) = lower(%s)""",
            (payload.email.strip(),),
        ).fetchone()
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return issue_session(user["id"], user["full_name"], user["email"])


@app.get("/api/auth/me")
# Return the current account for an authenticated frontend session.
def current_account(user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    """Return the profile associated with the caller's valid session."""
    return user


@app.put("/api/auth/me")
# Update the signed-in account's display name and sign-in email.
def update_account(
    payload: ProfileInput, user: dict[str, object] = Depends(require_user)
) -> dict[str, object]:
    """Save profile changes and return the updated account."""
    email = payload.email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    full_name = payload.fullName.strip()
    try:
        with connect() as connection:
            connection.execute(
                "UPDATE users SET full_name = %s, email = %s WHERE id = %s",
                (full_name, email, user["id"]),
            )
    except psycopg.errors.UniqueViolation as error:
        raise HTTPException(status_code=409, detail="Email is already in use") from error
    return {"id": user["id"], "fullName": full_name, "email": email}


@app.post("/api/auth/change-password", status_code=status.HTTP_204_NO_CONTENT)
# Replace the password after confirming the current one.
def change_password(
    payload: PasswordChangeInput,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    user: dict[str, object] = Depends(require_user),
) -> Response:
    """Store the new password hash and revoke every other session."""
    token_hash = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
    with connect() as connection:
        stored = connection.execute(
            "SELECT password_hash FROM users WHERE id = %s", (user["id"],)
        ).fetchone()
        if stored is None or not verify_password(payload.currentPassword, stored["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        connection.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (hash_password(payload.newPassword), user["id"]),
        )
        connection.execute(
            "DELETE FROM sessions WHERE user_id = %s AND token_hash <> %s",
            (user["id"], token_hash),
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
# Revoke the active session token.
def logout(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    _: dict[str, object] = Depends(require_user),
) -> Response:
    """Delete the caller's session so its bearer token can no longer be used."""
    token_hash = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
    with connect() as connection:
        connection.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/contacts")
# List contacts in the camel-case format consumed by the frontend.
def list_contacts(_: dict[str, object] = Depends(require_user)) -> list[dict[str, object]]:
    """Return contacts in the camel-case shape expected by the frontend."""
    with connect() as connection:
        rows = connection.execute(
            """SELECT id, first_name AS "firstName", last_name AS "lastName", email,
                      phone, company, role, status, last_contact AS "lastContact",
                      owner, tone
               FROM contacts ORDER BY last_contact DESC, lower(last_name)"""
        ).fetchall()
    return rows


@app.post("/api/contacts", status_code=status.HTTP_201_CREATED)
# Validate and create a contact record.
def create_contact(
    payload: ContactInput, _: dict[str, object] = Depends(require_user)
) -> dict[str, object]:
    """Persist and return a newly created contact."""
    contact_id = uuid4().hex
    values = payload.model_dump()
    with connect() as connection:
        connection.execute(
            """INSERT INTO contacts
               (id, first_name, last_name, email, phone, company, role, status,
                last_contact, owner, tone)
               VALUES (%(id)s, %(first_name)s, %(last_name)s, %(email)s, %(phone)s,
                       %(company)s, %(role)s, %(status)s, %(last_contact)s, %(owner)s,
                       %(tone)s)""",
            {
                "id": contact_id,
                "first_name": values["firstName"],
                "last_name": values["lastName"],
                "last_contact": values["lastContact"],
                **{key: values[key] for key in ("email", "phone", "company", "role", "status", "owner", "tone")},
            },
        )
    return {"id": contact_id, **values}


@app.put("/api/contacts/{contact_id}")
# Replace an existing contact and return 404 for unknown identifiers.
def update_contact(
    contact_id: str,
    payload: ContactInput,
    _: dict[str, object] = Depends(require_user),
) -> dict[str, object]:
    """Replace an existing contact's editable fields and return the result."""
    values = payload.model_dump()
    with connect() as connection:
        result = connection.execute(
            """UPDATE contacts SET first_name=%(first_name)s, last_name=%(last_name)s,
                      email=%(email)s, phone=%(phone)s, company=%(company)s, role=%(role)s,
                      status=%(status)s, last_contact=%(last_contact)s, owner=%(owner)s,
                      tone=%(tone)s
               WHERE id=%(id)s""",
            {
                "id": contact_id,
                "first_name": values["firstName"],
                "last_name": values["lastName"],
                "last_contact": values["lastContact"],
                **{key: values[key] for key in ("email", "phone", "company", "role", "status", "owner", "tone")},
            },
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"id": contact_id, **values}


@app.delete("/api/contacts/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
# Remove one contact by its unique identifier.
def delete_contact(
    contact_id: str, _: dict[str, object] = Depends(require_user)
) -> Response:
    """Delete one contact, returning 404 when its identifier is unknown."""
    with connect() as connection:
        result = connection.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Contact not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/deals")
# List deals in their most recently inserted order.
def list_deals(_: dict[str, object] = Depends(require_user)) -> list[dict[str, object]]:
    """Return all opportunities ordered by their insertion time."""
    with connect() as connection:
        rows = connection.execute(
            "SELECT id, name, contact, value, stage FROM deals ORDER BY seq DESC"
        ).fetchall()
    return rows


@app.post("/api/deals", status_code=status.HTTP_201_CREATED)
# Validate and create a deal in the pipeline.
def create_deal(
    payload: DealInput, _: dict[str, object] = Depends(require_user)
) -> dict[str, object]:
    """Persist and return a new opportunity in the sales pipeline."""
    deal_id = uuid4().hex
    values = payload.model_dump()
    with connect() as connection:
        connection.execute(
            "INSERT INTO deals (id, name, contact, value, stage) VALUES (%s, %s, %s, %s, %s)",
            (deal_id, values["name"], values["contact"], values["value"], values["stage"]),
        )
    return {"id": deal_id, **values}


@app.patch("/api/deals/{deal_id}/stage")
# Update a deal's pipeline stage and return the stored record.
def update_deal_stage(
    deal_id: str,
    payload: DealStageInput,
    _: dict[str, object] = Depends(require_user),
) -> dict[str, object]:
    """Move a deal to the requested stage and return its current record."""
    with connect() as connection:
        result = connection.execute(
            "UPDATE deals SET stage = %s WHERE id = %s", (payload.stage, deal_id)
        )
        row = connection.execute(
            "SELECT id, name, contact, value, stage FROM deals WHERE id = %s", (deal_id,)
        ).fetchone()
    if result.rowcount == 0 or row is None:
        raise HTTPException(status_code=404, detail="Deal not found")
    return row


@app.get("/api/tasks")
# List tasks ordered by due date.
def list_tasks(_: dict[str, object] = Depends(require_user)) -> list[dict[str, object]]:
    """Return all tasks ordered by due date, newest first within a day."""
    with connect() as connection:
        rows = connection.execute(
            "SELECT id, title, contact, due, done FROM tasks ORDER BY due, seq DESC"
        ).fetchall()
    return rows


@app.post("/api/tasks", status_code=status.HTTP_201_CREATED)
# Validate and create a follow-up task.
def create_task(
    payload: TaskInput, _: dict[str, object] = Depends(require_user)
) -> dict[str, object]:
    """Persist and return a follow-up task."""
    task_id = uuid4().hex
    values = payload.model_dump()
    with connect() as connection:
        connection.execute(
            "INSERT INTO tasks (id, title, contact, due, done) VALUES (%s, %s, %s, %s, %s)",
            (task_id, values["title"], values["contact"], values["due"], values["done"]),
        )
    return {"id": task_id, **values}


@app.patch("/api/tasks/{task_id}")
# Set a task's completion state and return its updated fields.
def update_task_status(
    task_id: str,
    payload: TaskStatusInput,
    _: dict[str, object] = Depends(require_user),
) -> dict[str, object]:
    """Set a task's completion state and return the updated record."""
    with connect() as connection:
        result = connection.execute(
            "UPDATE tasks SET done = %s WHERE id = %s", (payload.done, task_id)
        )
        row = connection.execute(
            "SELECT id, title, contact, due, done FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
    if result.rowcount == 0 or row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


# withaqChat: a Gemini agent that carries out CRM tasks through conversation.
# Override with GEMINI_MODEL in .env; the default alias always points at the newest Flash model.
CHAT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
CHAT_MAX_TOOL_ROUNDS = 10
CHAT_CONVERSATION_LIFETIME = timedelta(hours=12)
CHAT_MAX_CONVERSATIONS = 500
CONTACT_STATUSES = ["Lead", "Customer", "Partner"]
DEAL_STAGES = ["Lead", "Qualified", "Proposal", "Won"]
CONTACT_TONES = ["tone-green", "tone-coral", "tone-blue", "tone-lilac"]
# Tools that change CRM records; the frontend reloads its data after any of them run.
CHAT_WRITE_TOOLS = {
    "create_contact", "update_contact", "delete_contact",
    "create_deal", "update_deal_stage", "create_task", "set_task_done",
}
CONTACT_FIELDS = {
    "firstName": {"type": "string", "description": "First name."},
    "lastName": {"type": "string", "description": "Last name."},
    "email": {"type": "string", "description": "Email address."},
    "phone": {"type": "string", "description": "Phone number."},
    "company": {"type": "string", "description": "Company name."},
    "role": {"type": "string", "description": "Job title."},
    "status": {"type": "string", "enum": CONTACT_STATUSES, "description": "Relationship status."},
    "lastContact": {"type": "string", "description": "Date of the last contact, YYYY-MM-DD."},
}
CHAT_TOOLS = [
    {
        "name": "list_contacts",
        "description": "List CRM contacts. Use it to find a contact's id before editing or deleting, to check for duplicates before creating, and to answer questions about contacts.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional text matched against any contact field (name, company, email, ...)."},
                "status": {"type": "string", "enum": CONTACT_STATUSES, "description": "Optional status filter."},
            },
        },
    },
    {
        "name": "create_contact",
        "description": "Create a contact. firstName and lastName are required; status defaults to Lead and lastContact to today.",
        "parameters": {
            "type": "object",
            "properties": CONTACT_FIELDS,
            "required": ["firstName", "lastName"],
        },
    },
    {
        "name": "update_contact",
        "description": "Change fields on an existing contact. Pass only the fields that change; the rest are kept.",
        "parameters": {
            "type": "object",
            "properties": {"contactId": {"type": "string", "description": "The contact's id from list_contacts."}, **CONTACT_FIELDS},
            "required": ["contactId"],
        },
    },
    {
        "name": "delete_contact",
        "description": "Permanently delete a contact. Only call this after the user has explicitly confirmed the deletion of that specific contact.",
        "parameters": {
            "type": "object",
            "properties": {"contactId": {"type": "string", "description": "The contact's id from list_contacts."}},
            "required": ["contactId"],
        },
    },
    {
        "name": "list_deals",
        "description": "List deals (sales opportunities) with their stage and value in USD.",
        "parameters": {
            "type": "object",
            "properties": {"stage": {"type": "string", "enum": DEAL_STAGES, "description": "Optional stage filter."}},
        },
    },
    {
        "name": "create_deal",
        "description": "Create a deal. The value is stored in USD.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Deal name, e.g. Website redesign."},
                "contact": {"type": "string", "description": "Full name of the related contact, as stored in the CRM. Empty if none."},
                "valueUsd": {"type": "number", "description": "Deal value in US dollars (convert SAR by dividing by 3.75)."},
                "stage": {"type": "string", "enum": DEAL_STAGES, "description": "Pipeline stage; defaults to Lead."},
            },
            "required": ["name", "valueUsd"],
        },
    },
    {
        "name": "update_deal_stage",
        "description": "Move a deal to another pipeline stage.",
        "parameters": {
            "type": "object",
            "properties": {
                "dealId": {"type": "string", "description": "The deal's id from list_deals."},
                "stage": {"type": "string", "enum": DEAL_STAGES},
            },
            "required": ["dealId", "stage"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List follow-up tasks with their due date and completion state.",
        "parameters": {
            "type": "object",
            "properties": {"state": {"type": "string", "enum": ["open", "done", "all"], "description": "Which tasks to return; defaults to all."}},
        },
    },
    {
        "name": "create_task",
        "description": "Create a follow-up task.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What needs to be done."},
                "due": {"type": "string", "description": "Due date, YYYY-MM-DD."},
                "contact": {"type": "string", "description": "Full name of the related contact, as stored in the CRM. Empty if none."},
            },
            "required": ["title", "due"],
        },
    },
    {
        "name": "set_task_done",
        "description": "Mark a task as completed, or reopen it.",
        "parameters": {
            "type": "object",
            "properties": {
                "taskId": {"type": "string", "description": "The task's id from list_tasks."},
                "done": {"type": "boolean", "description": "true to complete the task, false to reopen it."},
            },
            "required": ["taskId", "done"],
        },
    },
]
CHAT_SYSTEM_PROMPT = """You are withaqChat, the assistant inside Withaq CRM. You help {user_name} manage their contacts, deals, and tasks by carrying out the work with your tools, through conversation.

How to work:
- When the user asks for something, work out which details the task needs. If a required detail is missing, ask for it in a short, friendly message; ask for everything that is missing at once rather than one field per message. Mention useful optional details once, and don't insist on them.
- Once you have what you need, do the task with your tools, then confirm in one or two sentences what you did.
- Look things up instead of asking for them: use the list tools to find ids, to check whether a contact already exists before creating one, and to answer questions about the data. Never show ids to the user.
- If a name matches more than one record, ask which one they mean.
- Before deleting a contact, ask the user to confirm, naming the contact, and delete only after they say yes.
- Deal values are stored in US dollars. The interface shows amounts in {currency_label}. If the user gives an amount without a currency, treat it as {default_currency}. Convert SAR to USD by dividing by 3.75, and report amounts back to the user in {default_currency}.
- Resolve relative dates ("tomorrow", "next Monday") against today's date and store dates as YYYY-MM-DD.
- Status, stage, and field values must use the English values the tools accept (Lead, Customer, Partner; Lead, Qualified, Proposal, Won), but talk to the user in their own words.
- If a request is outside what the CRM tools can do, say so briefly.
- Reply in the language the user writes in (Arabic or English). Keep replies short and conversational; use plain text without Markdown headings or tables.

Today's date is {today}."""
chat_conversations: dict[str, dict[str, Any]] = {}
chat_lock = threading.Lock()
chat_client: genai.Client | None = None
CHAT_CONFIG_ERROR = "withaqChat is not configured. Set GEMINI_API_KEY on the server."


# Request data for one user turn in a withaqChat conversation.
class ChatInput(BaseModel):
    """Validate a chat message and the conversation it continues."""

    message: str = Field(min_length=1, max_length=4000)
    conversationId: str | None = Field(default=None, max_length=64)
    language: Literal["en", "ar"] = "en"


# Create the Gemini client on first use so the API starts without chat credentials.
def get_chat_client() -> genai.Client:
    """Return the shared Gemini client, reading GEMINI_API_KEY from the environment."""
    global chat_client
    if chat_client is None:
        try:
            chat_client = genai.Client()
        except ValueError as error:
            raise HTTPException(status_code=503, detail=CHAT_CONFIG_ERROR) from error
    return chat_client


# Generation settings shared by every withaqChat request in a conversation.
def chat_config(system: str) -> genai_types.GenerateContentConfig:
    """Return the Gemini config with the CRM tools and the conversation's system prompt."""
    return genai_types.GenerateContentConfig(
        system_instruction=system,
        tools=[genai_types.Tool(function_declarations=[
            genai_types.FunctionDeclaration(
                name=tool["name"], description=tool["description"],
                parameters_json_schema=tool["parameters"],
            )
            for tool in CHAT_TOOLS
        ])],
        # Tool calls are executed by the loop in chat(), not by the SDK.
        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
    )


# Return the caller's conversation, or start a new one with a frozen system prompt.
def get_conversation(
    conversation_id: str | None, user: dict[str, object], language: str
) -> tuple[str, dict[str, Any]]:
    """Look up an existing conversation for this user or create a new one."""
    now = datetime.now(timezone.utc)
    with chat_lock:
        for key in [key for key, value in chat_conversations.items()
                    if now - value["updatedAt"] > CHAT_CONVERSATION_LIFETIME]:
            del chat_conversations[key]
        conversation = chat_conversations.get(conversation_id or "")
        if conversation is not None and conversation["userId"] == user["id"]:
            return conversation_id, conversation
        if len(chat_conversations) >= CHAT_MAX_CONVERSATIONS:
            oldest = min(chat_conversations, key=lambda key: chat_conversations[key]["updatedAt"])
            del chat_conversations[oldest]
        arabic = language == "ar"
        conversation = {
            "userId": user["id"],
            "updatedAt": now,
            "messages": [],
            # The system prompt stays fixed for the life of the conversation so its history stays valid.
            "system": CHAT_SYSTEM_PROMPT.format(
                user_name=user["fullName"],
                currency_label="Saudi riyals (SAR)" if arabic else "US dollars (USD)",
                default_currency="SAR" if arabic else "USD",
                today=date.today().isoformat(),
            ),
        }
        new_id = uuid4().hex
        chat_conversations[new_id] = conversation
        return new_id, conversation


# Find one record by id, raising a readable error for the model when it is unknown.
def find_record(rows: list[dict[str, object]], record_id: str, label: str) -> dict[str, object]:
    """Return the row with the given id."""
    for row in rows:
        if row["id"] == record_id:
            return row
    raise LookupError(f"No {label} with id {record_id}. Look it up again with the list tool.")


# Run one CRM tool call through the same functions the REST routes use.
def run_chat_tool(name: str, args: dict[str, Any], user: dict[str, object]) -> object:
    """Execute a withaqChat tool and return its JSON-serializable result."""
    if name == "list_contacts":
        query = str(args.get("query") or "").strip().lower()
        return [
            contact for contact in list_contacts(user)
            if (not args.get("status") or contact["status"] == args["status"])
            and (not query or query in " ".join(str(value) for value in contact.values()).lower())
        ]
    if name == "create_contact":
        values = {"lastContact": date.today().isoformat(), **args}
        return create_contact(ContactInput(**values, tone=random.choice(CONTACT_TONES)), user)
    if name == "update_contact":
        contact_id = args.pop("contactId")
        existing = find_record(list_contacts(user), contact_id, "contact")
        merged = {key: value for key, value in existing.items() if key != "id"} | args
        return update_contact(contact_id, ContactInput(**merged), user)
    if name == "delete_contact":
        delete_contact(args["contactId"], user)
        return {"deleted": args["contactId"]}
    if name == "list_deals":
        return [deal for deal in list_deals(user) if not args.get("stage") or deal["stage"] == args["stage"]]
    if name == "create_deal":
        return create_deal(DealInput(
            name=args["name"], contact=args.get("contact", ""),
            value=round(float(args["valueUsd"]), 2), stage=args.get("stage", "Lead"),
        ), user)
    if name == "update_deal_stage":
        return update_deal_stage(args["dealId"], DealStageInput(stage=args["stage"]), user)
    if name == "list_tasks":
        state = args.get("state", "all")
        return [task for task in list_tasks(user)
                if state == "all" or task["done"] == (state == "done")]
    if name == "create_task":
        date.fromisoformat(args["due"])
        return create_task(TaskInput(
            title=args["title"], due=args["due"], contact=args.get("contact", ""), done=False,
        ), user)
    if name == "set_task_done":
        return update_task_status(args["taskId"], TaskStatusInput(done=bool(args["done"])), user)
    raise LookupError(f"Unknown tool {name}")


# Run one function call and wrap its output or error as a function response for Gemini.
def chat_tool_result(call: genai_types.FunctionCall, user: dict[str, object]) -> tuple[genai_types.Part, bool]:
    """Execute a function call and return the response part and whether it failed."""
    try:
        result, failed = {"result": run_chat_tool(call.name, dict(call.args or {}), user)}, False
    except HTTPException as error:
        result, failed = {"error": str(error.detail)}, True
    except ValidationError as error:
        result, failed = {"error": "Invalid input: " + "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}" for issue in error.errors()
        )}, True
    except (LookupError, KeyError, TypeError, ValueError) as error:
        result, failed = {"error": f"Could not run {call.name}: {error}"}, True
    # Round-trip through JSON so dates and other values arrive as plain strings.
    response = json.loads(json.dumps(result, default=str))
    part = genai_types.Part(function_response=genai_types.FunctionResponse(id=call.id, name=call.name, response=response))
    return part, failed


@app.post("/api/chat")
# Continue a withaqChat conversation, running CRM tools until Gemini has a reply.
def chat(payload: ChatInput, user: dict[str, object] = Depends(require_user)) -> dict[str, object]:
    """Send the user's message to Gemini, execute requested CRM tools, and return the reply."""
    conversation_id, conversation = get_conversation(payload.conversationId, user, payload.language)
    messages = conversation["messages"]
    turn_start = len(messages)
    messages.append(genai_types.Content(role="user", parts=[genai_types.Part(text=payload.message.strip())]))
    changed = False
    try:
        client = get_chat_client()
        config = chat_config(conversation["system"])
        for _ in range(CHAT_MAX_TOOL_ROUNDS):
            response = client.models.generate_content(model=CHAT_MODEL, contents=messages, config=config)
            candidate = response.candidates[0] if response.candidates else None
            if candidate is None or candidate.content is None or not candidate.content.parts:
                # Blocked by safety filters or an empty answer: drop the turn so the history stays valid.
                del messages[turn_start:]
                reply = ("لا أستطيع المساعدة في هذا الطلب. جرّب صياغة أخرى." if payload.language == "ar"
                         else "I can't help with that request. Try rephrasing it.")
                return {"conversationId": conversation_id, "reply": reply, "changed": changed}
            # Keep the model's content unchanged (thought signatures included) for the next request.
            messages.append(candidate.content)
            calls = response.function_calls or []
            if not calls:
                conversation["updatedAt"] = datetime.now(timezone.utc)
                return {"conversationId": conversation_id, "reply": (response.text or "").strip(), "changed": changed}
            results = [chat_tool_result(call, user) for call in calls]
            changed = changed or any(
                call.name in CHAT_WRITE_TOOLS and not failed for call, (_, failed) in zip(calls, results)
            )
            messages.append(genai_types.Content(role="user", parts=[part for part, _ in results]))
        raise HTTPException(status_code=502, detail="The assistant did not finish the task. Try again.")
    except HTTPException:
        del messages[turn_start:]
        raise
    except genai_errors.ClientError as error:
        del messages[turn_start:]
        if error.code == 429 and "free_tier" in str(error):
            raise HTTPException(status_code=429, detail="withaqChat reached the Gemini free-tier limit. Wait a minute and try again.") from error
        if error.code == 429:
            raise HTTPException(status_code=429, detail="The assistant is busy. Try again in a moment.") from error
        if error.code in (401, 403) or "API key" in str(error):
            raise HTTPException(status_code=503, detail=CHAT_CONFIG_ERROR) from error
        raise HTTPException(status_code=502, detail="The assistant service rejected the request.") from error
    except genai_errors.ServerError as error:
        del messages[turn_start:]
        if error.code == 503:
            raise HTTPException(status_code=503, detail="The assistant is busy. Try again in a moment.") from error
        raise HTTPException(status_code=502, detail="The assistant service returned an error.") from error
    except genai_errors.APIError as error:
        del messages[turn_start:]
        raise HTTPException(status_code=502, detail="The assistant service returned an error.") from error
    except httpx.HTTPError as error:
        del messages[turn_start:]
        raise HTTPException(status_code=502, detail="Could not reach the assistant service.") from error
