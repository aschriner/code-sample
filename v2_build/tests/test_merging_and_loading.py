import json
from pprint import pformat

from django.db import connection
from django.test.testcases import TestCase
from django.test.utils import CaptureQueriesContext
import mock

from core.choice_lists import ATTRIBUTE_TYPE_CHOICES
from core.factories import OrgFactory, PersonFactory, RoleFactory
from core.factories import SOURCE_CHOICES
from core.models import Org, Attribute, Person, Role, OrgAttribute
from core.record_parsing.attributes import ParsedAttribute
from lead_api.v2_build import source_priority
from lead_api.v2_build.linking.mergers import OrgMerger, PersonMerger, RoleMerger
from lead_api.v2_build.linking.org import insert_org
from lead_api.v2_build.linking.person import split_person_and_role_dict, insert_person, insert_role
from lead_api.v2_build.tests.test_linking import PreExistingOrgsTestCaseMixin, \
    simulate_parser_from_dict, TestSource


class OrgMergingTestCase(PreExistingOrgsTestCaseMixin, TestCase):

    @mock.patch.object(source_priority, 'PRIORITY_LIST', ['testsource'] + SOURCE_CHOICES)
    def test_merge_and_update_org(self):
        pre_existing_org = OrgFactory.create(
            name="Google",
            website="google.com",
            street_address="1600 Amphitheatre Parkway",
            city="Mountain View",
            state="California",
            sources='["oldsource1", "oldsource2"]',
            ecommerce_package=True,
            external_ids=json.dumps(['oldsource1:123', 'oldsource2:abc'])
        )
        old_industry_independent = 'Automotive'
        old_industry_corroborated = 'Internet'
        pre_existing_industries = Attribute.objects.filter(
            value__in=[old_industry_independent, old_industry_corroborated])
        new_naics_value = "Potato Farming"  # exciting new Google venture
        new_naics_industry = Attribute.objects.filter(value=new_naics_value).first()
        OrgAttribute.objects.bulk_create(
            [OrgAttribute(org=pre_existing_org, attribute=industry, sources={'oldsource': 'yes'})
             for industry in pre_existing_industries]
        )
        new_industry = "Consumer Electronics"
        raw_org_dict = {
            "name": "Google Inc",
            "street_address": "123 NEW ADDRESS DR",
            "city": "Mountain View",
            "state": "California",
            "website": "google.com",
            "estimated_revenue": 10 ** 8,
            "ecommerce_package": False,
            "industry": [
                old_industry_corroborated,
                new_industry
            ],
            "naics_code": new_naics_industry.industry_code
        }
        parsed_org_dict = json.loads(simulate_parser_from_dict(raw_org_dict))
        parsed_org_dict['external_id'] = 'testsource:123'

        context = CaptureQueriesContext(connection)
        with context:
            OrgMerger(pre_existing_org, parsed_org_dict).merge_and_update()

        updated_org = Org.objects.get(id=pre_existing_org.id)
        # 3 queries: 1) Update Org 2) get map of Attribute values to ids 3) Upsert OrgAttributes
        max_num_queries = 3
        self.assertLessEqual(
            len(context.captured_queries), max_num_queries,
            msg="{} queries > {} queries; they were\n{}".format(
                len(context.captured_queries),
                max_num_queries,
                pformat(context.captured_queries)))

        self.assertEqual(updated_org.name, "Google Inc")  # use new name
        self.assertEqual(updated_org.street_address, "123 NEW ADDRESS DR")  # use new address
        self.assertEqual(updated_org.ecommerce_package, True)  # use ANY(bools)
        self.assertEqual(
            set([attr.value for attr in updated_org.industries]),
            # old plus new
            {old_industry_independent, old_industry_corroborated, new_industry, new_naics_value}
        )
        oa_set = updated_org.orgattribute_set
        # make sure sources are updated correctly
        self.assertEqual(
            ['oldsource'],
            oa_set.get(attribute__value=old_industry_independent).sources.keys()
        )
        self.assertEqual(
            ['oldsource', TestSource.source],
            oa_set.get(attribute__value=old_industry_corroborated).sources.keys()
        )
        self.assertEqual(
            [TestSource.source],
            oa_set.get(attribute__value=new_industry).sources.keys()
        )
        self.assertEqual(
            updated_org.field_sources['state'],
            pre_existing_org.field_sources['state'])  # unchanged
        # update if new source has same
        self.assertEqual(updated_org.field_sources['website'], TestSource.source)
        # new field, was empty
        self.assertEqual(updated_org.field_sources['estimated_revenue'], TestSource.source)
        self.assertEqual(
            set(json.loads(updated_org.sources)),
            {TestSource.source, "oldsource1", "oldsource2"}
        )

    def test_insert_org(self):
        parsed_org_dict = {
            "name": "Google Inc",
            "street_address": "123 NEW ADDRESS DR",
            "city": "Mountain View",
            "state": "California",
            "website": "google.com",
            "estimated_revenue": 10 ** 8,
            "ecommerce_package": False,
            "industry": [
                "Consumer Electronics"
            ]
        }
        parsed_org_dict = json.loads(simulate_parser_from_dict(parsed_org_dict))
        parsed_org_dict['source'] = 'testsource'
        parsed_org_dict['external_id'] = 'testsource:123'

        context = CaptureQueriesContext(connection)
        with context:
            insert_org(parsed_org_dict)
        # 3 queries: 1) Insert Org 2) Get Attributes by name 3) Insert OrgAttributes
        max_num_queries = 3
        self.assertLessEqual(
            len(context.captured_queries), max_num_queries,
            msg="{} queries > {} queries; they were\n{}".format(
                len(context.captured_queries),
                max_num_queries,
                pformat(context.captured_queries)))

        # org should be created - consider this a test assertion
        org = Org.objects.get(name="Google Inc")

        self.assertEqual(org.field_sources['name'], 'testsource')
        self.assertEqual(org.sources, '["testsource"]')
        # attributes added
        self.assertEqual(
            set([attr.value for attr in org.industries]),
            {'Consumer Electronics'}
        )
        for oa in org.orgattribute_set.all():
            self.assertIn(TestSource.source, oa.sources)

    def test_attribute_retrieval_with_normalization(self):
        # just lowercase
        attr_value = Attribute.objects.first().value
        expected_attrs = Attribute.objects.filter(value=attr_value)
        parsed_attribute = ParsedAttribute(
            attribute_type=ATTRIBUTE_TYPE_CHOICES.INDUSTRY, value_or_code=attr_value.lower())
        retrieved_attrs = ParsedAttribute.get_attribute_instances([parsed_attribute])
        self.assertEqual(list(expected_attrs), list(retrieved_attrs))


class PersonRoleMergingTestCase(TestCase):
    def test_merge_and_update_person(self):
        pre_existing_person = PersonFactory.create(
            email='bobsmith@company.com',
            external_ids='["blerg"]'
        )
        person_dict = {
            "previous_position_1_start_year": "0",
            "previous_position_2_title": None,
            "netwise_person_id": "149689825",
            "last_name": "Smith",
            "person_li_url": "https://www.linkedin.com/in/bobsmith/",
            "email_status": "unknown",
            "functional_area": "marketing",
            "previous_position_1_title": None,
            "skills": "Pharmaceutical Sales|Marketing Management|Sales Effectiveness|"
                      "Market Planning|Product Launch",
            "previous_position_2_company_name": None,
            "previous_position_1_company_name": None,
            "department": "Marketing",
            "job_title": "Marketing Manager",
            "previous_position_2_start_year": "0",
            "first_name": "Bob",
            "email": "bobsmith@company.com",
            "seniority": "Manager",
            "external_id": "foo1234"
        }

        person_dict, _ = split_person_and_role_dict(person_dict)
        PersonMerger(pre_existing_person, person_dict).merge_and_update()

        updated_person = Person.objects.get(id=pre_existing_person.id)
        self.assertEqual(updated_person.first_name, "Bob")
        self.assertEqual(set(json.loads(updated_person.external_ids)), {"blerg", "foo1234"})

    def test_insert_person(self):
        person_dict = {
            "previous_position_1_start_year": "0",
            "previous_position_2_title": None,
            "netwise_person_id": "149689825",
            "last_name": "Smith",
            "person_li_url": "https://www.linkedin.com/in/bobsmith/",
            "email_status": "unknown",
            "functional_area": "marketing",
            "previous_position_1_title": None,
            "skills": "Pharmaceutical Sales|Marketing Management|Sales Effectiveness|"
                      "Market Planning|Product Launch",
            "previous_position_2_company_name": None,
            "previous_position_1_company_name": None,
            "department": "Marketing",
            "job_title": "Marketing Manager",
            "previous_position_2_start_year": "0",
            "first_name": "Bob",
            "email": "bobsmith@company.com",
            "seniority": "Manager",
            "external_id": "foo1234"
        }
        person_dict, _ = split_person_and_role_dict(person_dict)
        person = insert_person(person_dict)
        inserted_person = Person.objects.get(id=person.id)
        self.assertEqual(inserted_person.first_name, "Bob")
        self.assertEqual(inserted_person.email, "bobsmith@company.com")
        self.assertEqual(inserted_person.external_ids, '["foo1234"]')

    def test_insert_role(self):
        pre_existing_org = OrgFactory.create()
        pre_existing_person = PersonFactory.create()
        person_dict = {
            "previous_position_1_start_year": "0",
            "previous_position_2_title": None,
            "netwise_person_id": "149689825",
            "last_name": "Smith",
            "person_li_url": "https://www.linkedin.com/in/bobsmith/",
            "email_status": "unknown",
            "functional_area": "marketing",
            "previous_position_1_title": None,
            "skills": "Pharmaceutical Sales|Marketing Management|Sales Effectiveness|"
                      "Market Planning|Product Launch",
            "previous_position_2_company_name": None,
            "previous_position_1_company_name": None,
            "department": "Marketing",
            "job_title": "Marketing Manager",
            "previous_position_2_start_year": "0",
            "first_name": "Bob",
            "email": "bobsmith@company.com",
            "seniority": "Manager",
            "external_id": "foo1234"
        }
        _, role_dict = split_person_and_role_dict(person_dict)
        role = insert_role(role_dict, pre_existing_org.id, pre_existing_person)
        inserted_role = Role.objects.get(id=role.id)
        self.assertEqual(inserted_role.email, "bobsmith@company.com")
        self.assertEqual(inserted_role.job_title, "Marketing Manager")
        self.assertEqual(inserted_role.seniority, "Manager")

    def test_merge_and_update_role(self):
        org = OrgFactory.create()
        person = PersonFactory.create()
        pre_existing_role = RoleFactory.create(person=person, org=org)
        person_dict = {
            "previous_position_1_start_year": "0",
            "previous_position_2_title": None,
            "netwise_person_id": "149689825",
            "last_name": "Smith",
            "person_li_url": "https://www.linkedin.com/in/bobsmith/",
            "email_status": "unknown",
            "functional_area": "marketing",
            "previous_position_1_title": None,
            "skills": "Pharmaceutical Sales|Marketing Management|Sales Effectiveness|"
                      "Market Planning|Product Launch",
            "previous_position_2_company_name": None,
            "previous_position_1_company_name": None,
            "department": "Marketing",
            "job_title": "Marketing Manager",
            "previous_position_2_start_year": "0",
            "first_name": "Bob",
            "email": "bobsmith@company.com",
            "seniority": "Manager",
            "external_id": "foo1234",
            "phone": "555-867-5309"
        }
        _, role_dict = split_person_and_role_dict(person_dict)
        RoleMerger(pre_existing_role, role_dict).merge_and_update()

        updated_role = Role.objects.get(id=pre_existing_role.id)
        self.assertEqual(updated_role.job_title, "Marketing Manager")
        self.assertEqual(updated_role.seniority, "Manager")
        self.assertEqual(updated_role.department, "Marketing")
        self.assertEqual(updated_role.email, "bobsmith@company.com")
        self.assertEqual(updated_role.work_phone, "555-867-5309")
