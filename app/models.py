from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    password_hash: Mapped[str | None] = mapped_column(String(255))
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    force_password_change: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pps_element: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    annual_data: Mapped[list["ProjectAnnualData"]] = relationship(
        back_populates="project",
        order_by="ProjectAnnualData.year",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "pps_element", name="uq_projects_owner_pps"
        ),
    )


class ProjectAnnualData(Base):
    __tablename__ = "project_annual_data"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    total_budget: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    starting_balance: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))

    project: Mapped[Project] = relationship(back_populates="annual_data")
    budget_lines: Mapped[list["ProjectBudgetLine"]] = relationship(
        back_populates="annual_data",
        order_by="ProjectBudgetLine.position",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("project_id", "year", name="uq_project_annual_data"),
    )


class ProjectBudgetLine(Base):
    __tablename__ = "project_budget_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_annual_data_id: Mapped[int] = mapped_column(
        ForeignKey("project_annual_data.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    account_prefix: Mapped[str | None] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    annual_data: Mapped[ProjectAnnualData] = relationship(
        back_populates="budget_lines"
    )


class ProjectShare(Base):
    __tablename__ = "project_shares"

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    granted_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_number: Mapped[str] = mapped_column(String(64), nullable=False)
    account_code: Mapped[str] = mapped_column(String(32), nullable=False)
    account_text: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    posting_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    employee: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[str | None] = mapped_column(String(64))
    year: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "document_number",
            "account_code",
            "amount",
            "posting_date",
            "employee",
            "text",
            name="uq_transactions_natural_key",
        ),
    )


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL")
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rows_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
