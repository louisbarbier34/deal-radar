"""
Attio API client.

Attio REST API docs: https://developers.attio.com/reference
All deal data flows through this module.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

import config

logger = logging.getLogger(__name__)

# ─── Deal list cache ──────────────────────────────────────────────────────────
# Shared across all callers (B1, B2, B4, B5, A5, C, find_deal_by_name).
# Reduces redundant Attio API calls during the same scheduler cycle.
_DEAL_CACHE_TTL_SECONDS = 300  # 5 minutes
_deal_cache: list[dict] | None = None
_deal_cache_ts: float = 0.0

BASE_URL = "https://api.attio.com/v2"


# ─── Attio attribute slug constants ────────────────────────────────────────────
# Reference these instead of magic strings throughout the codebase.
# If your Attio workspace uses different slug names, update them here only.
class AttioFields:
    NAME = "name"
    STAGE = "stage"
    PROBABILITY = "probability"
    VALUE = "value"
    CLOSE_DATE = "close_date"
    OWNER = "owner"


def _is_retryable(exc: BaseException) -> bool:
    """Retry on 429 (rate limit) and 5xx (server errors)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
        return True
    return False


class AttioClient:
    """Thin async wrapper around the Attio v2 REST API."""

    def __init__(self) -> None:
        self._headers = {
            "Authorization": f"Bearer {config.ATTIO_API_KEY}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    #  Low-level helpers (with tenacity retry)                            #
    # ------------------------------------------------------------------ #

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{BASE_URL}{path}", headers=self._headers, params=params)
            r.raise_for_status()
            return r.json()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _post(self, path: str, body: dict) -> Any:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{BASE_URL}{path}", headers=self._headers, json=body)
            r.raise_for_status()
            return r.json()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _patch(self, path: str, body: dict) -> Any:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.patch(f"{BASE_URL}{path}", headers=self._headers, json=body)
            r.raise_for_status()
            return r.json()

    # ------------------------------------------------------------------ #
    #  Deals                                                               #
    # ------------------------------------------------------------------ #

    async def list_deals(
        self,
        filters: list[dict] | None = None,
        limit: int = 500,
        bypass_cache: bool = False,
    ) -> list[dict]:
        """
        Return all deals matching optional Attio filter syntax.

        Results are cached for _DEAL_CACHE_TTL_SECONDS (default 5 min) when
        called without filters. Pass bypass_cache=True to force a fresh fetch
        (e.g. immediately after an update).
        """
        global _deal_cache, _deal_cache_ts

        use_cache = not filters and not bypass_cache
        if use_cache and _deal_cache is not None:
            age = time.monotonic() - _deal_cache_ts
            if age < _DEAL_CACHE_TTL_SECONDS:
                logger.debug("list_deals: cache hit (age=%.0fs)", age)
                return list(_deal_cache)

        body: dict = {"limit": limit}
        if filters:
            body["filter"] = {"$and": filters}
        data = await self._post(
            f"/objects/{config.ATTIO_DEAL_OBJECT}/records/query", body
        )
        result = data.get("data", [])

        if use_cache:
            _deal_cache = result
            _deal_cache_ts = time.monotonic()
            logger.debug("list_deals: cache refreshed (%d deals)", len(result))

        return result

    def invalidate_cache(self) -> None:
        """Call after writing to Attio so the next read gets fresh data."""
        global _deal_cache, _deal_cache_ts
        _deal_cache = None
        _deal_cache_ts = 0.0
        logger.debug("list_deals: cache invalidated")

    async def get_deal(self, record_id: str) -> dict:
        data = await self._get(
            f"/objects/{config.ATTIO_DEAL_OBJECT}/records/{record_id}"
        )
        return data.get("data", {})

    async def update_deal(self, record_id: str, attributes: dict) -> dict:
        """Patch arbitrary attributes on a deal record."""
        body = {"data": {"attributes": attributes}}
        data = await self._patch(
            f"/objects/{config.ATTIO_DEAL_OBJECT}/records/{record_id}", body
        )
        self.invalidate_cache()  # next list_deals() will see fresh data
        return data.get("data", {})

    async def find_deal_by_name(self, name: str) -> dict | None:
        """Fuzzy-find a deal by name. Returns the closest match or None."""
        deals = await self.list_deals()
        name_lower = name.lower().strip()
        # Exact match first
        for d in deals:
            if self._deal_name(d).lower() == name_lower:
                return d
        # Partial match
        for d in deals:
            if name_lower in self._deal_name(d).lower():
                return d
        return None

    # ------------------------------------------------------------------ #
    #  Convenience attribute readers                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _attr(deal: dict, key: str) -> Any:
        """
        Safely read a single Attio attribute value.

        Uses `is not None` guards (not `or`) to correctly handle falsy
        values like probability=0 or value=0.
        """
        attrs = deal.get("values", {})
        vals = attrs.get(key, [])
        if not vals:
            return None
        v = vals[0]
        if not isinstance(v, dict):
            return v
        # Number / text fields
        raw = v.get("value")
        if raw is not None:
            return raw
        # Relation fields
        rel = v.get("target_record_id")
        if rel is not None:
            return rel
        # Select / status fields
        option = v.get("option")
        if option is not None:
            return option.get("title")
        return None

    @classmethod
    def _deal_name(cls, deal: dict) -> str:
        return cls._attr(deal, AttioFields.NAME) or ""

    @classmethod
    def _deal_probability(cls, deal: dict) -> float | None:
        v = cls._attr(deal, AttioFields.PROBABILITY)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _deal_stage(cls, deal: dict) -> str:
        return cls._attr(deal, AttioFields.STAGE) or ""

    @classmethod
    def _deal_value(cls, deal: dict) -> float | None:
        v = cls._attr(deal, AttioFields.VALUE)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _deal_close_date(cls, deal: dict) -> datetime | None:
        raw = cls._attr(deal, AttioFields.CLOSE_DATE)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None

    @classmethod
    def _deal_owner(cls, deal: dict) -> str:
        return cls._attr(deal, AttioFields.OWNER) or ""

    @classmethod
    def _deal_last_updated(cls, deal: dict) -> datetime | None:
        raw = deal.get("updated_at") or deal.get("created_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None

    # ------------------------------------------------------------------ #
    #  Higher-level queries used by automations                           #
    # ------------------------------------------------------------------ #

    async def get_active_deals(self) -> list[dict]:
        """All deals not in Closed Won / Closed Lost."""
        deals = await self.list_deals()
        closed = {"closed won", "won", "closed lost", "lost"}
        return [d for d in deals if self._deal_stage(d).lower() not in closed]

    async def get_stale_deals(self) -> list[dict]:
        """Deals not updated in STALE_DEAL_DAYS days."""
        cutoff = datetime.now(timezone.utc).timestamp() - (
            config.STALE_DEAL_DAYS * 86_400
        )
        active = await self.get_active_deals()
        stale = []
        for d in active:
            last = self._deal_last_updated(d)
            if last and last.timestamp() < cutoff:
                stale.append(d)
        return stale

    async def get_deals_closing_in_month(self, year: int, month: int) -> list[dict]:
        deals = await self.get_active_deals()
        return [
            d
            for d in deals
            if (cd := self._deal_close_date(d))
            and cd.year == year
            and cd.month == month
        ]

    async def get_won_deals_since(self, since: datetime) -> list[dict]:
        deals = await self.list_deals()
        won_stages = {"closed won", "won"}
        result = []
        for d in deals:
            if self._deal_stage(d).lower() in won_stages:
                updated = self._deal_last_updated(d)
                if updated and updated >= since:
                    result.append(d)
        return result

    # ------------------------------------------------------------------ #
    #  Notes / Activities                                                  #
    # ------------------------------------------------------------------ #

    async def add_note(self, record_id: str, title: str, body: str) -> dict:
        """Append a note to a deal record."""
        payload = {
            "data": {
                "format": "plaintext",
                "title": title,
                "content": body,
                "parent_object": config.ATTIO_DEAL_OBJECT,
                "parent_record_id": record_id,
            }
        }
        data = await self._post("/notes", payload)
        return data.get("data", {})

    # ------------------------------------------------------------------ #
    #  Formatted deal summary (used across multiple handlers)             #
    # ------------------------------------------------------------------ #

    @classmethod
    def format_deal_line(cls, deal: dict, show_owner: bool = False) -> str:
        """One-line Slack-friendly summary of a deal."""
        name = cls._deal_name(deal)
        prob = cls._deal_probability(deal)
        stage = cls._deal_stage(deal)
        value = cls._deal_value(deal)
        close = cls._deal_close_date(deal)

        parts = [f"*{name}*"]
        if prob is not None:
            parts.append(f"{int(prob)}%")
        if stage:
            parts.append(f"_{stage}_")
        if value:
            parts.append(f"${value:,.0f}")
        if close:
            parts.append(f"closes {close.strftime('%b %d')}")
        if show_owner:
            owner = cls._deal_owner(deal)
            if owner:
                parts.append(f"(owner: {owner})")
        return " · ".join(parts)


# Singleton
attio = AttioClient()
