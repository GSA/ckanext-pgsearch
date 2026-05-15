[![Tests](https://github.com/FuhuXia/ckanext-psqlsearch/workflows/Tests/badge.svg?branch=main)](https://github.com/FuhuXia/ckanext-psqlsearch/actions)

# ckanext-psqlsearch

`ckanext-psqlsearch` is a CKAN search backend replacement that serves
`package_search`, autocomplete, and organization/group listing from PostgreSQL
instead of Solr.

It is aimed at CKAN 2.11 deployments that want to keep standard dataset and
organization pages usable without relying on a Solr service for the implemented
search features.

## Current behavior

- Replaces dataset search with PostgreSQL queries.
- Supports text search over `name`, `title`, `notes`, `author`, and `maintainer`.
- Supports filtering for common fields such as dataset type, owner
  organization, tags, groups, license, id, and name.
- Supports CKAN dataset sorting inputs including `sort`, `order_by`, and
  `title_string`.
- Provides package autocomplete from PostgreSQL.
- Provides SQL-backed organization and group listing pagination and dataset
  counts.

## Requirements

Compatibility with core CKAN versions:

| CKAN version | Compatible? |
| ------------ | ----------- |
| 2.11         | yes         |
| earlier      | not tested  |

This extension depends on PostgreSQL full-text search. No Solr service is
required for the search functionality implemented here.

## Installation

1. Activate your CKAN virtual environment.
2. Clone and install the extension:

```bash
git clone https://github.com/FuhuXia/ckanext-psqlsearch.git
cd ckanext-psqlsearch
pip install -e .
pip install -r requirements.txt
```

3. Add `psqlsearch` to `ckan.plugins` in your CKAN config.
4. Restart CKAN.

## Known limitations

- No facet support by design.
- No weighted field ranking yet.
- Extras and resources are not included in full-text search.
- Some CKAN bootstrap paths may still require deployment-specific handling if
  Solr is removed entirely from the environment.

## Development

```bash
git clone https://github.com/FuhuXia/ckanext-psqlsearch.git
cd ckanext-psqlsearch
pip install -e .
pip install -r dev-requirements.txt
```

## Tests

```bash
pytest --ckan-ini=test.ini
```

## License

[AGPL](https://www.gnu.org/licenses/agpl-3.0.en.html)
