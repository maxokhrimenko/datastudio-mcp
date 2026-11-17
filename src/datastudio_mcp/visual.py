"""See a page as viewers will: a screenshot of its whole canvas from the draft model.

Designing a report needs a feedback loop. The MCP tab shows the draft in View; the
viewport is temporarily enlarged to the canvas size (CDP device metrics, cleared after),
data requests are awaited, and the canvas element alone is captured.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp.utilities.types import Image

from .browser import REQUEST_SCRIPT, StudioError
from .pages import page_items

if TYPE_CHECKING:
    from .service import Studio

_SIZE = """pageId => {
    const report = window.angular.element(document.body).injector()
        .get('reportContextService').getReport();
    const page = report.getPage(pageId);
    const canvas = document.querySelector('.ng2-canvas-container');
    const width = Number(page?.getAttributeValue?.('width') || report.getAttributeValue('width'));
    const height = Number(page?.getAttributeValue?.('height') ||
        report.getAttributeValue('height'));
    // Uploaded images load after the data: their file payload, then the <img> decode.
    const files = (page?.getComponentsRecursive?.() || page?.getComponents?.() || [])
        .filter(c => c.getDisplayType?.() === 'image-component' && c.getDisplayKeyValue('file'))
        .filter(c => !c.getState().getValue('imagePayload')).length;
    const decoding = [...(canvas?.querySelectorAll('img') || [])]
        .filter(img => !img.complete || img.naturalWidth === 0).length;
    return {width, height, canvas: Boolean(canvas), loading: report.getLoadingCount?.() ?? 0,
        spinners: document.querySelectorAll('.ng2-canvas-container .loading-spinner, ' +
            '.ng2-canvas-container mat-progress-spinner, .ng2-canvas-container .lego-loading')
            .length + files + decoding};
}"""


def hidden_from_viewers(value: Any, page_id: str, hidden: bool = False) -> bool:
    """The page, or a section that holds it, is hidden from viewers in the navigation."""
    if isinstance(value, list):
        return any(hidden_from_viewers(item, page_id, hidden) for item in value)
    if not isinstance(value, dict):
        return False
    page = value.get("page")
    if isinstance(page, dict) and page.get("pageId") == page_id:
        return hidden or bool(page.get("hiddenFromViewer"))
    section = value.get("section")
    if isinstance(section, dict):
        return hidden_from_viewers(section.get("navItems", []), page_id,
                                   hidden or bool(section.get("hiddenFromViewer")))
    return hidden_from_viewers(value.get("navItems", []), page_id, hidden)


class Visual:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def page_screenshot(self, page_id: str, quality: int = 70) -> Image:
        if not 20 <= quality <= 95:
            raise StudioError("INVALID_QUALITY", "quality must be 20-95")
        page = await self.studio.session.acquire(page_id)
        shown = re.search(r"/page/([^/?#]+)", page.url)
        if not shown or shown[1] != page_id:
            # The viewer never shows a page hidden from viewers: it opens the first visible
            # page instead, and the canvas would be that page's. A visible page of an older
            # report may simply have another URL token than its ID (/page/SEHDG for 89400174).
            navigation = (await self.studio.inventory())["navigation"] or {}
            ids = {p["pageId"] for p in page_items(navigation)}
            if page_id not in ids or hidden_from_viewers(navigation, page_id) or (
                    shown and shown[1] in ids):
                raise StudioError("PAGE_NOT_SHOWN", f"Viewers cannot open {page_id} (hidden "
                                  "from viewers, or not a page of this report); the viewer "
                                  "opened another page, so there is nothing to capture")
        size = await page.evaluate(_SIZE, page_id)
        if not size["canvas"]:
            raise StudioError("EDITOR_NOT_READY", "The report canvas is not rendered")
        cdp = await page.context.new_cdp_session(page)
        try:
            await cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": int(size["width"]) + 320, "height": int(size["height"]) + 260,
                "deviceScaleFactor": 1, "mobile": False})
            deadline = time.monotonic() + 90
            quiet = 0
            while time.monotonic() < deadline:
                state = await page.evaluate(_SIZE, page_id)
                quiet = quiet + 1 if not state["loading"] and not state["spinners"] else 0
                if quiet >= 3:
                    break
                await asyncio.sleep(1)
            await asyncio.sleep(1.5)  # Charts animate in after their data arrives.
            data = await page.locator(".ng2-canvas-container").first.screenshot(
                type="jpeg", quality=quality, timeout=60_000)
        finally:
            try:
                await cdp.send("Emulation.clearDeviceMetricsOverride")
            finally:
                await cdp.detach()
        return Image(data=data, format="jpeg")

    async def report_pdf(
        self, path: str, page_ids: list[str] | None = None, *, print_background: bool = True,
        add_report_link: bool = False, expand_tables: bool = False, page_numbers: bool = False,
    ) -> dict[str, Any]:
        """File > Download as > PDF, saved to a local .pdf file (rendered by Google)."""
        target = await asyncio.to_thread(lambda: Path(path).expanduser())
        if target.suffix.lower() != ".pdf":
            raise StudioError("INVALID_PATH", "path must end with .pdf")
        config = (await self.studio.report())["reportConfig"]
        pages = {p["pageId"]: p for p in config["page"]}
        order = [p["pageId"] for p in page_items(config.get("navigationInfo", {}))]
        chosen = page_ids or order
        unknown = [p for p in chosen if p not in pages]
        if unknown:
            raise StudioError("NOT_FOUND", f"Unknown pages: {', '.join(unknown)}")
        names = {p["pageId"]: p.get("displayName") for p in page_items(
            config.get("navigationInfo", {}))}
        attributes = config["report"].get("attributeConfig", {}).get("reportAttribute", {})

        def setting(page_id: str) -> dict[str, Any]:
            own = pages[page_id]["page"].get("attributeConfig", {}).get("pageAttribute", {})
            return {"pageId": page_id, "name": names.get(page_id) or own.get("name", ""),
                    **{k: own[k] for k in ("width", "height") if k in own}}

        body = {"reportId": self.studio.config.report_id, "pageIds": chosen,
                "pageNames": [setting(p)["name"] for p in chosen],
                "reportName": config["shareable"]["name"].strip().replace(" ", "_"),
                "printBackground": print_background, "pdfPassword": "",
                "addReportLink": add_report_link, "width": attributes.get("width"),
                "height": attributes.get("height"), "pageSettings": [setting(p) for p in chosen],
                "timeZoneId": "Etc/GMT", "layoutType": 2 if attributes.get("layoutType")
                == "responsive" else 1, "enableRenderDebugData": False,
                "pdfSettings": {"expandTables": expand_tables, "addPageNumbers": page_numbers,
                                "includeFilterBar": False}}
        page = await self.studio.session.page()
        result = await page.evaluate(
            REQUEST_SCRIPT + """const r = await request('getPdf', args.body);
            if (r.error) return r;
            let data = r.text; try { data = JSON.parse(r.text) } catch {}
            return {status: r.status, data: typeof data === 'string' ? data : null};}""",
            {"body": body, "timeout": 300_000},
        )
        if result.get("error") or result.get("status") != 200 or not result.get("data"):
            raise StudioError("PDF_FAILED", f"getPdf returned {result.get('status')}")
        data = base64.b64decode(result["data"])
        if not data.startswith(b"%PDF"):
            raise StudioError("PDF_FAILED", "Response is not a PDF")
        await asyncio.to_thread(target.write_bytes, data)
        return {"status": "saved", "path": str(target), "bytes": len(data), "pages": chosen}

    async def image_export(self, page_id: str, component_id: str, path: str) -> dict[str, Any]:
        """Save an image component's uploaded file locally (read-only for the report)."""
        from .page_import import _IMAGE, decode_payload

        folder = await asyncio.to_thread(lambda: Path(path).expanduser())
        page = await self.studio.session.acquire(page_id)
        for _ in range(30):  # the payload loads with the component
            image = await page.evaluate(_IMAGE, {"pageId": page_id, "componentId": component_id})
            if image.get("data"):
                break
            await asyncio.sleep(1)
        else:
            raise StudioError("IMAGE_NOT_READABLE", "No uploaded image in this component")
        target = folder / Path(image.get("name") or f"{component_id}.png").name \
            if folder.suffix == "" else folder
        data = decode_payload(image["data"])
        await asyncio.to_thread(lambda: (target.parent.mkdir(parents=True, exist_ok=True),
                                         target.write_bytes(data)))
        return {"status": "saved", "path": str(target), "bytes": len(data),
                "name": image.get("name")}
