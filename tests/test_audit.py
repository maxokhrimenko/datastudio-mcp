import copy

from datastudio_mcp.audit import RULES, audit_saved, live_findings


def field(name, formula=None, created_by=2, enabled=True):
    item = {"field": {"ns": "t0", "name": name}, "outputName": name, "enabled": enabled,
            "createdBy": created_by}
    if formula is not None:
        item["textFormula"] = formula
    return item


def source(source_id, fields, columns=("event_type", "outcome"), delegated=True, draft=None):
    published = {"datasourceBlock": {"fields": fields},
                 "blocks": [{"fields": [{"field": {"name": c}} for c in columns]}],
                 "delegatedAccessEnabled": delegated}
    return {"source": {"id": source_id, "name": source_id},
            "published": published,
            "draft": draft if draft is not None else copy.deepcopy(published)}


def concept(name, source_field=None, formula=None):
    data = {"sourceFieldName": source_field} if formula is None else {
        "sourceFieldName": name, "textFormula": formula, "sourceType": 1}
    return {"name": name, "queryTimeTransformation": {"dataTransformation": data}}


def expression(source_field=None, formula=None):
    data = {"sourceFieldName": source_field} if formula is None else {
        "sourceFieldName": "qt_x", "textFormula": formula, "sourceType": 1}
    return {"filterExpression": {"filterConditionType": "T",
                                 "queryTimeTransformation": {"dataTransformation": data}}}


def chart(component_id, source_id, concepts, filters=(), used=None, dataset_type=1):
    names = used if used is not None else [c["name"] for c in concepts]
    return {"componentId": component_id, "type": "simple-barchart", "datasourceId": source_id,
            "conceptDefs": concepts,
            "propertyConfig": {"componentProperty": {
                "dataset": {"datasetType": dataset_type, "datasetId": source_id},
                "dimensions": {"labeledConcepts": [{"value": {"conceptNames": names}}]},
                "filters": list(filters)}}}


def report(components, filters=None, blends=None):
    return {"report": {"componentConfig": [], "resource": {
                "filter": {"entry": [{"key": k, "value": v} for k, v in (filters or {}).items()]},
                "dataViewResource": {"entry": [{"key": k, "value": v}
                                               for k, v in (blends or {}).items()]}}},
            "page": [{"pageId": "p1", "page": {"componentConfig": components}}]}


EVENTS = [field("_event_type_", "t0.event_type", created_by=1),
          field("_outcome_", "t0.outcome", created_by=1),
          field("is_final", "t0._event_type_ = 'end' AND t0._outcome_ IN ('ok')")]


def rules(findings):
    return sorted(f["rule"] for f in findings)


def test_healthy_report_has_no_findings():
    config = report([chart("c1", "ev", [concept("qt_a", "_event_type_")], filters=["f1"])],
                    filters={"f1": {"datasourceId": "ev",
                                    "filterDefinition": expression("is_final")}})
    assert audit_saved(config, {"ev": source("ev", EVENTS)}) == []


def test_filter_on_new_field_formula_is_the_original_defect():
    # The Employee Lifecycle incident: a filter built on an ad-hoc formula.
    definition = expression(formula="t0._event_type_ = 'end'")
    config = report([chart("c1", "ev", [concept("qt_a", "_event_type_")], filters=["f1"])],
                    filters={"f1": {"name": "Final", "datasourceId": "ev",
                                    "filterDefinition": definition}})
    [item] = audit_saved(config, {"ev": source("ev", EVENTS)})
    assert item["rule"] == "filter.inline_formula" and item["severity"] == "error"
    assert item["location"] == {"filter_id": "f1", "filter": "Final"}


def test_filter_rules_cover_missing_detached_unused_dangling_and_not_applicable():
    nested = {"and": {"filterDefinitions": [expression("_event_type_"), expression("gone")]}}
    filters = {
        "missing": {"datasourceId": "ev", "filterDefinition": nested},
        "detached": {"datasourceId": "other", "filterDefinition": expression("x")},
        "elsewhere": {"datasourceId": "hr", "filterDefinition": expression("_grade_")},
    }
    config = report([chart("c1", "ev", [concept("qt_a", "_event_type_")],
                           filters=["missing", "ghost", "elsewhere"])], filters=filters)
    sources = {"ev": source("ev", EVENTS), "hr": source("hr", [field("_grade_", "t0.g", 1)],
                                                         columns=("g",))}
    found = audit_saved(config, sources)
    assert rules(found) == ["filter.dangling_reference", "filter.detached_datasource",
                            "filter.missing_field", "filter.not_applicable",
                            "filter.not_applicable", "filter.unused"]
    by_rule = {f["rule"]: f for f in found}
    assert by_rule["filter.missing_field"]["fields"] == ["gone"]
    assert by_rule["filter.detached_datasource"]["referenced"] is False
    assert by_rule["filter.dangling_reference"]["location"]["filter_id"] == "ghost"


def test_component_rules_ignore_orphans_and_local_concepts():
    concepts = [concept("qt_a", "_event_type_"), concept("qt_gone", "dropped"),
                concept("qt_orphan", "also_dropped"),
                concept("qt_f", formula="CONCAT(t0._outcome_, t0.qt_a)")]
    config = report([chart("c1", "ev", concepts, used=["qt_a", "qt_gone", "qt_f"]),
                     chart("c2", "nowhere", [concept("qt_b", "_x_")])])
    found = audit_saved(config, {"ev": source("ev", EVENTS)})
    assert rules(found) == ["component.chart_formula", "component.detached_datasource",
                            "component.missing_field"]
    missing = next(f for f in found if f["rule"] == "component.missing_field")
    assert missing["fields"] == ["dropped"] and missing["concepts"] == ["qt_gone"]


def test_datasource_rules_drafts_credentials_and_broken_fields():
    fields = [*EVENTS, field("_dropped_", "t0.removed_column", created_by=1),
              field("calc", "t0._event_type_ + t0._missing_"),
              field("off", "t0._event_type_", enabled=False),
              field("uses_off", "t0.off")]
    changed = source("ev", fields, delegated=False)
    changed["draft"]["datasourceBlock"]["fields"].append(field("new"))
    found = audit_saved(report([]), {"ev": changed})
    assert rules(found) == ["datasource.broken_field", "datasource.broken_field",
                            "datasource.broken_field", "datasource.unpublished_draft",
                            "datasource.viewer_credentials"]
    broken = {f["location"]["field"]: f["missing_references"] for f in found
              if f["rule"] == "datasource.broken_field"}
    assert broken == {"_dropped_": ["removed_column"], "calc": ["_missing_"], "uses_off": ["off"]}


def test_blend_rules_and_blend_bound_fields():
    blend = {"blockDatasource": {
        "datasourceBlock": {"name": "B", "fields": [field("month_dv0")]},
        "blocks": [{"treeQueryBlockConfig": {"join": {
            "left": {"query": {"datasourceId": "ev"}},
            "right": {"query": {"datasourceId": "gone"}}}}}]}}
    unused = {"blockDatasource": {"datasourceBlock": {"name": "U", "fields": []},
                                  "blocks": [{"treeQueryBlockConfig": {"query": {}}}]}}
    components = [chart("c1", "b1", [concept("qt_m", "month_dv0"), concept("qt_d", "date_dv0")],
                        dataset_type=2),
                  chart("c2", "b9", [concept("qt_z", "x")], dataset_type=2)]
    found = audit_saved(report(components, blends={"b1": blend, "b2": unused}),
                        {"ev": source("ev", EVENTS)})
    assert rules(found) == ["blend.dangling_reference", "blend.detached_input", "blend.issue",
                            "blend.unused", "component.missing_field"]
    missing = next(f for f in found if f["rule"] == "component.missing_field")
    assert missing["fields"] == ["date_dv0"]


def test_live_errors_and_unsettled_components():
    flags = {"datasource": False, "access_denied": False, "configuration": True,
             "system": False, "viewers_credentials": False}
    components = [
        {"component_id": "ok", "type": "kpi", "scope": "page", "datasource_id": "ev",
         "settled": True, "has_error": False, "category": 0, "flags": flags, "error": None},
        {"component_id": "bad", "type": "table", "scope": "report", "datasource_id": "ev",
         "settled": True, "has_error": True, "category": 0, "flags": flags,
         "error": {"reason": 269, "title": "Invalid filter"}},
        {"component_id": "slow", "type": "table", "scope": "page", "datasource_id": "ev",
         "settled": False, "has_error": False, "category": 0, "flags": flags, "error": None},
    ]
    found = live_findings("p1", components)
    assert rules(found) == ["render.error", "render.not_settled"]
    error = next(f for f in found if f["rule"] == "render.error")
    assert error["location"]["page_id"] is None and error["error"]["title"] == "Invalid filter"


def test_owner_credentials_are_flagged_and_service_accounts_are_not():
    owned = source("ev", EVENTS)
    owned["credentials"] = {"type": "owner", "account": "A Person"}
    assert rules(audit_saved(report([]), {"ev": owned})) == ["datasource.owner_credentials"]
    owned["credentials"] = {"type": "service_account", "account": "r@p.iam.gserviceaccount.com"}
    assert audit_saved(report([]), {"ev": owned}) == []


def test_every_rule_documents_severity_meaning_and_fix():
    assert all(severity in {"error", "warning", "info"} and meaning and fix
               for severity, meaning, fix in RULES.values())


def test_stale_sort_is_a_sort_on_a_field_the_chart_does_not_show():
    from datastudio_mcp.audit import stale_sort

    def value(v):
        if isinstance(v, dict):
            return {"mapValue": {"entry": [{"key": k, "value": value(x)} for k, x in v.items()]}}
        if isinstance(v, list):
            return {"arrayValue": {"value": [value(x) for x in v]}}
        return {"strValue": v}

    def component(column):
        return {"propertyConfig": {"componentProperty": {
                    "dimensions": {"labeledConcepts": [{"key": "primary",
                                   "value": {"conceptNames": ["qt_month"]}}]},
                    "metrics": {"labeledConcepts": [{"key": "primary",
                                "value": {"conceptNames": ["qt_m"]}}]}}},
                "behavior": value({"onSort": [{"action": "sort", "init": {"sortOptions": {
                    "sortData": [{"sortDir": "1", "sortColumn": {"name": column}}]}}}]})}

    assert stale_sort(component("qt_month")) == []
    assert stale_sort(component("qt_gone")) == ["qt_gone"]
    panel = component("qt_hidden")
    panel["propertyConfig"]["componentProperty"]["sort"] = [{"sortColumn": "qt_hidden"}]
    assert stale_sort(panel) == []
