"""Per-report service bundles over one shared browser; the active report is a default.

The bundle keyed None is the datasource scope, for datasource work outside any report.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .bigquery import BigQuery
from .blend_create import BlendCreate
from .blend_filters import BlendFilters
from .blend_joins import BlendJoins
from .blends import Blends
from .browser import REPORT_ID, Browser, Config, ReportSession, StudioError
from .checkbox import Checkbox
from .community_viz import CommunityViz
from .component_filters import ComponentFilters
from .components import Components
from .creation import Creation
from .datasources import Datasources
from .field_overrides import FieldOverrides
from .fields import Fields
from .filter_conditions import FilterConditions
from .filters import Filters
from .formulas import Formulas
from .group_filters import GroupFilters
from .inheritance import Inheritance
from .interactions import Interactions
from .inventory import Inventory
from .page_filters import PageFilters
from .pages import Pages
from .panels import Panels
from .patches import Patches
from .publishing import Publishing
from .report_filters import ReportFilters
from .runtime_controls import RuntimeControls
from .scope import Scope
from .service import Studio
from .sharing import Sharing
from .switching import Switching
from .theme_image import ThemeFromImage
from .variables import Variables
from .visual import Visual


@dataclass
class Bundle:
    studio: Studio
    pages: Pages
    components: Components
    creation: Creation
    fields: Fields
    formulas: Formulas
    datasources: Datasources
    switching: Switching
    interactions: Interactions
    inheritance: Inheritance
    runtime_controls: RuntimeControls
    checkbox: Checkbox
    filters: Filters
    filter_conditions: FilterConditions
    component_filters: ComponentFilters
    page_filters: PageFilters
    report_filters: ReportFilters
    group_filters: GroupFilters
    blends: Blends
    blend_filters: BlendFilters
    blend_joins: BlendJoins
    blend_create: BlendCreate
    panels: Panels
    bigquery: BigQuery
    publishing: Publishing
    inventory: Inventory
    patches: Patches
    visual: Visual
    scope: Scope
    variables: Variables
    community_viz: CommunityViz
    theme_image: ThemeFromImage
    field_overrides: FieldOverrides
    sharing: Sharing
    name: str | None = None


def build_bundle(session: ReportSession) -> Bundle:
    studio = Studio(session)
    return Bundle(
        studio=studio,
        pages=Pages(studio),
        components=Components(studio),
        creation=Creation(studio),
        fields=Fields(studio),
        formulas=Formulas(studio),
        datasources=Datasources(studio),
        switching=Switching(studio),
        interactions=Interactions(studio),
        inheritance=Inheritance(studio),
        runtime_controls=RuntimeControls(studio),
        checkbox=Checkbox(studio),
        filters=Filters(studio),
        filter_conditions=FilterConditions(studio),
        component_filters=ComponentFilters(studio),
        page_filters=PageFilters(studio),
        report_filters=ReportFilters(studio),
        group_filters=GroupFilters(studio),
        blends=Blends(studio),
        blend_filters=BlendFilters(studio),
        blend_joins=BlendJoins(studio),
        blend_create=BlendCreate(studio),
        panels=Panels(studio),
        bigquery=BigQuery(studio),
        publishing=Publishing(studio),
        inventory=Inventory(studio),
        patches=Patches(studio),
        visual=Visual(studio),
        scope=Scope(studio),
        variables=Variables(studio),
        community_viz=CommunityViz(studio),
        theme_image=ThemeFromImage(studio),
        field_overrides=FieldOverrides(studio),
        sharing=Sharing(studio),
    )


class Workspace:
    def __init__(self, browser: Browser, factory: Callable[[ReportSession], Any] = build_bundle):
        self.browser = browser
        self.active: str | None = None
        self.tool_lock = asyncio.Lock()
        self._factory = factory
        self._bundles: dict[str | None, Any] = {}

    def resolve(self, report_id: str | None) -> Any:
        target = report_id or self.active
        if target is None:
            raise StudioError(
                "NO_ACTIVE_REPORT", "Call report_open with the report URL or UUID first"
            )
        if not REPORT_ID.fullmatch(target):
            raise StudioError("INVALID_REPORT_REFERENCE", "report_id must be a report UUID")
        bundle = self._bundles.get(target)
        if bundle is None:
            session = ReportSession(self.browser, Config.for_report(self.browser.config, target))
            bundle = self._bundles[target] = self._factory(session)
        return bundle

    def datasources(self) -> Any:
        """The datasource scope: datasource work that belongs to no report."""
        bundle = self._bundles.get(None)
        if bundle is None:
            session = ReportSession(self.browser, Config.for_report(self.browser.config, None))
            bundle = self._bundles[None] = self._factory(session)
        return bundle

    def name(self, report_id: str) -> str | None:
        bundle = self._bundles.get(report_id)
        return getattr(bundle, "name", None) if bundle is not None else None
