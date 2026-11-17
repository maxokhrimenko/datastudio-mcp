import asyncio

import pytest

from datastudio_mcp.browser import StudioError
from datastudio_mcp.visual import Visual


class Tab:
    url = "https://datastudio.google.com/reporting/r/page/p_visible"


class Session:
    async def acquire(self, page_id=None):
        return Tab()


class Studio:
    session = Session()


def test_screenshot_of_a_page_the_viewer_redirects_away_from_is_refused():
    with pytest.raises(StudioError) as caught:
        asyncio.run(Visual(Studio()).page_screenshot("p_hidden"))
    assert caught.value.code == "PAGE_NOT_SHOWN"
