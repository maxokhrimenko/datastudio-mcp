import asyncio
import copy

import pytest

from datastudio_mcp.browser import Config, StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.filters import FilterRename, Filters
from datastudio_mcp.report_resources import report_content
from datastudio_mcp.service import Studio

REPORT = "0f0f0f0f-1a1a-4b2b-8c3c-4d4d4d4d4d4d"


class Session:
    def __init__(self, root, mode="success"):
        self.config = Config(REPORT, root, root / "journal")
        self.mode, self.writes, self.reads = mode, [], 0
        self.saved = {"version": "1", "shareable": {"id": REPORT},
            "report": {"lastModifierGaiaId": "server", "opaque": {"future": 3},
                "componentConfig": [{"componentId": "report-c", "unknown": 4}],
                "conceptDefs": [{"name": "keep"}], "resource": {
                    "theme": {"unknown": [1, 2]}, "filter": {"future": True, "entry": [
                        {"key": "f1", "value": {"name": "Same", "definition": {"future": 7}}},
                        {"key": "f2", "value": {"name": "Same"}}]}}},
            "chartCondition": [{"unknown": 8}], "navigationInfo": {"keep": [1]},
            "page": [{"pageId": "p", "version": "1", "page": {"unknown": 9}}]}

    async def post(self, endpoint, body):
        if endpoint == "getReport":
            self.reads += 1
            if self.mode == "conflict" and self.reads == 2:
                self.saved["version"] = "2"
            return {"reportConfig": copy.deepcopy(self.saved)}
        assert endpoint == "updateReport"
        assert body["version"] == self.saved["version"]
        self.writes.append(copy.deepcopy(body))
        request = body["reportRequest"]
        assert "lastModifierGaiaId" not in request
        self.saved["report"] = {k: copy.deepcopy(v) for k, v in request.items()
                                if k not in {"navigationInfo", "chartCondition"}}
        self.saved["report"]["lastModifierGaiaId"] = "server"
        self.saved["chartCondition"] = copy.deepcopy(request["chartCondition"])
        self.saved["navigationInfo"] = copy.deepcopy(request["navigationInfo"])
        self.saved["version"] = str(int(self.saved["version"]) + 1)
        if self.mode == "corrupt":
            self.saved["report"]["resource"]["theme"] = {}
        if self.mode == "page-change":
            self.saved["page"][0]["page"] = {"unexpected": True}
        if self.mode == "timeout":
            raise StudioError("TRANSPORT_FAILURE", "Unknown outcome", outcome_unknown=True)
        return {}


def test_batch_rename_preserves_full_report_and_undo_is_exact(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        studio = Studio(session)
        original = copy.deepcopy(session.saved)
        changes = [FilterRename(filter_id="f2", name="Second"), FilterRename(filter_id="f1", name="First")]
        result = await Filters(studio).rename(changes, fingerprint(session.saved["report"]), dry_run=False)
        assert result["status"] == "verified"
        sent = session.writes[0]["reportRequest"]
        assert sent["componentConfig"] == original["report"]["componentConfig"]
        assert sent["conceptDefs"] == original["report"]["conceptDefs"]
        assert sent["opaque"] == original["report"]["opaque"]
        assert sent["resource"]["theme"] == original["report"]["resource"]["theme"]
        entries = sent["resource"]["filter"]["entry"]
        assert [e["value"]["name"] for e in entries] == ["First", "Second"]
        assert entries[0]["value"]["definition"] == {"future": 7}
        undo = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert undo["status"] == "verified"
        assert report_content(session.saved) == report_content(original)
        assert session.saved["page"] == original["page"]
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,status", [("timeout", "verified"), ("corrupt", "verification_failed")])
def test_timeout_reads_back_without_retry_and_collateral_loss_is_not_success(tmp_path, mode, status):
    async def scenario():
        session = Session(tmp_path, mode)
        result = await Filters(Studio(session)).rename([FilterRename(filter_id="f1", name="New")],
            fingerprint(session.saved["report"]), dry_run=False)
        assert result["status"] == status
        assert len(session.writes) == 1
    asyncio.run(scenario())


def test_conflict_after_journal_prevents_dispatch(tmp_path):
    async def scenario():
        session = Session(tmp_path, "conflict")
        with pytest.raises(StudioError, match="before resource dispatch"):
            await Filters(Studio(session)).rename([FilterRename(filter_id="f1", name="New")],
                fingerprint(session.saved["report"]), dry_run=False)
        assert not session.writes
        assert len(list(session.config.state_dir.glob("*.json"))) == 1
    asyncio.run(scenario())


def test_preview_and_bad_ids_never_write(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        filters = Filters(Studio(session))
        token = fingerprint(session.saved["report"])
        assert (await filters.rename([FilterRename(filter_id="f1", name="New")], token))["status"] == "planned"
        for changes in [[], [FilterRename(filter_id="missing", name="New")],
                        [FilterRename(filter_id="f1", name="A"), FilterRename(filter_id="f1", name="B")]]:
            with pytest.raises(StudioError):
                await filters.rename(changes, token, dry_run=False)
        assert not session.writes and not session.config.state_dir.exists()
    asyncio.run(scenario())


def test_restore_rejects_new_consumer_edits(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        studio = Studio(session)
        result = await Filters(studio).rename([FilterRename(filter_id="f1", name="New")],
            fingerprint(session.saved["report"]), dry_run=False)
        session.saved["page"][0]["page"]["later-edit"] = True
        with pytest.raises(StudioError, match="consumers changed"):
            await studio.restore_operation(result["operation_id"], dry_run=False)
        assert len(session.writes) == 1
    asyncio.run(scenario())


def test_resource_restore_cannot_claim_to_repair_page_side_effects(tmp_path):
    async def scenario():
        session = Session(tmp_path, "page-change")
        studio = Studio(session)
        result = await Filters(studio).rename([FilterRename(filter_id="f1", name="New")],
            fingerprint(session.saved["report"]), dry_run=False)
        assert result["status"] == "verification_failed"
        assert result["mismatches"] == ["/protected_content"]
        with pytest.raises(StudioError, match="recovery separately"):
            await studio.restore_operation(result["operation_id"], dry_run=False)
        session.saved["report"]["resource"]["filter"]["entry"][0]["value"]["name"] = "Same"
        with pytest.raises(StudioError, match="recovery separately"):
            await studio.restore_operation(result["operation_id"], dry_run=False)
        assert len(session.writes) == 1
    asyncio.run(scenario())
