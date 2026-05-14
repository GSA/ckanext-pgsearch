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


def _resolve_group_ids(values: list[str]) -> list[str]:
    if not values:
        return []
    rows = (
        model.Session.query(model.Group.id)
        .filter(
            or_(
                model.Group.id.in_(values),
                model.Group.name.in_(values),
                model.Group.title.in_(values),
            )
        )
        .all()
    )
    return [group_id for (group_id,) in rows]


def _resolve_tag_ids(values: list[str]) -> list[str]:
    if not values:
        return []
    rows = (
        model.Session.query(model.Tag.id)
        .filter(or_(model.Tag.id.in_(values), model.Tag.name.in_(values)))
        .all()
    )
    return [tag_id for (tag_id,) in rows]


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
    sort = (
        data_dict.get("order_by")
        or data_dict.get("sort")
        or config.get("ckan.search.default_package_sort")
    )
    sort = (sort or "metadata_modified desc").strip()

    if sort in {"rank", "score desc, metadata_modified desc", "score desc"}:
        if rank_column is not None:
            return query.order_by(desc(rank_column), desc(model.Package.metadata_modified))
        return query.order_by(desc(model.Package.metadata_modified))

    field_map = {
        "metadata_modified": model.Package.metadata_modified,
        "metadata_created": model.Package.metadata_created,
        "title": model.Package.title,
        "title_string": func.lower(func.coalesce(model.Package.title, model.Package.name)),
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


def _apply_supported_filter_clauses(
    query: Query[Any], clauses: list[tuple[str, list[str]]]
) -> tuple[Query[Any], list[tuple[str, list[str]]]]:
    unsupported: list[tuple[str, list[str]]] = []

    for field, values in clauses:
        if not values:
            continue

        if field in {"dataset_type", "type"}:
            query = query.filter(model.Package.type.in_(values))
            continue

        if field == "owner_org":
            group_ids = _resolve_group_ids(values)
            if group_ids:
                query = query.filter(model.Package.owner_org.in_(group_ids))
            else:
                query = query.filter(model.Package.owner_org.in_(values))
            continue

        if field == "organization":
            group_ids = _resolve_group_ids(values)
            query = query.filter(model.Package.owner_org.in_(group_ids or [""]))
            continue

        if field == "groups":
            group_ids = _resolve_group_ids(values)
            subquery = (
                model.Session.query(model.Member.table_id)
                .filter(model.Member.table_name == "package")
                .filter(model.Member.state == "active")
                .filter(model.Member.group_id.in_(group_ids or [""]))
            )
            query = query.filter(model.Package.id.in_(subquery))
            continue

        if field == "tags":
            tag_ids = _resolve_tag_ids(values)
            subquery = (
                model.Session.query(model.PackageTag.package_id)
                .filter(model.PackageTag.state == "active")
                .filter(model.PackageTag.tag_id.in_(tag_ids or [""]))
            )
            query = query.filter(model.Package.id.in_(subquery))
            continue

        if field == "license_id":
            query = query.filter(model.Package.license_id.in_(values))
            continue

        if field == "id":
            query = query.filter(model.Package.id.in_(values))
            continue

        if field == "name":
            query = query.filter(model.Package.name.in_(values))
            continue

        unsupported.append((field, values))

    return query, unsupported


def _query_count(query: Query[Any]) -> int:
    return query.with_entities(model.Package.id).order_by(None).count()


def _query_package_ids(query: Query[Any], start: int = 0, rows: Optional[int] = None) -> list[str]:
    if start:
        query = query.offset(start)
    if rows is not None:
        query = query.limit(rows)
    return [row[0] for row in query.all()]


def _group_dataset_counts(group_ids: list[str], *, is_org: bool) -> dict[str, int]:
    if not group_ids:
        return {}

    count_query = (
        model.Session.query(model.Member.group_id, func.count(model.Package.id))
        .join(
            model.Package,
            model.Member.table_id == model.Package.id,
        )
        .filter(model.Member.table_name == "package")
        .filter(model.Member.state == "active")
        .filter(model.Package.state == "active")
        .filter(model.Package.type == "dataset")
        .filter(model.Package.private.is_(False))
        .filter(model.Member.group_id.in_(group_ids))
        .group_by(model.Member.group_id)
    )

    counts = {group_id: count for group_id, count in count_query.all()}
    return {
        group_id: counts.get(group_id, 0)
        for group_id in group_ids
    }


def _parse_group_sort(sort: str) -> tuple[str, str]:
    sort = (sort or config.get("ckan.default_group_sort") or "title asc").strip()
    if sort in {"packages", "package_count"}:
        sort = "package_count desc"

    parts = sort.replace(",", " ").split()
    field = parts[0] if parts else "title"
    direction = parts[1].lower() if len(parts) > 1 else "asc"

    if field not in {"name", "packages", "package_count", "title"}:
        raise toolkit.ValidationError({"message": f"Cannot sort by field `{field}`"})
    if direction not in {"asc", "desc"}:
        raise toolkit.ValidationError({"message": f"Invalid sort direction `{direction}`"})

    if field == "packages":
        field = "package_count"
    return field, direction


def _group_or_org_list(
    context: dict[str, Any], data_dict: dict[str, Any], *, is_org: bool = False
) -> list[Any]:
    toolkit.check_access("organization_list" if is_org else "group_list", context, data_dict)

    group_type = data_dict.get("type") or ("organization" if is_org else "group")
    api_version = context.get("api_version")
    ref_group_by = "id" if api_version == 2 else "name"
    groups_filter = data_dict.get("groups") or []
    q = (data_dict.get("q") or "").strip()
    all_fields = _asbool(data_dict.get("all_fields"))

    try:
        max_limit = int(
            config.get(
                "ckan.group_and_organization_list_all_fields_max"
                if all_fields
                else "ckan.group_and_organization_list_max"
            )
        )
    except (TypeError, ValueError):
        max_limit = 25 if all_fields else 1000

    raw_limit = data_dict.get("limit")
    limit = max_limit if raw_limit is None else min(int(raw_limit), max_limit)
    offset = int(data_dict.get("offset") or 0)

    sort_field, sort_direction = _parse_group_sort(
        data_dict.get("sort") or data_dict.get("order_by") or ""
    )

    dataset_counts_sq = (
        model.Session.query(
            model.Member.group_id.label("group_id"),
            func.count(model.Package.id).label("package_count"),
        )
        .join(model.Package, model.Member.table_id == model.Package.id)
        .filter(model.Member.table_name == "package")
        .filter(model.Member.state == "active")
        .filter(model.Package.state == "active")
        .filter(model.Package.type == "dataset")
        .filter(model.Package.private.is_(False))
        .group_by(model.Member.group_id)
        .subquery()
    )

    package_count = func.coalesce(dataset_counts_sq.c.package_count, 0)
    query = (
        model.Session.query(model.Group)
        .outerjoin(dataset_counts_sq, dataset_counts_sq.c.group_id == model.Group.id)
        .filter(model.Group.state == "active")
        .filter(model.Group.is_organization == is_org)
        .filter(model.Group.type == group_type)
    )

    if groups_filter:
        group_names = groups_filter
        if isinstance(groups_filter, str):
            group_names = [name.strip() for name in groups_filter.split(",") if name.strip()]
        query = query.filter(model.Group.name.in_(group_names))

    if q:
        like_q = f"%{q}%"
        query = query.filter(
            or_(
                model.Group.name.ilike(like_q),
                model.Group.title.ilike(like_q),
                model.Group.description.ilike(like_q),
            )
        )

    sort_column = {
        "name": model.Group.name,
        "title": model.Group.title,
        "package_count": package_count,
    }[sort_field]
    if sort_direction == "asc":
        query = query.order_by(asc(sort_column), asc(model.Group.name))
    else:
        query = query.order_by(desc(sort_column), asc(model.Group.name))

    query = query.offset(offset).limit(limit)
    groups = query.all()

    if not all_fields:
        return [getattr(group, ref_group_by) for group in groups]

    include_dataset_count = _asbool(data_dict.get("include_dataset_count"), default=True)
    include_datasets = _asbool(data_dict.get("include_datasets"))
    show_context = dict(context)
    if include_dataset_count and not include_datasets:
        count_map = _group_dataset_counts([group.id for group in groups], is_org=is_org)
        show_context["dataset_counts"] = {
            "owner_org" if is_org else "groups": count_map,
        }

    action = toolkit.get_action("organization_show" if is_org else "group_show")
    group_list = []
    for group in groups:
        show_data = dict(data_dict)
        show_data["id"] = group.id
        for key in (
            "include_extras",
            "include_tags",
            "include_users",
            "include_groups",
            "include_followers",
        ):
            show_data.setdefault(key, False)
        group_list.append(action(show_context, show_data))
    return group_list


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
        filter_clauses = _extract_filter_clauses(query)
        db_query, unsupported_clauses = _apply_supported_filter_clauses(db_query, filter_clauses)
        db_query, rank_column = _apply_text_search(db_query, text_query)
        if rank_column is not None:
            db_query = db_query.add_columns(rank_column)
        db_query = _apply_sort(db_query, query, rank_column)

        start = int(query.get("start", 0) or 0)
        rows = int(query.get("rows", 10))
        self.count = _query_count(db_query)

        package_ids = _query_package_ids(db_query, start=start, rows=rows)
        package_dicts = _postgres_package_dicts(package_ids)

        if unsupported_clauses:
            package_dicts = [
                pkg_dict
                for pkg_dict in package_dicts
                if _matches_filter_clauses(pkg_dict, unsupported_clauses)
            ]

        facet_fields = _normalize_facet_fields(query.get("facet.field"))
        self.facets = {} if not facet_fields else _facet_counts(package_dicts, facet_fields)

        result_fields = _split_result_field_list(query.get("fl"))
        results: list[Any]
        if result_fields:
            results = []
            for pkg_dict in package_dicts:
                result = {field: pkg_dict.get(field) for field in result_fields}
                if len(result_fields) == 1 and result_fields[0] in {"id", "name"}:
                    results.append(result.get(result_fields[0]))
                else:
                    results.append(result)
        else:
            results = package_dicts

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
    filter_clauses = _extract_filter_clauses(data_dict)
    query, unsupported_clauses = _apply_supported_filter_clauses(query, filter_clauses)
    query, rank_column = _apply_text_search(query, text_query)
    if rank_column is not None:
        query = query.add_columns(rank_column)
    query = _apply_sort(query, data_dict, rank_column)

    total_count = _query_count(query)
    package_ids = _query_package_ids(query, start=start, rows=rows)
    visible_results = _iter_visible_results(context, package_ids, fl=fl)
    if unsupported_clauses:
        visible_results = [
            result
            for result in visible_results
            if _matches_filter_clauses(result, unsupported_clauses)
        ]

    search_results = {
        "count": total_count,
        "facets": {},
        "results": visible_results,
        "sort": (
            data_dict.get("order_by")
            or data_dict.get("sort")
            or config.get("ckan.search.default_package_sort")
        ),
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


def organization_list(context: dict[str, Any], data_dict: dict[str, Any]) -> list[Any]:
    data_dict = dict(data_dict)
    data_dict["groups"] = data_dict.pop("organizations", [])
    data_dict.setdefault("type", "organization")
    return _group_or_org_list(context, data_dict, is_org=True)


def group_list(context: dict[str, Any], data_dict: dict[str, Any]) -> list[Any]:
    return _group_or_org_list(context, dict(data_dict), is_org=False)


organization_list.side_effect_free = True
group_list.side_effect_free = True


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
            "organization_list": organization_list,
            "group_list": group_list,
        }
