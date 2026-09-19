"""
Terms:
linking - high level process of finding a match, combining old and new data, and saving new data
matching - return an existing record (or None) that a parsed record should be merged into
merging - combine old and new data according to priority rules and update metadata
update/insert - save to datastore

Same terms apply for person linking.
"""
import json
import logging

from difflib import SequenceMatcher

from core.models import Org
from core.record_parsing.attributes import ParsedAttribute
from core.record_parsing.text_normalization import normalize_search_org_name, normalize_search_street_name
from lead_api.events.signals import org_signal_handler
from lead_api.v2_build.config import ORG_WEBSITE_BLACKLIST
from lead_api.v2_build.linking.mergers import OrgMerger


logger = logging.getLogger(__name__)


def link_org(org_dict, org_metadata=None):
    matched_org = get_matched_org(org_dict)
    if matched_org:
        updated_org = OrgMerger(matched_org, org_dict, org_metadata).merge_and_update()
    else:
        if is_valid_org(org_dict):
            logger.info(u"No matching Org found for {} ({}); inserting new record".format(
                org_dict.get('name'), org_dict.get('website')))
            updated_org = insert_org(org_dict, org_metadata)
        else:
            logger.info(
                u"No matching Org found for {} ({}) and Org record not valid; skipping it".format(
                    org_dict.get('name'), org_dict.get('website')))
            updated_org = None
    return updated_org


def get_matched_org(org_dict, min_similarity_score=1.0):
    org_candidates = get_org_candidates(org_dict)
    matched_org = choose_org_from_candidates(org_candidates, org_dict, min_similarity_score)
    return matched_org


def get_org_candidates(org_dict):
    return Org.objects.get_similar_orgs(org_dict.get('website'), org_dict.get('name'), org_dict.get('state'))


def choose_org_from_candidates(candidate_orgs, target_org, min_similarity_score, top=True):
    # choose Org with top source priority
    orgs_and_priorities = ((org, calculate_similarity_score(org, target_org)) for org in candidate_orgs)
    orgs_and_priorities = (row for row in orgs_and_priorities if row[1] >= min_similarity_score)
    orgs_and_priorities = sorted(orgs_and_priorities, key=lambda row: row[1], reverse=True)
    # later can add much smarter logic for matching
    if orgs_and_priorities:
        if top:
            return orgs_and_priorities[0][0]
        return orgs_and_priorities
    return None


def calculate_similarity_score(candidate_org, target_org):
    target_name = target_org.get('name')
    target_street = target_org.get('street_address')
    target_website = target_org.get('website')
    target_state = target_org.get('state')
    target_city = target_org.get('city')
    target_source = target_org.get('source', '')

    if not target_name:
        return 0.0

    # define score per point matched
    scores = {
        'state': 0.2,
        'city': 0.2,
        'website': 0.4,
        'street': 0.6,
        'name_w_street': 0.5,
        'name_wo_street': 0.7,
    }
    req_street_score = 0.95
    req_name_score = 0.90
    perfrect_score = 1.0
    min_street_length = 5

    # increase 1% the score if target_org.source in candidate_org.sources
    source_in_sources_bonus = 1.01 if target_source in candidate_org.sources else 1

    # define normalized values
    norm_source_street = normalize_search_street_name(candidate_org.street_address)
    norm_target_street = normalize_search_street_name(target_street)
    norm_source_name = normalize_search_org_name(candidate_org.name)
    norm_target_name = normalize_search_org_name(target_name)

    # calculate scores
    street_match_score = SequenceMatcher(a=norm_source_street, b=norm_target_street).quick_ratio()
    name_match_score = SequenceMatcher(a=norm_source_name, b=norm_target_name).quick_ratio()

    # ignore short values like "17", "NE", etc.
    streets_not_empty = candidate_org.street_address != '' and target_street != ''
    streets_longer_than_5 = len(norm_target_street) > min_street_length and len(norm_source_street) > min_street_length

    matches = set([])

    # add street match if empty values or score is higher or equal to required
    if not streets_not_empty or (street_match_score >= req_street_score):
        matches.add('street')

    # if we got a perfect street match we can try to relax requirements for names
    if street_match_score == perfrect_score and streets_longer_than_5 and streets_not_empty:
        req_name_score = req_name_score - 0.15

    # match org name using adjusted required score
    if name_match_score >= req_name_score:
        if streets_not_empty:
            # matching using street use 0.5
            matches.add('name_w_street')
        else:
            # add extra points when matching without street to compensate for possible street score
            matches.add('name_wo_street')

    if target_state and (candidate_org.state == target_state):
        matches.add('state')

    if target_city and (candidate_org.city == target_city):
        matches.add('city')

    if target_website and (candidate_org.website == target_website):
        matches.add('website')

    # calculate score
    score = sum(scores[match] for match in matches)
    if score > 1.0:
        return source_in_sources_bonus * score
    return score


def insert_org(org_dict, org_metadata_dict=None):
    # insert Org, OrgAttribute, Person, Role
    # validate before insert
    attributes = org_dict.pop('attributes') or []

    org_source = org_dict.pop('source')
    org_dict['sources'] = [org_source]
    org_dict['external_ids'] = [org_dict.pop('external_id')]
    org_dict.update(org_metadata_dict or {})
    # hack for now - refactor Org.from_json so re-serialization is not necessary
    json_string = json.dumps(org_dict)

    org = Org.from_json(json_string)

    present_fields = [key for key, value in org_dict.items() if value]
    org.field_sources = {f.name: org_source for f in OrgMerger.get_fields_for_merging()
                         if f.name in present_fields}

    # this should be the only place we touch the db
    # do industry and tech together to save a few round trips to the db
    logger.info(u"Inserting new Org record: {} {}".format(
        org.name,
        org.website
    ))

    # disconnect signal and manually trigger afterwards
    with org_signal_handler.temporarily_suspend():
        org.save(force_insert=True)
        parsed_attributes = [
            ParsedAttribute(
                attribute_type=attribute_type,
                value_or_code=value_or_code
            ) for attribute_type, value_or_code in attributes
        ]
        attribute_instances = ParsedAttribute.get_attribute_instances(parsed_attributes)
        org.upsert_orgattributes(attribute_instances, org_source)
        # we can set the cached attributes bc we are inserting and we have all of them
        org.set_attributes(attribute_instances)
    org_signal_handler.trigger_event(instance=org, created=True)
    return org


def is_valid_org(org_dict):
    name = org_dict.get('name', '')
    website = org_dict.get('website', '')
    street_address = org_dict.get('street_address', '')
    ecommerce_package = org_dict.get('ecommerce_package')
    if website in ORG_WEBSITE_BLACKLIST:
        return False
    return (name and website) or (name and street_address) or ecommerce_package
