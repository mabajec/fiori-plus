from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.importer import import_file
from app.models import User


app = typer.Typer(help="Fiori — project financial data tool.", no_args_is_help=True)


@app.command()
def init() -> None:
    """Create the default admin user (idempotent)."""
    with SessionLocal() as session:
        existing = session.scalar(
            select(User).where(User.email == settings.default_admin_email)
        )
        if existing is not None:
            typer.echo(
                f"Admin user already exists (id={existing.id}, "
                f"email={existing.email})."
            )
            return
        user = User(
            email=settings.default_admin_email,
            name=settings.default_admin_name,
            role="admin",
        )
        session.add(user)
        session.commit()
        typer.echo(
            f"Created admin user id={user.id}, email={user.email}."
        )


@app.command(name="import")
def import_cmd(
    path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False),
    name: Optional[str] = typer.Option(
        None,
        "--name",
        "-n",
        help="Friendly project name (only used if PPS element is new).",
    ),
    user_email: str = typer.Option(
        None,
        "--user",
        help="Owner email (defaults to the configured admin).",
    ),
    encoding: str = typer.Option(
        "mac_latin2",
        "--encoding",
        help="Input file encoding (default: mac_latin2).",
    ),
) -> None:
    """Import a TSV export into the database."""
    email = user_email or settings.default_admin_email

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            typer.echo(
                f"User {email!r} not found. Run `fiori init` first.", err=True
            )
            raise typer.Exit(code=1)

        def name_resolver(pps: str) -> str:
            if name:
                return name
            typer.echo(
                f"First import for project {pps!r}. Enter a friendly name:"
            )
            return typer.prompt("Name")

        result = import_file(
            session=session,
            path=path,
            user_id=user.id,
            name_resolver=name_resolver,
            encoding=encoding,
        )

    if result.duplicate_file:
        typer.echo(
            f"File already imported (sha256={result.file_sha256[:12]}…). "
            f"Project: {result.project_name} ({result.pps_element}). "
            "Nothing to do."
        )
        return

    typer.echo(
        f"Imported {result.rows_imported} new rows "
        f"(skipped {result.rows_skipped} duplicates) into "
        f"{result.project_name} ({result.pps_element})."
    )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(True, "--reload/--no-reload"),
) -> None:
    """Run the web UI."""
    import uvicorn

    uvicorn.run("app.web:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
