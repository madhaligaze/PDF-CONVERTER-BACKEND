from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base


class SessionRecord(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parser_key: Mapped[str] = mapped_column(String(128), index=True)
    source_filename: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    payload: Mapped[dict] = mapped_column(JSON)


class PreferenceRecordDB(Base):
    __tablename__ = "preferences"

    parser_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    payload: Mapped[dict] = mapped_column(JSON)


class TransformationTemplateRecord(Base):
    __tablename__ = "transformation_templates"

    template_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parser_key: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    payload: Mapped[dict] = mapped_column(JSON)


class OCRReviewRecord(Base):
    __tablename__ = "ocr_reviews"

    review_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_filename: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    payload: Mapped[dict] = mapped_column(JSON)


class OCRMappingTemplateRecord(Base):
    __tablename__ = "ocr_mapping_templates"

    template_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    parser_key: Mapped[str] = mapped_column(String(128), default="ocr_scanned_statement")
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(64), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    payload: Mapped[dict] = mapped_column(JSON)


class JobRecord(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    source_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class CorrectionMemoryRecord(Base):
    __tablename__ = "correction_memory"

    correction_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parser_key: Mapped[str] = mapped_column(String(128), index=True)
    field_name: Mapped[str] = mapped_column(String(128), index=True)
    original_value: Mapped[str] = mapped_column(String(512))
    corrected_value: Mapped[str] = mapped_column(String(512))
    frequency: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class FxRateRecord(Base):
    """Daily cache of currency rates (price of 1 unit in KZT) from NB RK."""
    __tablename__ = "fx_rates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # "USD:2026-06-16"
    code: Mapped[str] = mapped_column(String(8), index=True)
    rate_date: Mapped[str] = mapped_column(String(10), index=True)
    rate: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(64), default="nationalbank.kz")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class AutocallSyncRecord(Base):
    """Tracks which autocall.kz campaigns were already pushed to Google Sheets.

    Dedup key = autocall id. Together with the cutoff date (settings.autocall_sync_since)
    this prevents re-appending rows the finance team already entered by hand.
    """
    __tablename__ = "autocall_syncs"

    autocall_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at_src: Mapped[str | None] = mapped_column(String(32), nullable=True)  # created_at from API
    final_cost: Mapped[str | None] = mapped_column(String(32), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    status: Mapped[str] = mapped_column(String(16), default="success")


class AutocallTopupSyncRecord(Base):
    """Tracks balance top-ups already pushed to the «Пополнения» worksheet.

    Top-up rows have no id, so the dedup key is a hash of (timestamp, amount, desc).
    """
    __tablename__ = "autocall_topup_syncs"

    topup_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    date_time: Mapped[str | None] = mapped_column(String(32), nullable=True)
    amount: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class OnboardingProjectRecord(Base):
    __tablename__ = "onboarding_projects"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    bank_name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(64), default="draft")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    samples: Mapped[list["OnboardingSampleRecord"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
    )


class OnboardingSampleRecord(Base):
    __tablename__ = "onboarding_samples"

    sample_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("onboarding_projects.project_id"))
    source_filename: Mapped[str] = mapped_column(String(512))
    review_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="captured")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    project: Mapped[OnboardingProjectRecord] = relationship(back_populates="samples")
