from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from pydantic import BaseModel, Field


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


app = FastAPI(title="Fieldnote CRM API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "https://stately-capybara-b8b8f1.netlify.app",
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