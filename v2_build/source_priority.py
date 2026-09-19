import json
import logging
from operator import indexOf

logger = logging.getLogger(__name__)

ADMIN_EDIT_SOURCE = 'admin'
PREMIERE_SOURCE = 'premiere'

# must match SourceClass.source but we don't import here to avoid importing spark
PRIORITY_LIST = [
    ADMIN_EDIT_SOURCE,
    PREMIERE_SOURCE,
    'hgdata',
    'orb',
    'netwise',
    'linkedin',
    'yelp',
    'builtwith',
    'leadslead',
    'squarespreadsheet',
    'emm',
    'builtwithlist',
    'amazon',
    'ebay',
    'etsy',
    'walmart',
    'alibaba',
    'leadpile'  # this is a fake source; it's just preexisting data where we've lost the real source
]


def get_min_source_priority(org):
    # remove org and json
    sources = json.loads(org.sources)
    return min([source_priority_index(source) for source in sources])


def source_priority_index(source):
    try:
        index = indexOf(PRIORITY_LIST, source)
    except ValueError:
        logger.critical("Invalid source: {}".format(source))
        raise
    return index
