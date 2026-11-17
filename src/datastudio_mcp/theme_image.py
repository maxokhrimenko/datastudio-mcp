"""Theme and layout > Theme > Extract theme from image.

The editor sends the image to `intelligence/color/getColorScheme`, shows the generated
color schemes and, on Apply, stores the chosen one as the report's custom theme: the
editor's default theme overlaid with the scheme's color slots and palette, marked as a
generated theme. The scheme -> theme mapping below is the editor's own (a module-local
function in the bundle, so it cannot be called directly); storing and activating the
theme use the editor's theme store on the live View model, through NativeOps.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .browser import REQUEST_SCRIPT, StudioError
from .document import fingerprint
from .nativeops import NativeOps
from .report_resources import report_content

if TYPE_CHECKING:
    from .service import Studio

MAX_BYTES = 10 * 1024 * 1024
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF")
ENDPOINT = "intelligence/color/getColorScheme"

_SCHEMES = REQUEST_SCRIPT + """
    const r = await request(args.endpoint, {image:{base64Data:args.data}, maxNumColorSchemes:3});
    if (r.error) return r;
    if (r.status !== 200 || !Array.isArray(r.json?.colorSchemes)) return {error:'SCHEME_FAILED',
        status:r.status};
    return {schemes:r.json.colorSchemes};
}"""

_APPLY = """async args => {
    // Apply of the generated-themes dialog: default theme + the scheme's slots, stored as
    // the report's custom theme and made active.
    const X=OPS(args),{report}=X;
    const scheme=args.scheme,slots=new Map();
    for(const m of scheme.themeMappings||[]){
        if(m.type===14){if(m.colorArray)slots.set(14,m.colorArray.colors);}
        else if(m.type&&m.color)slots.set(m.type,m.color);
    }
    for(const t of [1,2,3,4,5,6,7,8,9,11,13,14])
        if(!slots.get(t))slots.set(t,t===14?[]:{color:'transparent'});
    const series=slots.get(14);
    const content={...structuredClone(R.defaultTheme()),
        themeFillColor:slots.get(1),themeFontColor:slots.get(2),
        themeAccentFillColor:slots.get(3),themeAccentFontColor:slots.get(4),
        themeIncreaseColor:slots.get(5),themeDecreaseColor:slots.get(6),
        themeGridColor:slots.get(9),themeBackground:slots.get(7),themeBorderColor:slots.get(8),
        themeComponentHeaderColor:slots.get(13),themeSeriesColor:series,
        themeTextboxFontColor:slots.get(10),themeTextboxFillColor:slots.get(11),
        themePaletteColors:scheme.grid,isGeneratedTheme:true,
        themeNavigationBackground:slots.get(7),themeNavigationFontColor:slots.get(2),
        themeNavigationSelectionFontColor:series[0]};
    const before=X.serializeReport();
    const panel=R.panelService(X.i);
    const id=R.exported('themeStore')(panel.reportType.themeService,report,content);
    report.setDisplayKeyValue('themeConfig',id);
    report.syncDisplayState?.();
    return {before,after:X.serializeReport()};
}"""


def _preview(scheme: dict[str, Any]) -> dict[str, Any]:
    names = {1: "fill", 2: "font", 3: "accent_fill", 4: "accent_font", 7: "background",
             8: "border", 9: "grid", 13: "component_header"}
    colors = {names.get(m.get("type"), str(m.get("type"))): (m.get("color") or {}).get("color")
              for m in scheme.get("themeMappings", []) if m.get("type") != 14}
    series = next((m.get("colorArray", {}).get("colors") for m in scheme.get("themeMappings", [])
                   if m.get("type") == 14), None)
    return {"colors": colors, "series": [c.get("color") for c in series or []],
            "palette": [row.get("colors") for row in scheme.get("grid", [])]}


class ThemeFromImage:
    def __init__(self, studio: Studio):
        self.studio = studio
        self._shown: dict[str, list[dict[str, Any]]] = {}

    async def _schemes(self, path: str, *, reuse: bool) -> list[dict[str, Any]]:
        try:
            data = await asyncio.to_thread(lambda: Path(path).expanduser().read_bytes())
        except OSError as exc:
            raise StudioError("NOT_FOUND", "Image file not readable") from exc
        if len(data) > MAX_BYTES or not data.startswith(IMAGE_MAGIC):
            # Only real images leave the machine (a wrong path must not upload a file).
            raise StudioError("INVALID_IMAGE", "PNG, JPEG, GIF or WebP up to 10 MB")
        # Generation is not guaranteed deterministic: Apply uses the schemes that were shown.
        key = hashlib.sha256(data).hexdigest()
        if reuse and key in self._shown:
            return self._shown[key]
        page = await self.studio.session.page()
        result = await page.evaluate(_SCHEMES, {
            "endpoint": ENDPOINT, "data": base64.b64encode(data).decode(),
            "timeout": self.studio.config.timeout_ms})
        if result.get("error") or not result.get("schemes"):
            raise StudioError("SCHEME_FAILED", "Datastudio generated no theme from this image "
                              f"({result.get('error') or 'empty'}, HTTP {result.get('status')})")
        self._shown[key] = result["schemes"]
        return result["schemes"]

    async def extract(self, path: str, choice: int | None, expected: str,
                      *, dry_run: bool = True) -> dict[str, Any]:
        schemes = await self._schemes(path, reuse=choice is not None)
        options = [{"choice": i, **_preview(s)} for i, s in enumerate(schemes)]
        if choice is None:
            return {"status": "options", "options": options,
                    "report_fingerprint": await self.fingerprint()}
        if not 0 <= choice < len(schemes):
            raise StudioError("INVALID_CHOICE", f"choice must be 0..{len(schemes) - 1}")
        return {**await NativeOps(self.studio).run(
            _APPLY, {"scheme": schemes[choice]}, page_id=None,
            expected_fingerprint=expected, dry_run=dry_run,
            plan={"choice": options[choice]}), "options": options}

    async def fingerprint(self) -> str:
        return fingerprint(report_content((await self.studio.report())["reportConfig"]))
