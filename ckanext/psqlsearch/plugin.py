from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from flask import has_request_context, request
from sqlalchemy import asc, cast, desc, func, or_, String
from sqlalchemy.orm import Query

import ckan.model as model
import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit
from ckan.common import config
from ckan.lib import search
import ckan.lib.search.common as search_common
from ckan.lib.search.index import NoopSearchIndex
from ckan.lib.search.query import SearchQuery
import ckan.logic.schema as logic_schema


def _asbool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_text_query(raw_query: Optional[str]) -> str:
    if not raw_query:
        return ""
    query = raw_query.strip()
    if query in {"*:*", "*"}:
        return ""
    return query


def _search_document() -> Any:
    return func.concat_ws(
        " ",
        func.coalesce(model.Package.name, ""),
        func.coalesce(model.Package.title, ""),
        func.coalesce(model.Package.notes, ""),
        func.coalesce(model.Package.author, ""),
        func.coalesce(model.Package.maintainer, ""),
    )


def _search_query_base(data_dict: dict[str, Any]) -> Query[Any]:
    session = model.Session
    query = session.query(model.Package.id)

    include_deleted = _asbool(data_dict.get("include_deleted"))
    include_drafts = _asbool(data_dict.get("include_drafts"))

    states = ["active"]
    if include_drafts:
        states.append("draft")
    if include_deleted:
        states.append("deleted")

    query = query.filter(model.Package.state.in_(states))
    query = query.filter(model.Package.type == "dataset")
    if not _asbool(data_dict.get("include_private")):
        query = query.filter(model.Package.private.is_(False))
    return query


def _apply_text_search(
    query: Query[Any], text_query: str
) -> tuple[Query[Any], Optional[Any]]:
    if not text_query:
        return query, None

    document = _search_document()
    ts_query = func.plainto_tsquery("simple", text_query)
    rank = func.ts_rank_cd(func.to_tsvector("simple", document), ts_query)

    query = query.filter(
        or_(
            func.to_tsvector("simple", document).op("@@")(ts_query),
            cast(model.Package.name, String).ilike(f"%{text_query}%"),
            cast(model.Package.title, String).ilike(f"%{text_query}%"),
            cast(model.Package.notes, String).ilike(f"%{text_query}%"),
        )
    )
    return query, rank.label("rank")


def _apply_sort(
    query: Query[Any], data_dict: dict[str, Any], rank_column: Optional[Any]
) -> Query[Any]:
    sort = data_dict.get("sort") or config.get("ckan.search.default_package_sort")
    sort = (sort or "metadata_modified desc").strip()

    if sort in {"rank", "score desc, metadata_modified desc", "score desc"}:
        if rank_column is not None:
            return query.order_by(desc(rank_column), desc(model.Package.metadata_modified))
        return query.order_by(desc(model.Package.metadata_modified))

    field_map = {
        "metadata_modified": model.Package.metadata_modified,
        "metadata_created": model.Package.metadata_created,
        "title": model.Package.title,
        "name": model.Package.name,
    }

    order_clauses = []
    for raw_clause in sort.split(","):
        parts = raw_clause.strip().split()
        if not parts:
            continue
        field_name = parts[0]
        direction = parts[1].lower() if len(parts) > 1 else "asc"
        column = field_map.get(field_name)
        if column is None:
            continue
        order_clauses.append(desc(column) if direction == "desc" else asc(column))

    if not order_clauses:
        order_clauses.append(desc(model.Package.metadata_modified))
    return query.order_by(*order_clauses)


def _iter_visible_results(
    context: dict[str, Any], package_ids: Iterable[str], fl: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    show_action = toolkit.get_action("package_show")
    results: list[dict[str, Any]] = []

    show_context = {
        "model": model,
        "session": model.Session,
        "user": context.get("user"),
        "auth_user_obj": context.get("auth_user_obj"),
        "ignore_auth": context.get("ignore_auth", False),
        "for_view": context.get("for_view", False),
        "validate": False,
        "use_cache": False,
    }

    for package_id in package_ids:
        try:
            pkg_dict = show_action(show_context, {"id": package_id})
        except (toolkit.NotAuthorized, toolkit.ObjectNotFound):
            continue

        if fl:
            result = {field: pkg_dict.get(field) for field in fl}
            if "id" not in result:
                result["id"] = pkg_dict.get("id")
            results.append(result)
        else:
            if context.get("for_view"):
                for item in plugins.PluginImplementations(plugins.IPackageController):
                    pkg_dict = item.before_dataset_view(pkg_dict)
            results.append(pkg_dict)

    return results


def _split_result_field_list(fl: Any) -> Optional[list[str]]:
    if not fl:
        return None
    if isinstance(fl, str):
        return fl.split()
    return list(fl)


def _normalize_facet_fields(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _normalize_fq_values(raw_value: str) -> list[str]:
    value = raw_value.strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    parts = [part.strip().strip('"') for part in value.split(" OR ")]
    return [part for part in parts if part]


def _extract_filter_clauses(data_dict: dict[str, Any]) -> list[tuple[str, list[str]]]:
    clauses: list[tuple[str, list[str]]] = []
    raw_filters = []
    if data_dict.get("fq"):
        raw_filters.append(data_dict["fq"])
    raw_filters.extend(data_dict.get("fq_list", []))

    for raw_filter in raw_filters:
        for token in str(raw_filter).split():
            token = token.strip()
            if not token or ":" not in token:
                continue
            if token.startswith("+"):
                token = token[1:]
            field, raw_value = token.split(":", 1)
            if field in {"site_id", "state", "capacity", "permission_labels"}:
                continue
            clauses.append((field, _normalize_fq_values(raw_value)))
    return clauses


def _field_values(pkg_dict: dict[str, Any], field: str) -> list[str]:
    if field == "groups":
        return [
            value
            for group in pkg_dict.get("groups", [])
            for value in (group.get("name"), group.get("id"), group.get("title"))
            if value
        ]
    if field in {"organization", "owner_org"}:
        values = [pkg_dict.get("owner_org")]
        organization = pkg_dict.get("organization")
        if isinstance(organization, dict):
            values.extend(
                [organization.get("name"), organization.get("id"), organization.get("title")]
            )
        return [value for value in values if value]
    if field == "tags":
        return [
            value
            for tag in pkg_dict.get("tags", [])
            for value in (tag.get("name"), tag.get("display_name"))
            if value
        ]

    value = pkg_dict.get(field)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _matches_filter_clauses(
    pkg_dict: dict[str, Any], clauses: list[tuple[str, list[str]]]
) -> bool:
    for field, accepted_values in clauses:
        package_values = _field_values(pkg_dict, field)
        if not package_values:
            return False
        if not any(value in package_values for value in accepted_values):
            return False
    return True


def _facet_counts(
    package_dicts: list[dict[str, Any]], facet_fields: list[str]
) -> dict[str, dict[str, int]]:
    facets: dict[str, dict[str, int]] = {}
    for field in facet_fields:
        counts: dict[str, int] = {}
        for pkg_dict in package_dicts:
            for value in _field_values(pkg_dict, field):
                counts[value] = counts.get(value, 0) + 1
        facets[field] = counts
    return facets


def _postgres_package_dicts(
    package_ids: list[str], context: Optional[dict[str, Any]] = None
) -> list[dict[str, Any]]:
    if context is not None:
        return _iter_visible_results(context, package_ids)

    show_action = toolkit.get_action("package_show")
    results: list[dict[str, Any]] = []
    for package_id in package_ids:
        try:
            pkg_dict = show_action(
                {
                    "model": model,
                    "session": model.Session,
                    "ignore_auth": True,
                    "validate": False,
                    "use_cache": False,
                },
                {"id": package_id},
            )
        except (toolkit.NotAuthorized, toolkit.ObjectNotFound):
            continue
        results.append(pkg_dict)
    return results


class PostgresPackageSearchQuery(SearchQuery):
    def get_all_entity_ids(self, max_results: int = 1000) -> list[str]:
        query = _search_query_base({})
        query = query.order_by(desc(model.Package.metadata_modified))
        return [package_id for (package_id,) in query.limit(max_results).all()]

    def get_index(self, reference: str) -> dict[str, Any]:
        show_action = toolkit.get_action("package_show")
        pkg_dict = show_action(
            {
                "model": model,
                "session": model.Session,
                "ignore_auth": True,
                "validate": False,
                "use_cache": False,
            },
            {"id": reference},
        )
        metadata_modified = pkg_dict.get("metadata_modified")
        return {
            "id": pkg_dict.get("id"),
            "name": pkg_dict.get("name"),
            "metadata_modified": metadata_modified,
            "data_dict": json.dumps(pkg_dict),
            "validated_data_dict": json.dumps(pkg_dict),
        }

    def run(
        self,
        query: dict[str, Any],
        permission_labels: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        text_query = _normalize_text_query(query.get("q"))
        db_query = _search_query_base(
            {"include_private": False, "include_deleted": False, "include_drafts": False}
        )
        db_query, rank_column = _apply_text_search(db_query, text_query)
        if rank_column is not None:
            db_query = db_query.add_columns(rank_column)
        db_query = _apply_sort(db_query, query, rank_column)

        rows = int(query.get("rows", 10))
        package_ids = [row[0] for row in db_query.all()]
        package_dicts = _postgres_package_dicts(package_ids)

        filter_clauses = _extract_filter_clauses(query)
        if filter_clauses:
            package_dicts = [
                pkg_dict
                for pkg_dict in package_dicts
                if _matches_filter_clauses(pkg_dict, filter_clauses)
            ]

        self.count = len(package_dicts)
        facet_fields = _normalize_facet_fields(query.get("facet.field"))
        self.facets = _facet_counts(package_dicts, facet_fields)

        result_fields = _split_result_field_list(query.get("fl"))
        results: list[Any]
        if result_fields:
            results = []
            for pkg_dict in package_dicts[:rows]:
                result = {field: pkg_dict.get(field) for field in result_fields}
                if len(result_fields) == 1 and result_fields[0] in {"id", "name"}:
                    results.append(result.get(result_fields[0]))
                else:
                    results.append(result)
        else:
            results = package_dicts[:rows]

        self.results = results
        return {"results": self.results, "count": self.count}


def package_search(context: dict[str, Any], data_dict: dict[str, Any]) -> dict[str, Any]:
    schema = context.get("schema") or logic_schema.default_package_search_schema()
    data_dict, errors = toolkit.navl_validate(data_dict, schema, context)
    data_dict.update(data_dict.get("__extras", {}))
    data_dict.pop("__extras", None)
    if errors:
        raise toolkit.ValidationError(errors)

    toolkit.check_access("package_search", context, data_dict)

    data_dict["extras"] = data_dict.get("extras", {})
    for key in [key for key in list(data_dict.keys()) if key.startswith("ext_")]:
        data_dict["extras"][key] = data_dict.pop(key)

    data_dict["df"] = "text"
    for item in plugins.PluginImplementations(plugins.IPackageController):
        data_dict = item.before_dataset_search(data_dict)

    if data_dict.get("abort_search", False):
        empty = {"count": 0, "facets": {}, "results": [], "sort": data_dict.get("sort"), "search_facets": {}}
        for item in plugins.PluginImplementations(plugins.IPackageController):
            empty = item.after_dataset_search(empty, data_dict)
        return empty

    text_query = _normalize_text_query(data_dict.get("q"))
    rows = min(int(data_dict.get("rows") or 10), int(config.get("ckan.search.rows_max", 1000)))
    start = max(int(data_dict.get("start") or 0), 0)
    fl = _split_result_field_list(data_dict.get("fl"))

    query = _search_query_base(data_dict)
    query, rank_column = _apply_text_search(query, text_query)
    if rank_column is not None:
        query = query.add_columns(rank_column)
    query = _apply_sort(query, data_dict, rank_column)

    raw_rows = query.all()
    package_ids = [row[0] for row in raw_rows]
    visible_results = _iter_visible_results(context, package_ids, fl=fl)
    filter_clauses = _extract_filter_clauses(data_dict)
    if filter_clauses:
        visible_results = [
            result
            for result in visible_results
            if _matches_filter_clauses(result, filter_clauses)
        ]

    paged_results = visible_results[start:start + rows]
    search_results = {
        "count": len(visible_results),
        "facets": {},
        "results": paged_results,
        "sort": data_dict.get("sort") or config.get("ckan.search.default_package_sort"),
        "search_facets": {},
    }

    for item in plugins.PluginImplementations(plugins.IPackageController):
        search_results = item.after_dataset_search(search_results, data_dict)
    search_results.setdefault("search_facets", {})
    search_results.setdefault("facets", {})

    if has_request_context() and request.path == "/api/action/package_search":
        return {
            "count": search_results["count"],
            "results": search_results["results"],
            "sort": search_results["sort"],
        }

    return search_results


package_search.side_effect_free = True


def package_autocomplete(context: dict[str, Any], data_dict: dict[str, Any]) -> list[dict[str, Any]]:
    toolkit.check_access("package_autocomplete", context, data_dict)

    q = (data_dict.get("q") or "").strip()
    if not q:
        return []

    limit = int(data_dict.get("limit") or 10)
    query = _search_query_base({"include_deleted": False, "include_drafts": False})
    query = query.filter(
        or_(
            cast(model.Package.name, String).ilike(f"%{q}%"),
            cast(model.Package.title, String).ilike(f"%{q}%"),
        )
    ).order_by(asc(model.Package.name))

    package_ids = [package_id for (package_id,) in query.limit(limit * 5).all()]
    visible_results = _iter_visible_results(context, package_ids, fl=["name", "title"])

    output = []
    q_lower = q.lower()
    for pkg in visible_results[:limit]:
        name = (pkg.get("name") or "")
        title = pkg.get("title") or name
        if q_lower in name.lower():
            match_field = "name"
            match_displayed = name
        else:
            match_field = "title"
            match_displayed = f"{title} ({name})"
        output.append(
            {
                "name": name,
                "title": title,
                "match_field": match_field,
                "match_displayed": match_displayed,
            }
        )
    return output


class PsqlsearchPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IActions)

    def update_config(self, config_: toolkit.CKANConfig) -> None:
        toolkit.add_template_directory(config_, "templates")
        toolkit.add_public_directory(config_, "public")
        toolkit.add_resource("assets", "psqlsearch")

        # CKAN wires synchronous indexing through ckan.lib.search.index_for().
        # Replace the package index backend with a no-op implementation so
        # dataset writes do not require Solr to be available.
        search._INDICES["package"] = NoopSearchIndex
        search._QUERIES["package"] = PostgresPackageSearchQuery
        search.PackageSearchQuery = PostgresPackageSearchQuery
        search_common.is_available = lambda: True
        search.is_available = lambda: True

    def get_actions(self) -> dict[str, Any]:
        return {
            "package_search": package_search,
            "package_autocomplete": package_autocomplete,
        }
