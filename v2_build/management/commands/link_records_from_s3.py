from django.core.management.base import BaseCommand

from lead_api.utils.models import LongProcess
from lead_api.utils.tasks_transactions import cleanup_marks, S3_PREFIX
from lead_api.utils.tasks_transactions import get_redis_connection
from lead_api.v2_build.tasks import process_bucket


class Command(BaseCommand):

    def add_arguments(self, parser):
        parser.add_argument(
            '--bucket',
            action='store',
            default=None,
            help='URL to folder in s3 bucket to search for files'
        )
        parser.add_argument(
            '--force-restart',
            action='store_true'
        )

    def handle(self, *args, **options):
        redis = get_redis_connection()
        bucket = options['bucket']

        old_state_keys = redis.keys(S3_PREFIX + "*")
        if old_state_keys:
            clear_old_state = raw_input(
                "Old S3 state information exists in redis. "
                "Would you like to clear out old state before starting? Y/n \n")
            if clear_old_state.lower() in ['y', 'yes']:
                print "Deleting old S3 state information..."
                cleanup_marks(redis)
            else:
                del old_state_keys  # free up memory

        if options['force_restart']:
            cleanup_marks(redis)
        LongProcess.objects.create(
            status=LongProcess.Status.IN_PROGRESS,
            type=LongProcess.Types.ORG_AND_PERSON_LINKING,
            metadata=options
        )
        process_bucket(bucket)
