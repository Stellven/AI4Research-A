"""Identifiers, timestamps, and content hashes.

Primary keys are globally unique by being run-prefixed (design §6), so single-column
foreign keys can never match another run's row even in the shared store.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import uuid


def utc_now_iso() -> str:
    """UTC timestamp, second precision, ISO-8601 (e.g. 2026-05-29T15:30:12+00:00)."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def new_run_id(now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return f"run_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def mint(run_id: str, prefix: str, n: int) -> str:
    """A run-prefixed, globally-unique entity id, e.g. 'run_..._ab12.SC0001'."""
    return f"{run_id}.{prefix}{n:04d}"


def node_id(run_id: str, order_index: int) -> str:
    """Stable physical-plan node id, shared by the plan and the invocation that ran it."""
    return f"{run_id}.N{order_index:02d}"


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
