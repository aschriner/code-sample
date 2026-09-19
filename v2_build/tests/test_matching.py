from __future__ import unicode_literals

import json
import os

from django.test.testcases import TestCase
import mock
import unicodecsv

from core.factories import OrgFactory, PersonFactory, RoleFactory, SOURCE_CHOICES
from core.models import Org
from lead_api.v2_build.linking.org import get_matched_org, calculate_similarity_score
from lead_api.v2_build.linking.person import get_matched_person_and_role
from lead_api.v2_build.tests.test_linking import PreExistingOrgsTestCaseMixin
from lead_api.v2_build import source_priority


@mock.patch.object(source_priority, 'PRIORITY_LIST', SOURCE_CHOICES)
class OrgMatchingTestCase(PreExistingOrgsTestCaseMixin, TestCase):
    def test_bulk_org_matching(self):
        filename = 'gold_matched_orgs.csv'
        with open(os.path.join(os.path.dirname(__file__), filename)) as fin:
            reader = unicodecsv.DictReader(fin, quotechar=b'"')
            for line in reader:
                is_good_match = line.get('good_match_v1') == 'TRUE'

                org_in = line['Input'].split(',')
                org_out = line['Output'].split(',')

                candidate_org = Org(
                    name=org_in[0],
                    street_address=org_in[1],
                    state=org_in[2],
                    city=org_in[3])

                try:
                    target_org = {
                        'name': org_out[0],
                        'street_address': org_out[1],
                        'state': org_out[2],
                        'city': org_out[3],
                    }
                except IndexError:
                    target_org = {'name': ''}

                similarity_score = calculate_similarity_score(candidate_org, target_org)

                self.assertTrue(
                    (similarity_score > 1.0) is is_good_match,
                    msg=[org_in, org_out, similarity_score, is_good_match]
                )

    def test_match_org_with_website(self):
        # GIVEN: an existing org with some website, and a parsed record with the same website
        # (plus some other existing Orgs for noise)
        pre_existing_org = OrgFactory.create(
            name="Google",
            website="google.com",
            state="California"
        )
        parsed_org = {
            "name": "Google, Inc",
            "website": "google.com",
            "state": "California",
            "source": SOURCE_CHOICES[0]
        }
        # WHEN: I try to match the parsed record to an existing record
        matched_org = get_matched_org(parsed_org)
        # THEN: I get the Org with the matching website
        self.assertEqual(str(matched_org.id), str(pre_existing_org.id))

    def test_match_org_without_website(self):
        # GIVEN: some pre-existing Orgs with and without website,
        # and a parsed record without website
        parsed_org = {
            "name": "Google, Inc",
            "website": "",
            "state": "California"
        }
        # WHEN: I try to match the parsed record to an existing record
        matched_org = get_matched_org(parsed_org)
        # THEN: I get no match
        self.assertIsNone(matched_org)

    def test_match_org_with_name_and_address(self):
        pre_existing_org = OrgFactory.create(
            name='google',
            street_address='1 alphabet drive',
            state="California"
        )
        parsed_org = {
            'name': 'Google',
            'street_address': '1 ALPhabet Drive 1 11',
            "state": "California"
        }
        matched_org = get_matched_org(parsed_org)
        self.assertEqual(str(matched_org.id), pre_existing_org.id)

    def test_match_org_with_multiple_matching_websites(self):
        # GIVEN: an existing org with some website, and a parsed record with the same website
        # (plus some other existing Orgs for noise)
        pre_existing_org_top_prio = OrgFactory.create(
            name="The Real Google",
            website="google.com",
            state="California",
            sources=json.dumps(SOURCE_CHOICES[:2])  # first 2 sources
        )
        OrgFactory.create(
            name="Lower Priority Google Inc",
            website="google.com",
            state="California",
            sources=json.dumps(SOURCE_CHOICES[1:3])  # last 2 sources
        )
        parsed_org = {
            "name": "Google, Inc",
            "website": "google.com",
            "state": "California",
            "source": SOURCE_CHOICES[0]
        }
        # WHEN: I try to match the parsed record to an existing record
        matched_org = get_matched_org(parsed_org)
        # THEN: I get the Org with the matching website
        self.assertEqual(str(matched_org.id), str(pre_existing_org_top_prio.id))


class PersonMatchingTestCase(TestCase):
    def test_match_personal_email_and_job_title(self):
        pre_existing_person = PersonFactory.create(email='foo@bar.com')
        pre_existing_org = OrgFactory.create()

        # role is needed in order to return a match
        RoleFactory.create(
            person=pre_existing_person, org=pre_existing_org,
            job_title='unicorn')
        person_dict = {
            "first_name": "foo",
            "last_name": "bar",
            "email": "foo@bar.com",
            "job_title": "unicorn"
        }
        matched_person, _ = get_matched_person_and_role(person_dict)
        self.assertEqual(matched_person.id, pre_existing_person.id)

    def test_match_role_email_and_title(self):
        pre_existing_org = OrgFactory.create()
        pre_existing_person = PersonFactory.create()
        RoleFactory.create(
            person=pre_existing_person, org=pre_existing_org,
            email='foo@bar.com', job_title='unicorn')
        person_dict = {
            "first_name": "foo",
            "last_name": "bar",
            "email": "foo@bar.com",
            "job_title": "unicorn"
        }
        matched_person, _ = get_matched_person_and_role(person_dict)
        self.assertEqual(matched_person.id, pre_existing_person.id)

    def test_no_match_email(self):
        person_dict = {
            "first_name": "foo",
            "last_name": "bar",
            "email": "foo@bar.com"
        }
        matched_person, _ = get_matched_person_and_role(person_dict)
        self.assertIsNone(matched_person)

    def test_match_person_same_email_not_role_new_title(self):
        pre_existing_org = OrgFactory.create()
        pre_existing_person = PersonFactory.create()
        RoleFactory.create(
            person=pre_existing_person, org=pre_existing_org,
            email='foo@bar.com', job_title='unicorn')
        person_dict = {
            "first_name": "foo",
            "last_name": "bar",
            "email": "foo@bar.com",
            "job_title": "wizard"  # new job title -> new role
        }
        matched_person, matched_role = get_matched_person_and_role(person_dict)
        self.assertEqual(matched_person.id, pre_existing_person.id)
        self.assertIsNone(matched_role)
