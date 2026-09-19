import json
from pprint import pformat

import mock
from django.db import connection
from django.test.testcases import TestCase
from django.test.utils import CaptureQueriesContext

from core.factories import OrgFactory, PersonFactory, RoleFactory, SOURCE_CHOICES, \
    create_industry_attributes
from core.models import Org, Attribute, Role, Person, OrgAttribute
from core.record_parsing.abstract_source import AbstractSource
from lead_api.v2_build import source_priority
from lead_api.v2_build.tasks import link_org_and_persons


class TestSource(AbstractSource):
    source = 'testsource'


def simulate_parser_from_dict(org_dict):
    parsed_org = TestSource(org_dict).get_org_dict()
    parsed_org_json_line = json.dumps(parsed_org)
    return parsed_org_json_line


class PreExistingOrgsTestCaseMixin(object):
    @classmethod
    def setUpTestData(cls):
        with connection.cursor() as cursor:
            cursor.execute('create extension pg_trgm;')
        cls.create_noise_orgs()
        create_industry_attributes()
        super(PreExistingOrgsTestCaseMixin, cls).setUpTestData()

    @classmethod
    def create_noise_orgs(cls):
        """Create some random Orgs so there's non-matching stuff in the database"""
        num_orgs_with_website = 9
        cls.noise_orgs_with_website = OrgFactory.create_batch(size=num_orgs_with_website)
        num_orgs_without_website = 7
        cls.noise_orgs_without_website = OrgFactory.create_batch(
            size=num_orgs_without_website, website='')


class LinkOrgsTestCase(PreExistingOrgsTestCaseMixin, TestCase):
    """
    High level functional tests to exercise all pieces together
    """

    def test_link_org_no_match(self):
        # GIVEN: some pre-existing Orgs with and without website,
        # and a parsed record without website
        OrgFactory.create(
            name="We R Real Company Inc",
            website="realcompany.com",
        )
        OrgFactory.create(
            name="BEST SEO CONSULTANTS LLC",
            website="",
        )

        org_dict = {
            "name": "Google Inc",
            "street_address": "123 New Address Drive",
            "website": "",  # major oversight guys
            "estimated_revenue": 10 ** 8,
            "industry": [
                "Consumer Electronics",
                "Internet"
            ],
            "person_data": [
                {
                    "first_name": "Larry",
                    "last_name": "Page",
                    "email": "larry@google.com",
                    "job_title": "Founder"
                },
                {
                    "first_name": "Sergey",
                    "last_name": "Brin",
                    "email": "sergey@google.com",
                    "job_title": "Founder"
                },
                {
                    "first_name": "Sundar",
                    "last_name": "Pichai",
                    "email": "sundar@google.com",
                    "job_title": "CEO"
                },
            ]
        }
        orgs_count_before_db = Org.objects.count()
        persons_count_before_db = Person.objects.count()
        roles_count_before_db = Role.objects.count()

        # WHEN: I try to link the parsed record to an existing record

        # simulated parsing process
        parsed_org_json_line = simulate_parser_from_dict(org_dict)
        context = CaptureQueriesContext(connection)
        with context:
            link_org_and_persons(1, 1, parsed_org_json_line)

        # THEN: I get no match and we insert a new Org, new Persons and Roles instead

        # TODO - can we reduce number of queries necessary to link?
        max_num_queries = 17
        self.assertLessEqual(
            len(context.captured_queries),
            max_num_queries,
            msg="{} queries > {} queries; they were\n{}".format(
                len(context.captured_queries),
                max_num_queries,
                pformat(context.captured_queries)))

        orgs_count_after_db = Org.objects.count()
        persons_count_after_db = Person.objects.count()
        roles_count_after_db = Role.objects.count()

        self.assertEqual(orgs_count_after_db, orgs_count_before_db + 1)
        self.assertEqual(persons_count_after_db, persons_count_before_db + 3)
        self.assertEqual(roles_count_after_db, roles_count_before_db + 3)

        org = Org.objects.get(name='Google Inc')
        self.assertEqual(
            set([attr.value for attr in org.industries]),
            {'Consumer Electronics', 'Internet'}
        )

        for name in ["Larry", "Sergey", "Sundar"]:
            self.assertEqual(Role.objects.filter(person__first_name=name).count(), 1)

    @mock.patch.object(source_priority, 'PRIORITY_LIST', ['testsource'] + SOURCE_CHOICES)
    def test_link_org_with_match(self):
        # GIVEN: a matched_org, and a parsed_org with new Persons and Attributes
        pre_existing_org = OrgFactory.create(
            name="Google",
            website="google.com",
            street_address="1600 Amphitheatre Parkway",
            city="Mountain View",
            state="California",
            external_ids='["foo", "bar"]'
        )
        pre_existing_industries = Attribute.objects.filter(
            value__in=['Internet', 'Computer Software'])
        OrgAttribute.objects.bulk_create(
            [OrgAttribute(org=pre_existing_org, attribute=industry)
             for industry in pre_existing_industries]
        )

        person1 = PersonFactory.create(
            first_name="Larry",
            last_name="Page",
            external_ids='["foo123"]'
        )
        RoleFactory.create(person=person1, org=pre_existing_org, job_title="Founder",
                           email="larry@google.com")
        RoleFactory.create(person=person1, org=pre_existing_org, job_title="CEO",
                           email="larry@google.com", )
        person2 = PersonFactory.create(
            first_name="Sergey",
            last_name="Brin",
            external_ids='["bar456"]'
        )
        RoleFactory.create(person=person2, org=pre_existing_org, job_title="Founder",
                           email="sergey@google.com")

        org_dict = {
            "name": "Google Inc",
            "street_address": "1600 Amphitheatre Parkway Suite 1",
            "website": "google.com",
            "state": "California",
            "estimated_revenue": 10 ** 8,
            "industry": [
                "Consumer Electronics",
                "Internet"
            ],
            "person_data": [
                # Case 1 - same name, email & title
                {
                    "first_name": "Larry",
                    "last_name": "Page",
                    "email": "larry@google.com",
                    "job_title": "Founder"
                },
                # Case 2 - same name, email, new title
                {
                    "first_name": "Sergey",
                    "last_name": "Brin",
                    "email": "sergey@google.com",
                    "job_title": "Mailman"  # sorry Sergey
                },
                # Case 3 - new person
                {
                    "first_name": "Sundar",
                    "last_name": "Pichai",
                    "email": "sundar@google.com",
                    "job_title": "CEO"
                },
            ]
        }
        # simulated parsing process
        parsed_org_json_line = simulate_parser_from_dict(org_dict)

        # WHEN: I merge the parsed record into the matched Org
        context = CaptureQueriesContext(connection)
        with context:
            link_org_and_persons(1, 1, parsed_org_json_line)

        # THEN: 1) the org data is updated

        max_num_queries = 17
        # It takes 5 queries per Org and 4 per person/role
        #
        # Org:
        # 1. Select Org matches
        # 2. Insert/Update Org
        # 3. Select Org's existing Attributes
        # 4. Select Attributes by name (we need their IDs to create OrgAttributes)
        # 5. Insert OrgAttributes
        #
        # Person/Role:
        # 1. Select Role matches (if we get a match we can skip #2)
        # 2. Select Person matches
        # 3. Insert/Update Person
        # 4. Insert/Update Role
        #
        # For our test Org, 5 queries for the Org and 3x4 = 12 for persons = 17 total

        self.assertLessEqual(
            len(context.captured_queries),
            max_num_queries,
            msg="{} queries > {} queries; they were\n{}".format(
                len(context.captured_queries),
                max_num_queries,
                pformat(context.captured_queries)))
        updated_org_db = Org.objects.get(id=pre_existing_org.id)
        self.assertEqual(updated_org_db.name, org_dict["name"])
        self.assertEqual(updated_org_db.estimated_revenue, 10 ** 8)
        self.assertEqual(updated_org_db.field_sources['estimated_revenue'],
                         'testsource')
        expected_sources = SOURCE_CHOICES + ['testsource']
        self.assertEqual(set(json.loads(updated_org_db.sources)), set(expected_sources))
        # AND THEN: 2) the Attribute data is updated
        self.assertEqual(
            set([attr.value for attr in updated_org_db.industries]),
            {'Consumer Electronics', 'Internet', 'Computer Software'}
        )
        # AND THEN: 3) the Person data is updated
        persons_db = updated_org_db.persons.distinct('id')
        # Length should be 3 because 2 were updated and 1 new created
        self.assertEqual(len(persons_db), 3)
        self.assertEqual(
            set([" ".join([person.first_name, person.last_name]) for person in persons_db]),
            {"Larry Page", "Sergey Brin", "Sundar Pichai"}
        )
        # should return the 2 preexisting roles - we should not create a third duplicate Role
        self.assertEqual(Role.objects.filter(person__first_name="Larry").count(), 2)
        # should return 2 objects because we started with 1 and we create a new role
        # for the same person
        self.assertEqual(Role.objects.filter(person__first_name="Sergey").count(), 2)
        # we should add a new Person and Role for Sundar
        self.assertEqual(
            Role.objects.filter(person__first_name="Sundar", job_title="CEO").count(),
            1)

    @mock.patch.object(source_priority, 'PRIORITY_LIST', ['netwise'] + SOURCE_CHOICES)
    def test_with_netwise_parsed_data(self):
        json_line = """
        {
          "assigned_id": "netwise:company429348:person250257924",
          "attributes": [
            [
              "netwise_industry",
              "Electronics Stores"
            ],
            [
              "sic_code",
              "57319902"
            ]
          ],
          "city": "Redwood City",
          "ecommerce_package": false,
          "employee_size_range": "50 - 200",
          "external_id": "netwise:company429348:person250257924",
          "industry": [
            "Electronics Stores"
          ],
          "linkedin_url": "wwww.linkedin.com\/\/company\/jill-milan",
          "naics_code": [

          ],
          "name": "Jill Milan LLC",
          "person_data": [
            {
              "previous_position_1_start_year": "0",
              "previous_position_2_title": null,
              "netwise_person_id": "250257924",
              "last_name": "Fraser",
              "linkedin_url": "https://www.linkedin.com/in/jill-fraser-a1975759/",
              "email_status": "good",
              "functional_area": null,
              "previous_position_1_title": null,
              "skills": "Fashion|Social Media|Public Relations|Social Media Marketing|Blogging",
              "previous_position_2_company_name": null,
              "previous_position_1_company_name": null,
              "department": null,
              "job_title": "CEO",
              "previous_position_2_start_year": "0",
              "first_name": "Jill",
              "email": "jill.fraser@jillmilan.com",
              "seniority": null
            }
          ],
          "phone": "+16506547381",
          "postal_code": "94065",
          "revenue_range": "1000000 - 10000000",
          "sic_code": [
            "57319902"
          ],
          "source": "netwise",
          "street_address": "274 REDWOOD SHORES PKWY 542",
          "state": "California",
          "website": "jillmilan.com"
        }
        """
        pre_existing_org = OrgFactory.create(
            name="Jill Milan LLC",
            website="jillmilan.com",
            state="California"
        )

        before_org_count_db = Org.objects.count()

        link_org_and_persons(1, 1, json_line)

        after_org_count_db = Org.objects.count()
        self.assertEqual(before_org_count_db, after_org_count_db)
        updated_org_db = Org.objects.get(id=pre_existing_org.id)
        self.assertIn('netwise', updated_org_db.sources)

        person = Person.objects.get(email='jill.fraser@jillmilan.com')
        self.assertEqual(
            person.skills,
            "Fashion|Social Media|Public Relations|Social Media Marketing|Blogging")
        self.assertEqual(person.linkedin_url, "https://www.linkedin.com/in/jill-fraser-a1975759/")
