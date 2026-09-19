from django.conf import settings
from django.core.management.base import BaseCommand
import redis

from lead_api.utils.librato_api import submit_librato_metric


class Command(BaseCommand):

    def handle(self, *args, **options):
        conn = redis.from_url(settings.BROKER_URL)
        info = conn.info()
        fraction_used_memory = info['used_memory'] * 1.0 / info['maxmemory']
        submit_librato_metric('redis_fraction_used_memory', fraction_used_memory)
