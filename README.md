[![Tests](https://github.com/GSA/ckanext-pgsearch/workflows/Tests/badge.svg?branch=main)](https://github.com/GSA/ckanext-pgsearch/actions)

# ckanext-pgsearch

`ckanext-pgsearch` is a CKAN search backend replacement that serves
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

Removing Solr entirely may also need a CKAN core patch so CKAN does not fail
during Solr schema checks while bootstrapping. For the GSA CKAN fork, see
[GSA/ckan@1af1607](https://github.com/GSA/ckan/commit/1af1607e4f74fe4855b4ae4814c60fff1b2d199c),
which makes `ckan.lib.search.check_solr_schema_version` return `False` when the
Solr schema cannot be retrieved.

CKAN still expects `ckan.solr_url` / `CKAN_SOLR_URL` to be configured in some
startup paths, even when this extension handles search without Solr. In a
Solr-free deployment, set it to a non-empty placeholder value such as:

```bash
export CKAN_SOLR_URL=http://placeholder-value.local
```

For an example deployment change, see
[GSA/inventory-app@60f0a23](https://github.com/GSA/inventory-app/commit/60f0a2395766668a04b1a8b90218606fb03d4209).

## Installation

1. Activate your CKAN virtual environment.
2. Clone and install the extension:

```bash
git clone https://github.com/FuhuXia/ckanext-pgsearch.git
cd ckanext-pgsearch
pip install -e .
pip install -r requirements.txt
```

3. Add `pgsearch` to `ckan.plugins` in your CKAN config.
4. Restart CKAN.

## Known limitations

- No facet support by design.
- No weighted field ranking yet.
- Extras and resources are not included in full-text search.
- Some CKAN bootstrap paths may still require deployment-specific handling if
  Solr is removed entirely from the environment.

## Development

```bash
git clone https://github.com/FuhuXia/ckanext-pgsearch.git
cd ckanext-pgsearch
pip install -e .
pip install -r dev-requirements.txt
```

## Tests

To run the same test flow locally that GitHub Actions uses:

```bash
./scripts/run-tests-docker.sh
```

This uses [docker-compose.test.yml](/Users/fxia/git/ckanext-pgsearch/docker-compose.test.yml)
to start the same CKAN, PostgreSQL, Redis, and Solr image set used in CI, then
installs the extension and runs pytest inside `ckan/ckan-dev:2.11`.

If you already have a compatible CKAN test environment, you can still run:

```bash
pytest --ckan-ini=test.ini
```

## License

[AGPL](https://www.gnu.org/licenses/agpl-3.0.en.html)
