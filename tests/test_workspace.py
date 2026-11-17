from types import SimpleNamespace

import pytest

from datastudio_mcp.browser import Browser, BrowserConfig, StudioError
from datastudio_mcp.workspace import Workspace

REPORT = "0f0f0f0f-1a1a-4b2b-8c3c-4d4d4d4d4d4d"
OTHER = "11111111-2222-4333-8444-555555555555"


def workspace(tmp_path):
    built = []

    def factory(session):
        built.append(session.config.report_id)
        return SimpleNamespace(session=session, name=None)

    browser = Browser(BrowserConfig(tmp_path, tmp_path / "journal"))
    return Workspace(browser, factory=factory), built


def test_resolve_requires_an_active_or_explicit_report(tmp_path):
    ws, built = workspace(tmp_path)
    with pytest.raises(StudioError) as error:
        ws.resolve(None)
    assert error.value.code == "NO_ACTIVE_REPORT"
    assert built == []


def test_resolve_builds_each_report_once_and_isolates_them(tmp_path):
    ws, built = workspace(tmp_path)
    first = ws.resolve(REPORT)
    assert first.session.config.report_id == REPORT
    assert first.session.config.state_dir == tmp_path / "journal"
    ws.active = OTHER
    second = ws.resolve(None)
    assert second.session.config.report_id == OTHER
    assert ws.resolve(REPORT) is first
    assert ws.resolve(OTHER) is second
    assert built == [REPORT, OTHER]


def test_resolve_rejects_malformed_ids(tmp_path):
    ws, _ = workspace(tmp_path)
    with pytest.raises(StudioError) as error:
        ws.resolve("report")
    assert error.value.code == "INVALID_REPORT_REFERENCE"


def test_name_is_known_only_after_a_bundle_learned_it(tmp_path):
    ws, _ = workspace(tmp_path)
    assert ws.name(REPORT) is None
    ws.resolve(REPORT).name = "Weekly"
    assert ws.name(REPORT) == "Weekly"


def test_default_factory_builds_the_full_service_bundle(tmp_path):
    browser = Browser(BrowserConfig(tmp_path, tmp_path / "journal"))
    bundle = Workspace(browser).resolve(REPORT)
    assert bundle.studio.config.report_id == REPORT
    assert bundle.components.studio is bundle.studio
    assert bundle.blend_joins.studio is bundle.studio
