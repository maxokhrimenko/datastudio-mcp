import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from datastudio_mcp.browser import StudioError
from datastudio_mcp.checkbox import Checkbox


class Studio:
    def __init__(self, outcome="success"):
        self.config = SimpleNamespace(report_id="r", timeout_ms=1000)
        self.current = dict(page_id="p", component_id="c", document_id="doc",
                            editor_page_version="1", page_fingerprint="saved",
                            interaction_fingerprint="before", can_reset=False,
                            on_select_value=None,
                            checkbox=dict(status="available", value=None, enabled=True))
        self.initial = copy.deepcopy(self.current)
        self.records, self.dispatches, self.outcome = [], 0, outcome
        self.session = SimpleNamespace(page=self.page)

    @asynccontextmanager
    async def mutation_lock(self):
        yield

    async def page(self):
        return self

    def _journal(self, operation_id, record):
        self.records.append(copy.deepcopy(record))

    async def inspect(self, page_id, component_id):
        return copy.deepcopy(self.current)

    async def evaluate(self, script, args):
        self.dispatches += 1
        assert self.records[-1]["state"] == "dispatched"
        if self.outcome != "rejected":
            if args["reset"]:
                self.current = copy.deepcopy(self.initial)
            else:
                self.current["checkbox"]["value"] = args["value"]
                self.current["can_reset"] = True
                self.current["interaction_fingerprint"] = "after"
                if self.outcome != "ui-only":
                    self.current["on_select_value"] = {"filterDefinition": {
                        "filterExpression": {"filterConditionType": "T" if args["value"] else "F"}
                    }} if args["value"] is not None else {}
        if self.outcome == "timeout":
            raise TimeoutError()
        return {"error": "VERSION_CONFLICT"} if self.outcome == "rejected" else {"applied": True}


def setup(outcome="success"):
    studio = Studio(outcome)
    checkbox = Checkbox(studio)
    checkbox.interactions = studio
    return studio, checkbox


def test_preview_and_conflict_never_dispatch_or_journal():
    async def scenario():
        studio, checkbox = setup()
        assert (await checkbox.set("p", "c", True, "before"))["status"] == "planned"
        with pytest.raises(StudioError, match="changed"):
            await checkbox.set("p", "c", True, "wrong", dry_run=False)
        assert not studio.records and studio.dispatches == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome,status", [("success", "verified"), ("timeout", "verified"),
                                             ("rejected", "verification_failed"),
                                             ("ui-only", "verification_failed")])
def test_write_is_journalled_read_back_and_never_retried(outcome, status):
    async def scenario():
        studio, checkbox = setup(outcome)
        result = await checkbox.set("p", "c", True, "before", dry_run=False)
        assert result["status"] == status
        assert studio.dispatches == 1
        assert [r["state"] for r in studio.records] == ["prepared", "dispatched", status]
        assert studio.records[0]["before"] == studio.initial
    asyncio.run(scenario())


def test_restore_exact_and_expired_document_guard():
    async def scenario():
        studio, checkbox = setup()
        await checkbox.set("p", "c", True, "before", dry_run=False)
        record = copy.deepcopy(studio.records[-1])
        studio.current["document_id"] = "new"
        with pytest.raises(StudioError, match="replaced"):
            await checkbox.restore(record, dry_run=False)
        assert studio.dispatches == 1
        studio.current = copy.deepcopy(record["after"])
        assert (await checkbox.restore(record, dry_run=False))["status"] == "verified"
        assert studio.current == studio.initial
        assert studio.dispatches == 2
    asyncio.run(scenario())
