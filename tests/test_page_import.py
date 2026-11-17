import base64

import pytest

from datastudio_mcp.page_import import _datasets, _remap, decode_payload


def test_image_payload_uses_the_editors_web_safe_base64():
    raw = bytes(range(256)) * 3
    websafe = base64.b64encode(raw).decode().replace("+", "-").replace("/", "_").replace("=", ".")
    assert decode_payload(websafe) == raw
    with pytest.raises(ValueError):  # never silently drop characters (corrupt images)
        decode_payload("not*base64")


def test_page_links_follow_the_imported_pages_only():
    body = {"a": [{"internalLink": "p_src1"}, {"internalLink": "p_other"}], "n": 3,
            "text": "p_src1 inside text stays"}
    assert _remap(body, {"p_src1": "p_new1"}) == {
        "a": [{"internalLink": "p_new1"}, {"internalLink": "p_other"}], "n": 3,
        "text": "p_src1 inside text stays"}


def test_datasets_split_datasources_and_blends():
    found = {"datasources": set(), "blends": set()}
    _datasets({"c": [{"datasourceId": "ds1", "dataset": {"datasetType": 1, "datasetId": "ds1"}},
                     {"dataset": {"datasetType": 2, "datasetId": "dataViewResourcex"}},
                     {"datasourceId": ""}]}, found)
    assert found == {"datasources": {"ds1"}, "blends": {"dataViewResourcex"}}
