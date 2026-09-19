import json
import logging

from core.models import Person, Role, Org
from core.record_parsing.attributes import ParsedAttribute
from lead_api.events.signals import org_signal_handler
from lead_api.v2_build.source_priority import source_priority_index

logger = logging.getLogger(__name__)


class Merger(object):
    def __init__(self, model_instance, new_data_dict, new_metadata_dict=None):
        self.model_instance = model_instance
        self.new_data_dict = new_data_dict
        self.new_metadata = new_metadata_dict or {}
        self._updated = False

    def update_field(self, field, new_value, new_source=None, new_external_id=None):
        """
        Set field value and update field sources
        """
        old_value = getattr(self.model_instance, field)
        source = new_source or new_external_id  # hack bc person does not have sources right now
        if not old_value == new_value:
            logger.debug(u"[Updating {} ({})] {} from '{}' to '{}' per source '{}'".format(
                self._simple_identifier(),
                self.model_instance.id,
                field,
                old_value,
                new_value,
                source
            ))
            setattr(self.model_instance, field, new_value)
        # even if the old value and new value are the same, if we would have updated it, we'll track
        # the new source instead of old
        if new_source:
            self.model_instance.field_sources[field] = new_source
            self.union_to_sources(new_source)
        if new_external_id:
            self.union_to_external_ids(new_external_id)

    def should_update_field(self, new_source, old_source, new_field_value, old_field_value):
        # newsource prio higher (lower index) and new field not empty
        # OR old field empty and new field not empty

        # note - check for absence of old_field first because if old_field is empty and old_source is
        # None, then source_priority_index will throw an error
        if new_field_value is not None and not old_source:
            return True
        return (
            new_field_value and
            (
                not old_field_value or
                source_priority_index(new_source) <= source_priority_index(old_source)
            )
        )

    def union_to_sources(self, new_source):
        self.model_instance.sources = self._update_set_char_field(
            self.model_instance.sources, new_source)

    def union_to_external_ids(self, new_external_id):
        self.model_instance.external_ids = self._update_set_char_field(
            self.model_instance.external_ids, new_external_id)

    def _update_set_char_field(self, original_value_set_chars, new_value):
        """Append to set fields."""
        if original_value_set_chars:
            chars_list = set(json.loads(original_value_set_chars))
        else:
            chars_list = set()
        chars_list.add(new_value)
        return json.dumps(list(chars_list))

    @classmethod
    def get_fields_for_merging(cls):
        raise NotImplementedError()

    def _simple_identifier(self):
        """Similar to unicode but avoid joins. Convenience for logging."""
        raise NotImplementedError()

    def update_metadata(self):
        for key, new_value in self.new_metadata.items():
            old_value = getattr(self.model_instance, key)
            if old_value != new_value:
                setattr(self.model_instance, key, new_value)
                self._updated = True


class OrgMerger(Merger):
    model = Org
    LOCATION_FIELDS = [
        "street_address",
        "city",
        "state",
        "county",
        "country",
        "postal_code"
    ]

    def merge_and_update(self):
        self.new_source = self.new_data_dict['source']
        new_external_id = self.new_data_dict['external_id']
        # process attributes separately
        attributes = self.new_data_dict.pop('attributes') or []

        # process location fields separately
        new_location_data = self.extract_location_fields_from_dict()
        old_location_data = self.extract_location_fields_from_org()
        for field, value in self.new_data_dict.items():
            if field in [f.name for f in self.get_fields_for_merging()]:
                if self.should_update_field(
                        new_source=self.new_source,
                        old_source=self.model_instance.field_sources.get(field),
                        new_field_value=self.new_data_dict.get(field),
                        old_field_value=getattr(self.model_instance, field)):
                    self._updated = True
                    self.update_field(field, value, self.new_source, new_external_id)

        # update address fields together bc otherwise we will create chimera addresses
        if self.should_update_address_fields(
                new_source=self.new_source,
                old_source=self.model_instance.field_sources.get(field),
                new_location_data=new_location_data,
                old_location_data=old_location_data):
            self._updated = True
            for field, value in new_location_data.items():
                self.update_field(field, value, self.new_source, new_external_id)

        # update metadata
        self.update_metadata()

        # disconnect signal and manually trigger afterwards
        with org_signal_handler.temporarily_suspend():
            # this should be the only place we touch the db
            if self._updated:
                self.model_instance.save()
            # do industry and tech together to save a few round trips to the db
            self.update_attributes(attributes)
        org_signal_handler.trigger_event(instance=self.model_instance, created=False)
        return self.model_instance

    def update_attributes(self, attributes):
        parsed_attributes = [ParsedAttribute(
            attribute_type=attribute_type,
            value_or_code=value_or_code
        ) for attribute_type, value_or_code in attributes]
        attribute_instances = ParsedAttribute.get_attribute_instances(parsed_attributes)
        self.model_instance.upsert_orgattributes(attribute_instances, self.new_source)

    @classmethod
    def get_fields_for_merging(cls):
        non_merged_fields = [
            'id',
            'created_at',
            'last_modified_at',
            'sources',
            'external_ids'
        ]
        return [
            field for field in Org.get_nonrelational_fields()
            if field.name not in non_merged_fields
        ]

    def should_update_address_fields(
            self, new_source, old_source,
            new_location_data, old_location_data):
        if not old_source and new_location_data:
            return True
        return (
            self.is_complete_enough_address(new_location_data) and
            (
                not self.is_complete_enough_address(old_location_data) or
                source_priority_index(new_source) <= source_priority_index(old_source)
            )
        )

    def extract_location_fields_from_dict(self):
        location_data = {}
        for field in self.LOCATION_FIELDS:
            location_data[field] = self.new_data_dict.pop(field, '') or ''
        return location_data

    def extract_location_fields_from_org(self):
        location_data = {}
        for field in self.LOCATION_FIELDS:
            location_data[field] = getattr(self.model_instance, field)
        return location_data

    def is_complete_enough_address(self, address_dict):
        return all([
            address_dict.get('street_address'),
            address_dict.get('city'),
            address_dict.get('state'),
        ])

    def _simple_identifier(self):
        return unicode(self.model_instance.name)


class PersonRoleMerger(Merger):
    def get_fields_for_merging(self):
        non_merged_fields = [
            'id',
            'created_at',
            'last_modified_at',
        ]
        return [
            field for field in self.model.get_nonrelational_fields()
            if field.name not in non_merged_fields
        ]

    def should_update_field(self, old_field_value, new_field_value):
        # placeholder implementation; we don't track field by field source, or multiple sources for
        # person, so we can't really implement any logic here yet
        return bool(new_field_value)


class PersonMerger(PersonRoleMerger):
    model = Person

    def merge_and_update(self):
        new_external_id = self.new_data_dict['external_id']

        for field, value in self.new_data_dict.items():
            if field in [f.name for f in self.get_fields_for_merging()]:
                if self.should_update_field(
                    old_field_value=getattr(self.model_instance, field),
                    new_field_value=self.new_data_dict.get(field)
                ):
                    self._updated = True
                    self.update_field(field, value, new_external_id=new_external_id)

        # update metadata
        self.update_metadata()

        # this should be the only place we touch the db
        if self._updated:
            self.model_instance.save()
        return self.model_instance

    def _simple_identifier(self):
        return u"{} {} <{}>".format(
            self.model_instance.first_name,
            self.model_instance.last_name,
            self.model_instance.email
        )


class RoleMerger(PersonRoleMerger):
    model = Role

    def merge_and_update(self):
        for field, value in self.new_data_dict.items():
            if field in [f.name for f in self.get_fields_for_merging()]:
                if self.should_update_field(
                    old_field_value=getattr(self.model_instance, field),
                    new_field_value=self.new_data_dict.get(field)
                ):
                    self._updated = True
                    self.update_field(field, value)

        # update metadata
        self.update_metadata()

        # this should be the only place we touch the db
        if self._updated:
            self.model_instance.save()
        return self.model_instance

    def _simple_identifier(self):
        return u"{} <{}>".format(
            self.model_instance.job_title,
            self.model_instance.email
        )
