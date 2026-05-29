import pytest

import ckan.tests.factories as factories
import ckan.tests.helpers as helpers


@pytest.mark.ckan_config("ckan.plugins", "pgsearch")
@pytest.mark.usefixtures("clean_db", "with_plugins")
class TestPackageSearchIntegration:
    def test_package_search_supports_q_all_sort_and_pagination(self):
        first = factories.Dataset(name="alpha", title="Alpha dataset")
        second = factories.Dataset(name="bravo", title="Bravo dataset")
        third = factories.Dataset(name="charlie", title="Charlie dataset")

        result = helpers.call_action(
            "package_search",
            q="*:*",
            order_by="title_string asc",
            start=1,
            rows=2,
        )

        assert result["count"] == 3
        assert [pkg["name"] for pkg in result["results"]] == [
            second["name"],
            third["name"],
        ]

    def test_package_search_filters_by_owner_org_and_tag(self):
        owner = factories.Organization(name="water-dept")
        other_owner = factories.Organization(name="parks-dept")

        matching = factories.Dataset(
            name="water-quality",
            owner_org=owner["id"],
            tags=[{"name": "water"}],
            license_id="cc-by",
        )
        factories.Dataset(
            name="parks-inventory",
            owner_org=other_owner["id"],
            tags=[{"name": "water"}],
            license_id="cc-by",
        )
        factories.Dataset(
            name="water-internal",
            owner_org=owner["id"],
            tags=[{"name": "internal"}],
            license_id="cc-by",
        )

        result = helpers.call_action(
            "package_search",
            q="*:*",
            fq=f"owner_org:{owner['id']} tags:water",
        )

        assert result["count"] == 1
        assert [pkg["name"] for pkg in result["results"]] == [matching["name"]]

    def test_package_search_excludes_private_datasets_by_default(self):
        owner = factories.Organization(name="private-owner")
        public = factories.Dataset(name="public-dataset")
        factories.Dataset(
            name="private-dataset",
            private=True,
            owner_org=owner["id"],
        )

        default_result = helpers.call_action("package_search", q="*:*")
        include_private_result = helpers.call_action(
            "package_search",
            q="*:*",
            include_private=True,
        )

        assert [pkg["name"] for pkg in default_result["results"]] == [public["name"]]
        assert {pkg["name"] for pkg in include_private_result["results"]} == {
            "public-dataset",
            "private-dataset",
        }

    def test_package_autocomplete_returns_name_and_title_matches(self):
        name_match = factories.Dataset(name="harbor-water", title="Harbor Conditions")
        title_match = factories.Dataset(name="coastal-report", title="Water Quality Report")

        result = helpers.call_action("package_autocomplete", q="water")

        assert {item["name"] for item in result} == {
            name_match["name"],
            title_match["name"],
        }
        result_by_name = {item["name"]: item for item in result}
        assert result_by_name[name_match["name"]]["match_field"] == "name"
        assert result_by_name[title_match["name"]]["match_field"] == "title"


@pytest.mark.ckan_config("ckan.plugins", "pgsearch")
@pytest.mark.usefixtures("clean_db", "with_plugins")
class TestOrganizationListIntegration:
    def test_organization_list_sorts_by_package_count(self):
        larger = factories.Organization(name="larger-org", title="Larger Org")
        smaller = factories.Organization(name="smaller-org", title="Smaller Org")

        factories.Dataset(name="larger-one", owner_org=larger["id"])
        factories.Dataset(name="larger-two", owner_org=larger["id"])
        factories.Dataset(name="smaller-one", owner_org=smaller["id"])

        result = helpers.call_action(
            "organization_list",
            all_fields=False,
            sort="package_count desc",
        )

        assert result[:2] == [larger["name"], smaller["name"]]

    def test_organization_list_supports_query_limit_and_offset(self):
        first = factories.Organization(name="alpha-org", title="Alpha Org")
        second = factories.Organization(name="beta-org", title="Beta Org")
        factories.Organization(name="gamma-org", title="Gamma Org")

        result = helpers.call_action(
            "organization_list",
            all_fields=False,
            q="org",
            sort="name asc",
            limit=1,
            offset=1,
        )

        assert result == [second["name"]]
