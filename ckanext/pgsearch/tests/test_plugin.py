import pytest
from types import SimpleNamespace

from ckan.plugins import plugin_loaded
import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit
from ckan.lib import search
import ckan.lib.search.index as search_index

import ckanext.pgsearch.plugin as plugin


@pytest.mark.ckan_config("ckan.plugins", "pgsearch")
@pytest.mark.usefixtures("with_plugins")
def test_plugin_loads():
    assert plugin_loaded("pgsearch")


def test_actions_are_registered():
    actions = plugin.PgsearchPlugin().get_actions()

    assert "package_search" in actions
    assert "package_autocomplete" in actions
    assert "organization_list" in actions
    assert "group_list" in actions


def test_update_config_replaces_direct_package_index(monkeypatch):
    monkeypatch.setattr(toolkit, "add_template_directory", lambda config, path: None)
    monkeypatch.setattr(toolkit, "add_public_directory", lambda config, path: None)
    monkeypatch.setattr(toolkit, "add_resource", lambda path, name: None)

    plugin.PgsearchPlugin().update_config({})

    assert search._INDICES["package"] is plugin.NoopPackageSearchIndex
    assert search.PackageSearchIndex is plugin.NoopPackageSearchIndex
    assert search_index.PackageSearchIndex is plugin.NoopPackageSearchIndex

    package_index = search.PackageSearchIndex()
    package_index.index_package({"id": "dataset-id"})


@pytest.mark.parametrize("raw_query, expected", [
    (None, ""),
    ("", ""),
    ("   ", ""),
    ("*", ""),
    ("*:*", ""),
    (" search terms ", "search terms"),
])
def test_normalize_text_query(raw_query, expected):
    assert plugin._normalize_text_query(raw_query) == expected


def test_extract_filter_clauses_skips_internal_filters():
    clauses = plugin._extract_filter_clauses(
        {
            "fq": 'tags:(climate OR water) +owner_org:dept-1 state:active',
            "fq_list": ['capacity:public license_id:"cc-by"'],
        }
    )

    assert clauses == [
        ("tags", ["climate", "water"]),
        ("owner_org", ["dept-1"]),
        ("license_id", ["cc-by"]),
    ]


def test_parse_group_sort_normalizes_package_count_alias():
    assert plugin._parse_group_sort("packages") == ("package_count", "desc")
    assert plugin._parse_group_sort("title desc") == ("title", "desc")


def test_parse_group_sort_rejects_invalid_field():
    with pytest.raises(toolkit.ValidationError):
        plugin._parse_group_sort("created desc")


def test_get_index_returns_ckan_index_document(monkeypatch):
    pkg_dict = {
        "id": "dataset-id",
        "name": "dataset-name",
        "metadata_modified": "2026-05-11T16:00:00.000000",
        "title": "Dataset title",
    }

    monkeypatch.setattr(
        toolkit,
        "get_action",
        lambda action_name: (lambda context, data_dict: pkg_dict),
    )

    result = plugin.PostgresPackageSearchQuery().get_index("dataset-id")

    assert result["id"] == "dataset-id"
    assert result["name"] == "dataset-name"
    assert result["metadata_modified"] == pkg_dict["metadata_modified"]
    assert result["data_dict"] == result["validated_data_dict"]
    assert '"title": "Dataset title"' in result["data_dict"]


def test_package_search_returns_api_action_shape(monkeypatch):
    data_dict = {"q": "*:*", "rows": 5, "sort": "metadata_modified desc"}

    monkeypatch.setattr(plugin.logic_schema, "default_package_search_schema", lambda: {})
    monkeypatch.setattr(toolkit, "navl_validate", lambda data, schema, context: (data, {}))
    monkeypatch.setattr(toolkit, "check_access", lambda action, context, data: None)
    monkeypatch.setattr(plugins, "PluginImplementations", lambda iface: [])
    monkeypatch.setattr(plugin, "_search_query_base", lambda data: object())
    monkeypatch.setattr(plugin, "_apply_supported_filter_clauses", lambda query, clauses: (query, []))
    monkeypatch.setattr(plugin, "_apply_text_search", lambda query, text: (query, None))
    monkeypatch.setattr(plugin, "_apply_sort", lambda query, data, rank: query)
    monkeypatch.setattr(plugin, "_query_count", lambda query: 2)
    monkeypatch.setattr(plugin, "_query_package_ids", lambda query, start=0, rows=None: ["dataset-1"])
    monkeypatch.setattr(
        plugin,
        "_iter_visible_results",
        lambda context, package_ids, fl=None: [{"id": "dataset-1", "name": "dataset-1"}],
    )
    monkeypatch.setattr(plugin, "has_request_context", lambda: True)
    monkeypatch.setattr(plugin, "request", SimpleNamespace(path="/api/action/package_search"))

    result = plugin.package_search({}, data_dict)

    assert result == {
        "count": 2,
        "results": [{"id": "dataset-1", "name": "dataset-1"}],
        "sort": "metadata_modified desc",
    }


def test_package_search_returns_standard_shape_outside_api_action(monkeypatch):
    data_dict = {"q": "*:*", "rows": 5, "order_by": "title_string asc"}

    monkeypatch.setattr(plugin.logic_schema, "default_package_search_schema", lambda: {})
    monkeypatch.setattr(toolkit, "navl_validate", lambda data, schema, context: (data, {}))
    monkeypatch.setattr(toolkit, "check_access", lambda action, context, data: None)
    monkeypatch.setattr(plugins, "PluginImplementations", lambda iface: [])
    monkeypatch.setattr(plugin, "_search_query_base", lambda data: object())
    monkeypatch.setattr(plugin, "_apply_supported_filter_clauses", lambda query, clauses: (query, []))
    monkeypatch.setattr(plugin, "_apply_text_search", lambda query, text: (query, None))
    monkeypatch.setattr(plugin, "_apply_sort", lambda query, data, rank: query)
    monkeypatch.setattr(plugin, "_query_count", lambda query: 1)
    monkeypatch.setattr(plugin, "_query_package_ids", lambda query, start=0, rows=None: ["dataset-1"])
    monkeypatch.setattr(
        plugin,
        "_iter_visible_results",
        lambda context, package_ids, fl=None: [{"id": "dataset-1"}],
    )
    monkeypatch.setattr(plugin, "has_request_context", lambda: False)

    result = plugin.package_search({}, data_dict)

    assert result == {
        "count": 1,
        "facets": {},
        "results": [{"id": "dataset-1"}],
        "sort": "title_string asc",
        "search_facets": {},
    }
