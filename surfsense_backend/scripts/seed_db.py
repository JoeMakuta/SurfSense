#!/usr/bin/env python
"""
Seed the database with realistic sample data for development, testing, or demo purposes.

Only works with AUTH_TYPE=LOCAL (email/password). For GOOGLE auth, user creation
is skipped and a warning is printed.

Usage:
    python scripts/seed_db.py [--reset]

Options:
    --reset    Delete all existing seed data before re-seeding (idempotent)
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Allow importing app modules from the backend root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import config
from app.db import (
    Chunk,
    Document,
    DocumentStatus,
    DocumentType,
    SearchSourceConnector,
    SearchSourceConnectorType,
    SearchSpace,
    SearchSpaceMembership,
    SearchSpaceRole,
    User,
    async_session_maker,
    get_default_roles_config,
)
from app.utils.document_converters import (
    create_document_chunks,
    embed_text,
    generate_content_hash,
    generate_unique_identifier_hash,
)

# ── Seed identity ────────────────────────────────────────────────────────────
SEED_EMAIL = "demo@surfsense.com"
SEED_PASSWORD = "demo1234!"
SEED_DISPLAY_NAME = "Demo User"

# Prefix used to identify all seed-created search spaces (for --reset)
SEED_SPACE_PREFIX = "[SEED] "

# ── Sample search spaces ─────────────────────────────────────────────────────
SAMPLE_SPACES = [
    {
        "name": f"{SEED_SPACE_PREFIX}Dev Knowledge Base",
        "description": "A sample search space pre-loaded with development resources.",
        "citations_enabled": True,
        "qna_custom_instructions": "Answer concisely and cite sources when available.",
    },
]

# ── Sample connectors (one per space) ────────────────────────────────────────
SAMPLE_CONNECTORS = [
    {
        "name": "Tavily Web Search",
        "connector_type": SearchSourceConnectorType.TAVILY_API,
        "is_indexable": False,
        "config": {"tavily_api_key": "tvly-REPLACE_ME"},
    },
    {
        "name": "GitHub – SurfSense",
        "connector_type": SearchSourceConnectorType.GITHUB_CONNECTOR,
        "is_indexable": True,
        "config": {
            "token": "ghp_REPLACE_ME",
            "repos": ["MODSetter/SurfSense"],
        },
    },
]

# ── Sample documents ─────────────────────────────────────────────────────────
SAMPLE_DOCUMENTS = [
    {
        "title": "Getting Started with SurfSense",
        "document_type": DocumentType.FILE,
        "document_metadata": {"source": "seed", "url": "https://surfsense.net/docs"},
        "content": (
            "# Getting Started with SurfSense\n\n"
            "SurfSense is an AI-powered knowledge management platform that lets you "
            "search across all your data sources with a single natural-language query.\n\n"
            "## Key Features\n"
            "- Hybrid vector + full-text search\n"
            "- 30+ connector integrations (GitHub, Slack, Notion, Google Drive, …)\n"
            "- Collaborative search spaces with role-based access control\n"
            "- AI chat threads with source citations\n\n"
            "## Quick Start\n"
            "1. Create a search space\n"
            "2. Add connectors and trigger an initial index\n"
            "3. Ask questions in the Chat tab\n"
        ),
    },
    {
        "title": "SurfSense Connector Overview",
        "document_type": DocumentType.FILE,
        "document_metadata": {"source": "seed", "url": "https://surfsense.net/docs/connectors"},
        "content": (
            "# Connector Overview\n\n"
            "Connectors pull data from external services into your search space.\n\n"
            "## Web / Search\n"
            "- **Tavily API** – real-time web search (not indexable)\n"
            "- **SearxNG** – self-hosted meta-search engine\n\n"
            "## Code & Project Management\n"
            "- **GitHub** – repositories, issues, pull requests\n"
            "- **Linear** – issues and projects\n"
            "- **Jira** – tickets and epics\n\n"
            "## Productivity\n"
            "- **Notion** – pages and databases\n"
            "- **Confluence** – spaces and pages\n"
            "- **Google Drive** – documents and spreadsheets\n"
        ),
    },
    {
        "title": "Understanding Search Spaces and RBAC",
        "document_type": DocumentType.FILE,
        "document_metadata": {"source": "seed", "url": "https://surfsense.net/docs/rbac"},
        "content": (
            "# Search Spaces and RBAC\n\n"
            "Every piece of content in SurfSense lives inside a **search space**. "
            "Users can own multiple spaces and invite collaborators.\n\n"
            "## Roles\n"
            "| Role   | Permissions |\n"
            "|--------|-------------|\n"
            "| Owner  | Full access including delete and role management |\n"
            "| Editor | Create and update content; no delete or settings |\n"
            "| Viewer | Read-only access |\n\n"
            "## Invites\n"
            "Owners can generate invite links with a configurable role. "
            "Invitees automatically receive the Editor role unless changed.\n"
        ),
    },
]



# ── Helpers ──────────────────────────────────────────────────────────────────

def _print(msg: str) -> None:
    print(msg, flush=True)


def _ok(msg: str) -> None:
    print(f"  ✔  {msg}", flush=True)


def _skip(msg: str) -> None:
    print(f"  –  {msg} (already exists, skipping)", flush=True)


# ── Reset ────────────────────────────────────────────────────────────────────

async def reset_seed_data(session) -> None:
    """Delete all data that was created by this seeder."""
    _print("\n[reset] Removing existing seed data…")

    result = await session.execute(
        select(SearchSpace).where(SearchSpace.name.like(f"{SEED_SPACE_PREFIX}%"))
    )
    spaces = result.scalars().all()
    for space in spaces:
        await session.delete(space)
        _ok(f"Deleted search space: {space.name!r}")

    result = await session.execute(select(User).where(User.email == SEED_EMAIL))
    user = result.scalar_one_or_none()
    if user:
        await session.delete(user)
        _ok(f"Deleted seed user: {SEED_EMAIL}")

    await session.commit()
    _print("[reset] Done.\n")


# ── User ─────────────────────────────────────────────────────────────────────

async def seed_user(session) -> "User | None":
    """
    Create the demo seed user (LOCAL auth only).
    Returns the User object, or None when running under GOOGLE auth.
    """
    if config.AUTH_TYPE != "LOCAL":
        _print(
            "  ⚠  AUTH_TYPE is not LOCAL — skipping user creation.\n"
            "     Create a user via Google OAuth and re-run to seed spaces/connectors/docs."
        )
        return None

    result = await session.execute(select(User).where(User.email == SEED_EMAIL))
    existing = result.scalar_one_or_none()
    if existing:
        _skip(f"User {SEED_EMAIL}")
        return existing

    from fastapi_users.password import PasswordHelper
    hashed_password = PasswordHelper().hash(SEED_PASSWORD)

    user = User(
        email=SEED_EMAIL,
        hashed_password=hashed_password,
        is_active=True,
        is_verified=True,
        is_superuser=False,
        display_name=SEED_DISPLAY_NAME,
    )
    session.add(user)
    await session.flush()
    _ok(f"Created user: {SEED_EMAIL}  (password: {SEED_PASSWORD})")
    return user


# ── Search Space ─────────────────────────────────────────────────────────────

async def seed_search_space(session, user: "User", space_data: dict) -> SearchSpace:
    """Create a search space with default RBAC roles and owner membership."""
    result = await session.execute(
        select(SearchSpace).where(
            SearchSpace.user_id == user.id,
            SearchSpace.name == space_data["name"],
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        _skip(f"Search space {space_data['name']!r}")
        return existing

    space = SearchSpace(
        name=space_data["name"],
        description=space_data.get("description"),
        citations_enabled=space_data.get("citations_enabled", True),
        qna_custom_instructions=space_data.get("qna_custom_instructions"),
        user_id=user.id,
    )
    session.add(space)
    await session.flush()

    owner_role_id = None
    for role_cfg in get_default_roles_config():
        role = SearchSpaceRole(
            name=role_cfg["name"],
            description=role_cfg["description"],
            permissions=role_cfg["permissions"],
            is_default=role_cfg["is_default"],
            is_system_role=role_cfg["is_system_role"],
            search_space_id=space.id,
        )
        session.add(role)
        await session.flush()
        if role_cfg["name"] == "Owner":
            owner_role_id = role.id

    session.add(SearchSpaceMembership(
        user_id=user.id,
        search_space_id=space.id,
        role_id=owner_role_id,
        is_owner=True,
    ))
    await session.flush()
    _ok(f"Created search space: {space_data['name']!r}  (id={space.id})")
    return space


# ── Connectors ───────────────────────────────────────────────────────────────

async def seed_connectors(session, user: "User", space: SearchSpace) -> list:
    """Create sample connectors for the given search space."""
    created = []
    for conn_data in SAMPLE_CONNECTORS:
        result = await session.execute(
            select(SearchSourceConnector).where(
                SearchSourceConnector.search_space_id == space.id,
                SearchSourceConnector.name == conn_data["name"],
            )
        )
        if result.scalar_one_or_none():
            _skip(f"Connector {conn_data['name']!r}")
            continue

        connector = SearchSourceConnector(
            name=conn_data["name"],
            connector_type=conn_data["connector_type"],
            is_indexable=conn_data["is_indexable"],
            config=conn_data["config"],
            search_space_id=space.id,
            user_id=user.id,
        )
        session.add(connector)
        await session.flush()
        _ok(f"Created connector: {conn_data['name']!r}  ({conn_data['connector_type']})")
        created.append(connector)
    return created


# ── Documents ────────────────────────────────────────────────────────────────

async def seed_documents(session, user: "User", space: SearchSpace) -> list:
    """Create sample documents with embeddings and chunks."""
    created = []
    for doc_data in SAMPLE_DOCUMENTS:
        content = doc_data["content"]
        content_hash = generate_content_hash(content, space.id)

        result = await session.execute(
            select(Document).where(Document.content_hash == content_hash)
        )
        if result.scalar_one_or_none():
            _skip(f"Document {doc_data['title']!r}")
            continue

        unique_hash = generate_unique_identifier_hash(
            doc_data["document_type"],
            doc_data["title"],
            space.id,
        )

        _print(f"  …  Embedding document: {doc_data['title']!r}")
        embedding = await asyncio.to_thread(embed_text, content)
        chunks = await create_document_chunks(content)

        document = Document(
            title=doc_data["title"],
            document_type=doc_data["document_type"],
            document_metadata=doc_data.get("document_metadata", {}),
            content=content,
            content_hash=content_hash,
            unique_identifier_hash=unique_hash,
            embedding=embedding,
            search_space_id=space.id,
            created_by_id=user.id,
            status=DocumentStatus.ready(),
        )
        session.add(document)
        await session.flush()

        for chunk in chunks:
            chunk.document_id = document.id
            session.add(chunk)

        await session.flush()
        _ok(f"Created document: {doc_data['title']!r}  ({len(chunks)} chunks)")
        created.append(document)
    return created


# ── Orchestrator ─────────────────────────────────────────────────────────────

async def seed_all(reset: bool = False) -> None:
    async with async_session_maker() as session:
        if reset:
            await reset_seed_data(session)

        _print("\n── Users ──────────────────────────────────────────")
        user = await seed_user(session)
        if user is None:
            return

        for space_data in SAMPLE_SPACES:
            _print(f"\n── Search Space: {space_data['name']} ──────────────────")
            space = await seed_search_space(session, user, space_data)

            _print("\n── Connectors ─────────────────────────────────────")
            await seed_connectors(session, user, space)

            _print("\n── Documents ──────────────────────────────────────")
            await seed_documents(session, user, space)

        await session.commit()

    _print("\n" + "=" * 52)
    _print("  Seeding complete!")
    _print(f"  Login: {SEED_EMAIL}  /  {SEED_PASSWORD}")
    _print("=" * 52 + "\n")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the SurfSense database with sample data."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing seed data before re-seeding.",
    )
    args = parser.parse_args()

    print("=" * 52)
    print("  SurfSense Database Seeder")
    print("=" * 52)
    asyncio.run(seed_all(reset=args.reset))


if __name__ == "__main__":
    main()
