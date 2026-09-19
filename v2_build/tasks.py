from __future__ import absolute_import

from datetime import timedelta
import json
import logging
import os
import time

from celery.task import periodic_task
from django.conf import settings

from core.utils import s3
from lead_api.celery_app import app, MONITORING_QUEUE_NAME
from lead_api.utils import tasks_transactions
from lead_api.utils.librato_api import submit_librato_metric
from lead_api.utils.tasks import submit_queue_length_metric
from lead_api.v2_build.linking import person as person_linking
from lead_api.v2_build.linking.org import link_org

QUEUE_WITH_FILES = 'v2_build_files'  # must match in procfile
QUEUE_WITH_RECORDS = 'v2_build_records'  # must match in procfile
QUEUE_WITH_PERSONS = 'v2_build_person_linker'  # must match in procfile

logger = logging.getLogger(__name__)


@app.task(bind=True, queue='v2_build')
def another_debug_task(self):
    print('Request: {0!r}'.format(self.request))


@app.task(bind=True, queue='v2_build')
def raise_exception_task(self):
    """For testing error handling inside Celery tasks"""
    raise Exception("Uh oh")


def parse_values_from_path(path):
    """
    Used to extract country=foo and state=bar from S3 path (as put there by spark partitionBy()
    """
    result = {}
    while True:
        path, segment = os.path.split(path)
        if not segment:
            break
        if "=" in segment:
            name, value = segment.split("=", 1)
            result[name] = value
    return result


def wait_queue_ready(queue, max_size):
    while tasks_transactions.get_message_count(queue) > max_size:
        print("waiting")
        time.sleep(1)


# retry many times, but with long delay
PROCESS_S3_FILE_TASK_RETRY_POLICY = {
    'max_retries': 100,
    'interval_start': 10,
    'interval_step': 30,
    'interval_max': 600
}


@app.task(queue=QUEUE_WITH_FILES, retry_policy=PROCESS_S3_FILE_TASK_RETRY_POLICY)
def process_s3_file(bucket_url, filename):
    redis = tasks_transactions.get_redis_connection()
    content = s3.download(bucket_url, filename)
    wait_queue_ready(QUEUE_WITH_RECORDS, settings.QUEUE_WITH_RECORDS_SIZE)
    values_encoded_in_path = parse_values_from_path(filename)
    unprocessed, unprocessed_after = tasks_transactions.find_unprocessed_tasks(redis, filename)
    for lineno, line in enumerate(content.splitlines()):
        if lineno < unprocessed_after:
            if lineno not in unprocessed:
                continue
        data = json.loads(line)
        data.update(values_encoded_in_path)
        tasks_transactions.mark_task_as_started(redis, filename, lineno)
        link_org_and_persons.delay(filename, lineno, json.dumps(data))


def process_bucket(path):
    redis = tasks_transactions.get_redis_connection()
    start = time.time()
    for key in s3.enlist(path):
        wait_queue_ready(QUEUE_WITH_FILES, settings.QUEUE_WITH_FILES_SIZE)
        print key.name
        process_s3_file.delay(path, key.name)
        print "RATE {:.2f} records/second".format(
            tasks_transactions.get_processed_count(redis) / (time.time() - start)
        )


LINKER_TASK_RETRY_POLICY = {
    'max_retries': 5,
    'interval_start': 1,
    'interval_step': 30,
    'interval_max': 600
}


@app.task(queue=QUEUE_WITH_RECORDS, retry_policy=LINKER_TASK_RETRY_POLICY)
@tasks_transactions.transactional_marking
def link_org_and_persons(filename, lineno, parsed_org_json_line):
    # filename, lineno are throwaway arguments used for the transaction_marking decorator
    # TODO cleanup/remove them

    org_dict = json.loads(parsed_org_json_line)
    external_id = org_dict['external_id']
    logger.info("Processing parsed org record with external id {}".format(external_id))
    linked_org = link_org(org_dict)
    # process persons separately
    if linked_org:
        person_data = org_dict.pop('person_data', [])
        for person in person_data:
            # TODO encapsulate this better
            person['external_id'] = external_id
            link_person_and_role.delay(person, str(linked_org.id))


@app.task(queue=QUEUE_WITH_PERSONS, retry_policy=LINKER_TASK_RETRY_POLICY)
def link_person_and_role(person, org_id):
    person_linking.link_person_and_role(person, org_id)


@periodic_task(queue=MONITORING_QUEUE_NAME, run_every=timedelta(seconds=10.0), max_retries=None)
def submit_linker_queue_length_metric():
    submit_queue_length_metric(queue_name=QUEUE_WITH_RECORDS)


@periodic_task(queue=MONITORING_QUEUE_NAME, run_every=timedelta(seconds=10.0), max_retries=None)
def submit_linker_person_queue_length_metric():
    submit_queue_length_metric(queue_name=QUEUE_WITH_PERSONS)


@periodic_task(queue=MONITORING_QUEUE_NAME, run_every=timedelta(seconds=10.0), max_retries=None)
def submit_linker_s3_file_processor_queue_length_metric():
    submit_queue_length_metric(queue_name=QUEUE_WITH_FILES)


@periodic_task(queue=MONITORING_QUEUE_NAME, run_every=timedelta(seconds=10.0), max_retries=None)
def submit_records_processed_metric():
    redis = tasks_transactions.get_redis_connection()
    count = tasks_transactions.get_processed_count(redis)
    submit_librato_metric(metric_name='v2_linker_total_records_processed', value=count)
