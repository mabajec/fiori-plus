from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from sqlalchemy import select

from app.auth import generate_temp_password, hash_password
from app.config import settings
from app.db import SessionLocal
from app.importer import import_file
from app.models import User


app = typer.Typer(help="Fiori — project financial data tool.", no_args_is_help=True)
user_app = typer.Typer(help="Manage users (admin operations).", no_args_is_help=True)
app.add_typer(user_app, name="user")


@app.command()
def init() -> None:
    """Create the default admin user (idempotent). Generates a temp password
    that must be changed on first login."""
    with SessionLocal() as session:
        existing = session.scalar(
            select(User).where(User.email == settings.default_admin_email)
        )
        if existing is not None:
            typer.echo(
                f"Admin user already exists (id={existing.id}, email={existing.email})."
            )
            return
        temp = generate_temp_password()
        user = User(
            email=settings.default_admin_email,
            name=settings.default_admin_name,
            role="admin",
            password_hash=hash_password(temp),
            force_password_change=True,
        )
        session.add(user)
        session.commit()
        typer.echo(f"Created admin user id={user.id}, email={user.email}.")
        typer.echo("")
        typer.echo(f"  Temporary password:  {temp}")
        typer.echo("")
        typer.echo("Sign in at /login and you'll be prompted to change it.")


# ─── User management subcommands ────────────────────────────────────────────


@user_app.command("create")
def user_create(
    email: str = typer.Argument(..., help="Email address (used as the login)"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Display name"),
    admin: bool = typer.Option(False, "--admin", help="Grant admin role"),
) -> None:
    """Create a user with a temporary password. The user must change it on
    first login, and will be guided through 2FA enrolment after that."""
    email = email.strip().lower()
    with SessionLocal() as session:
        if session.scalar(select(User).where(User.email == email)):
            typer.echo(f"User {email!r} already exists.", err=True)
            raise typer.Exit(code=1)
        temp = generate_temp_password()
        user = User(
            email=email,
            name=name,
            role="admin" if admin else "user",
            password_hash=hash_password(temp),
            force_password_change=True,
        )
        session.add(user)
        session.commit()
        typer.echo(f"Created user id={user.id}, email={user.email}, role={user.role}.")
        typer.echo("")
        typer.echo(f"  Temporary password:  {temp}")
        typer.echo("")
        typer.echo("Share this with the user. They will be prompted to change it.")


@user_app.command("list")
def user_list() -> None:
    """List all users."""
    with SessionLocal() as session:
        users = list(session.scalars(select(User).order_by(User.id)))
    if not users:
        typer.echo("No users.")
        return
    typer.echo(
        f"{'id':>4}  {'email':<35}  {'role':<8}  {'2FA':<5}  {'force-pwd':<10}  name"
    )
    typer.echo("-" * 80)
    for u in users:
        typer.echo(
            f"{u.id:>4}  {u.email:<35}  {u.role:<8}  "
            f"{'yes' if u.totp_secret else 'no':<5}  "
            f"{'yes' if u.force_password_change else 'no':<10}  "
            f"{u.name or ''}"
        )


@user_app.command("reset-password")
def user_reset_password(email: str) -> None:
    """Reset a user's password to a new temporary one (printed once)."""
    email = email.strip().lower()
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            typer.echo(f"User {email!r} not found.", err=True)
            raise typer.Exit(code=1)
        temp = generate_temp_password()
        user.password_hash = hash_password(temp)
        user.force_password_change = True
        session.commit()
        typer.echo(f"Reset password for {email}.")
        typer.echo("")
        typer.echo(f"  Temporary password:  {temp}")
        typer.echo("")
        typer.echo("The user must change it on their next sign in.")


@user_app.command("reset-2fa")
def user_reset_2fa(email: str) -> None:
    """Clear a user's TOTP secret. They will re-enroll on next sign in."""
    email = email.strip().lower()
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            typer.echo(f"User {email!r} not found.", err=True)
            raise typer.Exit(code=1)
        if user.totp_secret is None:
            typer.echo(f"User {email!r} hasn't enrolled 2FA yet. Nothing to do.")
            return
        user.totp_secret = None
        session.commit()
        typer.echo(
            f"Cleared 2FA for {email}. They will be guided through enrolment "
            "again on their next sign in."
        )


@user_app.command("delete")
def user_delete(
    email: str,
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Permanently delete a user. Their projects (and transactions) are deleted too via cascade."""
    email = email.strip().lower()
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            typer.echo(f"User {email!r} not found.", err=True)
            raise typer.Exit(code=1)
        if not yes:
            typer.confirm(
                f"Delete user {email!r} and all their projects/transactions?",
                abort=True,
            )
        session.delete(user)
        session.commit()
        typer.echo(f"Deleted user {email}.")


# ─── Existing commands ──────────────────────────────────────────────────────


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
