"""Copy pages from another report (copy and paste between reports, as in the editor).

The source report is only read. Into the target the import brings, in this order, each
step journalled and read back on its own (a batch, not a transaction):

1. the reusable datasources the pages use, attached to the target (as pasting does);
2. the saved filter resources the pages reference (same IDs);
3. the pages themselves, with their conditional formatting, through the page lifecycle
   engine (`createPages`); in-report page links (`internalLink`) to imported pages point
   to their new copies; the pages go into navigation, optionally as one section;
4. image files: an uploaded image belongs to its report, so each one is read from the
   source page model and uploaded again into the copy, as the panel's file picker does.

Blends, report variables and embedded datasources are refused with a clear error.
"""

from __future__ import annotations

import base64
import copy
import json
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .bigquery import BigQuery
from .browser import StudioError
from .document import fingerprint, walk_components
from .pages import Pages, page_items, structure
from .panels import Panels
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio

_IMAGE = """({pageId, componentId}) => {
    const i = window.angular?.element(document.body).injector();
    const page = i?.get('reportContextService').getReport()?.getPage(pageId);
    const model = (page?.getComponentsRecursive?.() || page?.getComponents() || [])
        .find(c => c.getId() === componentId);
    const data = model?.getState().getValue('imagePayload');
    return {data: typeof data === 'string' ? data : null,
            name: model?.getDisplayKeyValue('file')?.name || null};
}"""


def decode_payload(data: str) -> bytes:
    """An image payload is base64 in the editor's web-safe variant ('-', '_', '.' for
    '+', '/', '='); the standard decoder would silently drop those and corrupt it."""
    return base64.b64decode(data.translate(str.maketrans("-_.", "+/=")), validate=True)


def _datasets(value: Any, found: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("datasetId"), str):
            kind = "blends" if value.get("datasetType") == 2 else "datasources"
            found[kind].add(value["datasetId"])
        if isinstance(value.get("datasourceId"), str) and value["datasourceId"]:
            found["datasources"].add(value["datasourceId"])
        for item in value.values():
            _datasets(item, found)
    elif isinstance(value, list):
        for item in value:
            _datasets(item, found)


def _remap(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: _remap(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap(v, ids) for v in value]
    return ids.get(value, value) if isinstance(value, str) else value


def _entries(content: dict, key: str) -> list[dict]:
    resource = content.get("resource", {}).get(key, {})
    return resource.get("entry", []) if isinstance(resource, dict) else []


class PageImport:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def run(self, source: Studio, page_ids: list[str], expected_structure: str,
                  *, section_name: str | None = None, replace: dict[str, str] | None = None,
                  dry_run: bool = True) -> dict[str, Any]:
        """`replace` maps a source page to a page here whose content it replaces: the page
        keeps its ID (links and bookmarks), name and place in the navigation."""
        replace = replace or {}
        if not 1 <= len(page_ids) <= 30 or len(set(page_ids)) != len(page_ids):
            raise StudioError("INVALID_BATCH", "Provide 1-30 distinct source page IDs")
        if not set(replace) <= set(page_ids) or len(set(replace.values())) != len(replace):
            raise StudioError("INVALID_BATCH", "replace maps distinct imported pages to "
                              "distinct pages of this report")
        if source.config.report_id == self.studio.config.report_id:
            raise StudioError("SAME_REPORT", "Use page_duplicate inside one report")
        src = (await source.report())["reportConfig"]
        frames = {p["pageId"]: p for p in src["page"]}
        missing = [p for p in page_ids if p not in frames]
        if missing:
            raise StudioError("NOT_FOUND", f"Not pages of the source report: {missing}")
        meta = {p["pageId"]: p for p in page_items(src.get("navigationInfo", {}))}
        target = await self.studio.report()
        config = target["reportConfig"]
        if fingerprint(structure(config)) != expected_structure:
            raise StudioError("VERSION_CONFLICT", "Target pages changed; report_inspect again")

        picked = {p: {"page": frames[p]["page"], "chartCondition": frames[p].get(
            "chartCondition", [])} for p in page_ids}
        found: dict[str, set[str]] = {"datasources": set(), "blends": set()}
        _datasets(picked, found)
        text = json.dumps(picked)
        if found["blends"]:
            raise StudioError("BLEND_IMPORT_UNSUPPORTED",
                              f"The pages use blends {sorted(found['blends'])}")
        if "REPORT_VARIABLE:" in text or any(
                f'"{e.get("reportParameterConfig", {}).get("parameterKey")}"' in text
                for e in report_content(src).get("resource", {}).get(
                    "unifiedParameterResource", [])):
            raise StudioError("VARIABLE_IMPORT_UNSUPPORTED", "The pages use report variables")
        attached = {s["id"] for s in await self.studio.sources()}
        embedded = {s["id"] for s in await source.sources() if s.get("isEmbedded")}
        to_attach = sorted(found["datasources"] - attached)
        if embedded.intersection(to_attach):
            raise StudioError("EMBEDDED_DATASOURCE",
                              f"Embedded in the source report: {sorted(embedded & set(to_attach))}")

        src_filters = {e["key"]: e for e in _entries(report_content(src), "filter")}
        own_filters = {e["key"]: e for e in _entries(report_content(config), "filter")}
        needed = sorted(k for k in src_filters if f'"{k}"' in text)
        clash = [k for k in needed if k in own_filters and own_filters[k] != src_filters[k]]
        if clash:
            raise StudioError("FILTER_ID_CONFLICT", f"Different filters with these IDs: {clash}")
        to_copy = [copy.deepcopy(src_filters[k]) for k in needed if k not in own_filters]

        existing = set(structure(config)["pages"])
        if not set(replace.values()) <= existing:
            raise StudioError("NOT_FOUND", "replace targets must be pages of this report")
        new_ids = dict(replace)
        for page_id in (p for p in page_ids if p not in replace):
            new_id = "p_" + uuid.uuid4().hex[:10]
            while new_id in existing or new_id in new_ids.values():
                new_id = "p_" + uuid.uuid4().hex[:10]
            new_ids[page_id] = new_id
        used = {c["componentId"] for f in config["page"] if f["pageId"] not in replace.values()
                for c in walk_components(f["page"].get("componentConfig", []))}
        incoming = [c["componentId"] for p in picked.values()
                    for c in walk_components(p["page"].get("componentConfig", []))]
        if used.intersection(incoming) or len(set(incoming)) != len(incoming):
            raise StudioError("DUPLICATE_ID", "Component IDs already exist in the target")
        links = set(re.findall(r'"internalLink": "(p_[A-Za-z0-9]+)"', text))
        dangling = sorted(links - set(page_ids))
        images = [(p, c["componentId"]) for p, body in picked.items()
                  for c in walk_components(body["page"].get("componentConfig", []))
                  if c.get("type") == "image-component"]
        plan = {"source_report_id": source.config.report_id, "page_id_map": new_ids,
                **({"replaced_pages": sorted(replace.values())} if replace else {}),
                "datasources_to_attach": to_attach, "filters_to_copy": needed,
                "images_to_upload": [f"{new_ids[p]}/{c}" for p, c in images],
                **({"links_to_pages_not_imported": dangling} if dangling else {}),
                "atomic_scope": "each step on its own (datasources, filters, pages, images)"}
        if dry_run:
            return {"status": "planned", **plan}

        steps: dict[str, Any] = {}

        def stop(name: str, result: dict) -> dict[str, Any]:
            return {"status": "partial" if steps else result.get("status"), "failed_step": name,
                    "steps": {**steps, name: result}, **plan}

        for datasource_id in to_attach:
            result = await BigQuery(self.studio).attach(datasource_id)
            if result.get("status") != "verified":
                return stop(f"attach:{datasource_id}", result)
            steps[f"attach:{datasource_id}"] = result
        if to_copy:
            async with self.studio.mutation_lock():
                raw = (await self.studio.report())["reportConfig"]
                after = report_content(raw)
                after.setdefault("resource", {}).setdefault("filter", {}).setdefault(
                    "entry", []).extend(to_copy)
                result = await ReportResources(self.studio).write(raw, after)
            if result.get("status") != "verified":
                return stop("filters", result)
            steps["filters"] = result
        async with self.studio.mutation_lock():
            config = (await self.studio.report())["reportConfig"]
            wanted = structure(config)
            src_size = src["report"].get("attributeConfig", {}).get("reportAttribute", {})
            own_size = config["report"].get("attributeConfig", {}).get("reportAttribute", {})
            replaced = {}
            for page_id, new_id in new_ids.items():
                body = _remap(copy.deepcopy(picked[page_id]), new_ids)
                # Canvas size is report-wide unless a page sets its own: keep the source's.
                attrs = body["page"].setdefault("attributeConfig", {}).setdefault(
                    "pageAttribute", {})
                for key in ("width", "height"):
                    if key not in attrs and src_size.get(key) != own_size.get(key) and (
                            src_size.get(key) is not None):
                        attrs[key] = src_size[key]
                if page_id in replace:
                    own = wanted["pages"][new_id]["page"].get("attributeConfig", {}).get(
                        "pageAttribute", {})
                    if "name" in own:
                        attrs["name"] = own["name"]
                    replaced[new_id] = body
                    continue
                wanted["pages"][new_id] = body
            items = [{"page": {**{k: copy.deepcopy(v) for k, v in meta.get(p, {}).items()
                                  if k != "pageId"}, "pageId": new_ids[p]}}
                     for p in page_ids if p not in replace]
            created = [p for p in page_ids if p not in replace]
            for item, page_id in zip(items, created, strict=True):
                item["page"].setdefault("displayName", frames[page_id]["page"].get(
                    "attributeConfig", {}).get("pageAttribute", {}).get("name", page_id))
            if section_name and items:
                items = [{"section": {"sectionId": "stx_" + uuid.uuid4().hex[:10],
                                      "displayName": section_name, "navItems": items}}]
            if items:
                wanted["navigation"]["navItems"].extend(items)
                result = await Pages(self.studio)._reconcile(
                    config, wanted, dry_run=False,
                    created_ids=[i for i in new_ids.values() if i not in replaced])
                if result.get("status") != "verified":
                    return stop("pages", result)
                steps["pages"] = result
            frames_here = {f["pageId"]: f for f in config["page"]}
            for page_id, body in replaced.items():
                result = await self.studio._write_page(
                    frames_here[page_id], body["page"], body["chartCondition"])
                if result.get("status") != "verified":
                    return stop(f"replace:{page_id}", result)
                steps[f"replace:{page_id}"] = result
        for page_id, component_id in images:
            await source.session.acquire(page_id)
            image = await (await source.session.page()).evaluate(
                _IMAGE, {"pageId": page_id, "componentId": component_id})
            if not image.get("data"):
                return stop(f"image:{component_id}", {"status": "IMAGE_NOT_READABLE"})
            # The upload keeps the file name: a private folder, the source's name.
            folder = self.studio.config.state_dir / f"import-{uuid.uuid4().hex}"
            folder.mkdir(mode=0o700, parents=True)
            path = folder / Path(image.get("name") or "image.png").name
            path.write_bytes(decode_payload(image["data"]))
            try:
                new_page = new_ids[page_id]
                frame = next(f for f in (await self.studio.report())["reportConfig"]["page"]
                             if f["pageId"] == new_page)
                result = await Panels(self.studio).image(
                    new_page, component_id, str(path), fingerprint(frame["page"]),
                    dry_run=False)
            finally:
                path.unlink(missing_ok=True)
                folder.rmdir()
            if result.get("status") != "verified":
                return stop(f"image:{component_id}", result)
            steps[f"image:{component_id}"] = result
        return {"status": "verified", "steps": steps, **plan}


class ThemeCopy:
    """Use another report's theme here: its custom theme resource, made active."""

    def __init__(self, studio: Studio):
        self.studio = studio

    async def run(self, source: Studio, expected: str, *, dry_run: bool = True) -> dict:
        src = report_content((await source.report())["reportConfig"])
        active = src.get("propertyConfig", {}).get("reportProperty", {}).get("themeConfig")
        if not active or not src.get("resource", {}).get("theme"):
            raise StudioError("BUILT_IN_THEME",
                              f"The source uses built-in theme {active!r}; pick it with "
                              "settings_set on the report instead")
        raw = (await self.studio.report())["reportConfig"]
        before = report_content(raw)
        if fingerprint(before) != expected:
            raise StudioError("VERSION_CONFLICT", "Report changed; report_content_get again")
        after = copy.deepcopy(before)
        after.setdefault("resource", {})["theme"] = copy.deepcopy(src["resource"]["theme"])
        after.setdefault("propertyConfig", {}).setdefault("reportProperty", {})[
            "themeConfig"] = active
        if after == before:
            return {"status": "unchanged", "theme": active}
        if dry_run:
            return {"status": "planned", "theme": active,
                    "source_report_id": source.config.report_id}
        async with self.studio.mutation_lock():
            return {**await ReportResources(self.studio).write(raw, after, settings=True),
                    "theme": active}
