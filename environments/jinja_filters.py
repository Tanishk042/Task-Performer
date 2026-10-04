"""Jinja filters/globals shared by the simulated apps' templates."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from environments.seed_data import format_date, format_money
from environments.shared_web import STATUS_LABELS, status_tag


def _display_date(value: str, style: str) -> str:
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return str(value)
    return format_date(parsed, style)  # type: ignore[arg-type]


def _money(value: Any, currency: str) -> str:
    try:
        return format_money(Decimal(str(value)), currency)
    except Exception:
        return str(value)


def register(env: Any) -> None:
    env.filters["display_date"] = _display_date
    env.filters["money"] = _money
    env.filters["status_tag"] = status_tag
    env.filters["status_label"] = lambda s: STATUS_LABELS.get(str(s), str(s).title())
    env.globals["STATUS_LABELS"] = STATUS_LABELS