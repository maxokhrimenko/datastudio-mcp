import asyncio

import pytest

from datastudio_mcp.browser import StudioError
from datastudio_mcp.visual import Visual


NAVIGATION = {"navItems": [
    {"page": {"pageId": "p_visible"}},
    {"page": {"pageId": "p_hidden", "hiddenFromViewer": True}},
    {"section": {"hiddenFromViewer": True, "navItems": [{"page": {"pageId": "p_in_hidden"}}]}},
    {"page": {"pageId": "89400174"}},
]}


class Tab:
    def __init__(self, url):
        self.url = url

    async def evaluate(self, script, page_id):
        return {"canvas": False}


class Session:
    def __init__(self, token):
        self.token = token

    async def acquire(self, page_id=None):
        return Tab(f"https://datastudio.google.com/reporting/r/page/{self.token}")


class Studio:
    def __init__(self, token="p_visible"):
        self.session = Session(token)

    async def inventory(self):
        return {"navigation": NAVIGATION}


@pytest.mark.parametrize("page_id", ["p_hidden", "p_in_hidden", "p_unknown"])
def test_screenshot_of_a_page_the_viewer_redirects_away_from_is_refused(page_id):
    with pytest.raises(StudioError) as caught:
        asyncio.run(Visual(Studio()).page_screenshot(page_id))
    assert caught.value.code == "PAGE_NOT_SHOWN"


def test_a_visible_page_under_an_older_url_token_is_captured():
    # An older report shows page 89400174 at /page/SEHDG: the capture goes on (and here
    # stops at the canvas check, past the redirect guard).
    with pytest.raises(StudioError) as caught:
        asyncio.run(Visual(Studio("SEHDG")).page_screenshot("89400174"))
    assert caught.value.code == "EDITOR_NOT_READY"
