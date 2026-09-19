import json
import logging

from core.models import Person, Role
from lead_api.v2_build.linking.mergers import PersonMerger, RoleMerger

logger = logging.getLogger(__name__)


def link_person_and_role(person_dict, org_id, role_metadata_dict=None):
    """
    @person dict of person_data
    @updated_org Org model instance
    """
    matched_person, matched_role = get_matched_person_and_role(person_dict)

    person_dict, role_dict = split_person_and_role_dict(person_dict)

    if matched_person:
        updated_person = PersonMerger(matched_person, person_dict).merge_and_update()
    else:
        updated_person = insert_person(person_dict)

    if matched_role:
        updated_role = RoleMerger(matched_role, role_dict, role_metadata_dict).merge_and_update()
    else:
        updated_role = insert_role(role_dict, org_id, updated_person, role_metadata_dict)

    return updated_person, updated_role


def get_matched_person_and_role(person_dict):
    email = person_dict.get('email')
    matched_person = None
    matched_role = None
    if email:
        # get Person by personal email or Role email; kinda confusing
        # we don't really treat personal and work emails differently

        # do one query for all candidates, then pick match; don't hit the db more than once
        # these need to be indexed!

        # look up person and role emails separately
        candidate_matched_roles = Role.objects.filter(email=email).select_related('person')
        if candidate_matched_roles:
            matched_role = choose_role_from_candidates(candidate_matched_roles, person_dict)
            if matched_role:
                matched_person = matched_role.person
            else:
                # return a Person from a role if possible
                matched_person = choose_person_from_candidates(
                    candidate_persons=[role.person for role in candidate_matched_roles],
                    person_dict=person_dict)
                if not matched_person:
                    matched_person = candidate_matched_roles[0].person
        else:
            logger.info(u"No matching Role found for {}".format(email))
            candidate_matched_persons = Person.objects.filter(email=email)
            if candidate_matched_persons:
                matched_person = choose_person_from_candidates(
                    candidate_persons=candidate_matched_persons,
                    person_dict=person_dict)
                if not matched_person:
                    matched_person = candidate_matched_persons[0]
    if matched_role:
        assert matched_person
    return matched_person, matched_role


def choose_role_from_candidates(candidate_roles, role_dict):
    incoming_job_title = role_dict.get('job_title') or ''
    if incoming_job_title:
        for role in candidate_roles:
            if role.job_title.lower() == incoming_job_title.lower():
                return role
    all_roles_have_blank_job_title = not any([role.job_title for role in candidate_roles])
    if all_roles_have_blank_job_title:
        # return latest role
        latest_role = sorted(candidate_roles, key=lambda role: getattr(role, 'start_date'))
        return latest_role[-1]

    return None


def choose_person_from_candidates(candidate_persons, person_dict):
    first_name = person_dict.get('first_name', '')
    last_name = person_dict.get('last_name', '')
    email = person_dict.get('email', '')
    for person in candidate_persons:
        if all([
            first_name.lower() == person.first_name.lower(),
            last_name.lower() == person.last_name.lower(),
            email.lower() == person.email.lower(),
        ]):
            return person
    return None


def insert_person(person_dict):
    person_dict['external_ids'] = json.dumps([person_dict.pop('external_id')])

    # hacknasty - prevent None from being in the final dict
    # TODO - coordinate default values better
    person_dict['linkedin_url'] = person_dict.get('linkedin_url') or ''
    person_dict['email_status'] = person_dict.get('email_status') or ''
    person_dict['functional_area'] = person_dict.get('functional_area') or ''
    person_dict['previous_position_1_title'] = person_dict.get('previous_position_1_title') or ''
    person_dict['previous_position_1_company_name'] = person_dict.get(
        'previous_position_1_company_name') or ''
    person_dict['previous_position_1_start_year'] = person_dict.get(
        'previous_position_1_start_year') or ''
    person_dict['previous_position_2_title'] = person_dict.get('previous_position_2_title') or ''
    person_dict['previous_position_2_company_name'] = person_dict.get(
        'previous_position_2_company_name') or ''
    person_dict['previous_position_2_start_year'] = person_dict.get(
        'previous_position_2_start_year') or ''
    person_dict['skills'] = person_dict.get('skills') or ''
    person_dict['fax'] = person_dict.get('fax') or ''

    person = Person(**person_dict)
    logger.info(u"Inserting new Person record: {} {} <{}>".format(
        person.first_name,
        person.last_name,
        person.email
    ))
    person.save()
    return person


def insert_role(role_dict, org_id, person, role_metadata_dict=None):
    role_dict.pop('external_id', None)  # TODO store this on the Role model for tracking
    role_dict.update(role_metadata_dict or {})
    role_dict = {key: value for key, value in role_dict.items() if value}
    role = Role(org_id=org_id, person=person, **role_dict)
    logger.info(u"Inserting new Role record: Org {}, Person {}, {}".format(
        org_id,
        person.id,
        role.email
    ))
    role.save()
    return role


def split_person_and_role_dict(person_data):
    role_dict = get_role_dict(person_data)
    person_dict = get_person_dict(person_data)
    external_id = person_data.get('external_id', '')
    person_dict['external_id'] = external_id
    # for tracking purposes
    role_dict['external_id'] = external_id

    return person_dict, role_dict


def get_role_dict(person_data):
    role_dict = {}
    for field in Role.get_nonrelational_fields():
        if field.name in person_data:
            role_dict[field.name] = person_data[field.name]

    # special case for phone since in Role it is called "work_phone"
    phone = person_data.get("phone")
    if phone:
        role_dict['work_phone'] = phone

    # Temporary fix to remove created_at column for EMM
    role_dict.pop('created_at', None)
    return role_dict


def get_person_dict(person_data):
    person_dict = {}
    for field in Person.get_nonrelational_fields():
        if field.name in person_data:
            person_dict[field.name] = person_data[field.name]
    # Temporary fix to remove created_at column for EMM
    person_dict.pop('created_at', None)
    return person_dict
