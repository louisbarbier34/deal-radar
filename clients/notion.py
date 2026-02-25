"""
Notion API client — manages the Production Calendar database.

Notion database expected properties (created by setup or manually):
  - Project Name      (title)
  - Client            (rich_text)
  - Stage             (select)
  - Probability       (number, 0-100)
  - Deal Value        (number)
  - Deliverable Type  (select)
  - Close Date        (date)
  - Projected Start   (date)
  - Duration (weeks)  (number)
  - Production Lead   (rich_text)
  - Crew Notes        (rich_text)
  - Production Status (select)
  - Attio Record ID   (rich_text)  ← internal sync key
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from notion_client import AsyncClient

import config

logger = logging.getLogger(__name__)


class NotionProductionDB:
    """Async wrapper around the Production Calendar Notion database."""

    def __init__(self) -> None:
        self._client = AsyncClient(auth=config.NOTION_TOKEN)
        self._db_id = config.NOTION_PRODUCTION_DB_ID

    # ------------------------------------------------------------------ #
    #  Query                                                               #
    # ------------------------------------------------------------------ #

    async def get_all_pages(self) -> list[dict]:
        """Fetch every row in the Production Calendar database."""
        pages: list[dict] = []
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {"database_id": self._db_id}
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = await self._client.databases.query(**kwargs)
            pages.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        return pages

    async def find_page_by_attio_id(self, attio_id: str) -> dict | None:
        """Look up an existing Notion page by its Attio Record ID."""
        resp = await self._client.databases.query(
            database_id=self._db_id,
            filter={
                "property": "Attio Record ID",
                "rich_text": {"equals": attio_id},
            },
        )
        results = resp.get("results", [])
        return results[0] if results else None

    # ------------------------------------------------------------------ #
    #  Write                                                               #
    # ------------------------------------------------------------------ #

    async def upsert_deal(self, attio_deal: dict, attio_client: Any) -> dict:
        """
        Create or update a Notion page for the given Attio deal.
        Returns the Notion page dict.
        """
        record_id: str = attio_deal.get("id", {}).get("record_id", "")
        name = attio_client._deal_name(attio_deal)
        stage = attio_client._deal_stage(attio_deal)
        prob = attio_client._deal_probability(attio_deal)
        value = attio_client._deal_value(attio_deal)
        close = attio_client._deal_close_date(attio_deal)
        owner = attio_client._deal_owner(attio_deal)

        props = self._build_properties(
            project_name=name,
            stage=stage,
            probability=prob,
            deal_value=value,
            close_date=close,
            production_lead=owner,
            attio_record_id=record_id,
        )

        existing = await self.find_page_by_attio_id(record_id)
        if existing:
            page = await self._client.pages.update(
                page_id=existing["id"], properties=props
            )
            logger.debug("Updated Notion page for deal: %s", name)
        else:
            page = await self._client.pages.create(
                parent={"database_id": self._db_id}, properties=props
            )
            logger.info("Created Notion page for deal: %s", name)
        return page

    async def mark_deal_won(self, attio_id: str, handoff_notes: str = "") -> bool:
        """Set Production Status to 'Handed Off' when a deal is Won."""
        page = await self.find_page_by_attio_id(attio_id)
        if not page:
            return False
        props: dict[str, Any] = {
            "Production Status": {"select": {"name": "Handed Off"}},
        }
        if handoff_notes:
            props["Crew Notes"] = {
                "rich_text": [{"text": {"content": handoff_notes[:2000]}}]
            }
        await self._client.pages.update(page_id=page["id"], properties=props)
        return True

    # ------------------------------------------------------------------ #
    #  Property builders                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_properties(
        project_name: str = "",
        client_name: str = "",
        stage: str = "",
        probability: float | None = None,
        deal_value: float | None = None,
        deliverable_type: str = "",
        close_date: datetime | None = None,
        projected_start: datetime | None = None,
        duration_weeks: int | None = None,
        production_lead: str = "",
        crew_notes: str = "",
        production_status: str = "",
        attio_record_id: str = "",
    ) -> dict:
        props: dict[str, Any] = {}

        if project_name:
            props["Project Name"] = {"title": [{"text": {"content": project_name}}]}
        if client_name:
            props["Client"] = {"rich_text": [{"text": {"content": client_name}}]}
        if stage:
            props["Stage"] = {"select": {"name": stage}}
        if probability is not None:
            props["Probability"] = {"number": probability}
        if deal_value is not None:
            props["Deal Value"] = {"number": deal_value}
        if deliverable_type:
            props["Deliverable Type"] = {"select": {"name": deliverable_type}}
        if close_date:
            props["Close Date"] = {"date": {"start": close_date.strftime("%Y-%m-%d")}}
        if projected_start:
            props["Projected Start"] = {
                "date": {"start": projected_start.strftime("%Y-%m-%d")}
            }
        if duration_weeks is not None:
            props["Duration (weeks)"] = {"number": duration_weeks}
        if production_lead:
            props["Production Lead"] = {
                "rich_text": [{"text": {"content": production_lead}}]
            }
        if crew_notes:
            props["Crew Notes"] = {
                "rich_text": [{"text": {"content": crew_notes[:2000]}}]
            }
        if production_status:
            props["Production Status"] = {"select": {"name": production_status}}
        if attio_record_id:
            props["Attio Record ID"] = {
                "rich_text": [{"text": {"content": attio_record_id}}]
            }
        return props

    # ------------------------------------------------------------------ #
    #  Database setup helper (run once)                                    #
    # ------------------------------------------------------------------ #

    async def ensure_database_properties(self) -> None:
        """
        Adds any missing properties to the Notion database.
        Safe to call repeatedly (no-ops if property already exists).
        """
        db = await self._client.databases.retrieve(database_id=self._db_id)
        existing_props = set(db.get("properties", {}).keys())

        desired = {
            "Client": {"rich_text": {}},
            "Stage": {
                "select": {
                    "options": [
                        {"name": "Lead", "color": "gray"},
                        {"name": "Qualified", "color": "blue"},
                        {"name": "Proposal Sent", "color": "yellow"},
                        {"name": "Negotiation", "color": "orange"},
                        {"name": "Won", "color": "green"},
                        {"name": "Lost", "color": "red"},
                    ]
                }
            },
            "Probability": {"number": {"format": "percent"}},
            "Deal Value": {"number": {"format": "dollar"}},
            "Deliverable Type": {
                "select": {
                    "options": [
                        {"name": "Commercial", "color": "blue"},
                        {"name": "Film", "color": "purple"},
                        {"name": "TV Series", "color": "green"},
                        {"name": "Brand Content", "color": "yellow"},
                        {"name": "Other", "color": "gray"},
                    ]
                }
            },
            "Close Date": {"date": {}},
            "Projected Start": {"date": {}},
            "Duration (weeks)": {"number": {}},
            "Production Lead": {"rich_text": {}},
            "Crew Notes": {"rich_text": {}},
            "Production Status": {
                "select": {
                    "options": [
                        {"name": "Pre-Production", "color": "yellow"},
                        {"name": "In Production", "color": "blue"},
                        {"name": "Post", "color": "purple"},
                        {"name": "Delivered", "color": "green"},
                        {"name": "Handed Off", "color": "orange"},
                        {"name": "On Hold", "color": "gray"},
                    ]
                }
            },
            "Attio Record ID": {"rich_text": {}},
        }

        to_add = {k: v for k, v in desired.items() if k not in existing_props}
        if not to_add:
            logger.info("Notion database properties are up to date.")
            return

        await self._client.databases.update(
            database_id=self._db_id, properties=to_add
        )
        logger.info("Added %d properties to Notion database.", len(to_add))


# Singleton
notion_db = NotionProductionDB()
