"""Budgets périodiques versionnés, alertes et portes persistantes d'admission."""
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from app.identity_models import Base, utcnow


class ExecutionBudget(Base):
    __tablename__ = 'execution_budget'
    __table_args__ = (
        UniqueConstraint('project_id', 'scope', 'scope_id', 'version'),
        Index('uq_budget_current', 'project_id', 'scope', 'scope_id', unique=True, postgresql_where=text('is_current')),
        CheckConstraint("scope IN ('project','agent','session','task','model') AND period IN ('daily','weekly','monthly') AND version > 0"),
        CheckConstraint("metric IN ('tokens','seconds','cost') AND amount > 0 AND warning_percent BETWEEN 1 AND 99"),
        CheckConstraint("(metric = 'cost' AND currency IS NOT NULL) OR (metric <> 'cost' AND currency IS NULL)"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), nullable=False)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    period: Mapped[str] = mapped_column(String(10), nullable=False)
    metric: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(16,6), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3))
    warning_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey('app_user.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class BudgetAlert(Base):
    __tablename__ = 'budget_alert'
    __table_args__ = (UniqueConstraint('budget_id', 'period_start', 'level'),
                      CheckConstraint("level IN ('warning','exhausted','unavailable')"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), nullable=False)
    budget_id: Mapped[str] = mapped_column(ForeignKey('execution_budget.id'), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    level: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    acknowledged_by: Mapped[str | None] = mapped_column(ForeignKey('app_user.id'))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BudgetSuspension(Base):
    __tablename__ = 'budget_suspension'
    __table_args__ = (UniqueConstraint('alert_id', 'plan_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey('project.id'), nullable=False)
    alert_id: Mapped[str] = mapped_column(ForeignKey('budget_alert.id'), nullable=False)
    plan_id: Mapped[str] = mapped_column(ForeignKey('agent_plan_version.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resumed_by: Mapped[str | None] = mapped_column(ForeignKey('app_user.id'))
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
