"""
These models are used in the databases of content that get imported from Studio.
Any fields added here (and not in base_models.py) are assumed to be locally
calculated cached fields. If a field is intended to be imported from a content
database generated by Studio, it should be added in base_models.py.


*DEVELOPER WARNING regarding updates to these models*

If you modify the schema here, it has implications for the content import pipeline
because we will need to calculate these values during content import (as we they will
not be present in the content databases distributed by Studio).

In the case where new fields are added that do not need to be added to an export schema
the generate_schema command should be run like this:

    `kolibri manage generate_schema current`

This will just regenerate the current schema for SQLAlchemy, so that we can use SQLAlchemy
to calculate these fields if needed (this can frequently be more efficient than using the
Django ORM for these calculations).
"""
from __future__ import print_function

import os
from gettext import gettext as _

from django.core.urlresolvers import reverse
from django.db import connection
from django.db import models
from django.db.models import Min
from django.db.models import Q
from django.utils.encoding import python_2_unicode_compatible
from django.utils.text import get_valid_filename
from le_utils.constants import content_kinds
from le_utils.constants import format_presets
from mptt.managers import TreeManager
from mptt.querysets import TreeQuerySet

from .utils import paths
from kolibri.core.content import base_models
from kolibri.core.content.errors import InvalidStorageFilenameError
from kolibri.core.device.models import ContentCacheKey
from kolibri.core.mixins import FilterByUUIDQuerysetMixin

PRESET_LOOKUP = dict(format_presets.choices)


@python_2_unicode_compatible
class ContentTag(base_models.ContentTag):
    def __str__(self):
        return self.tag_name


class ContentNodeQueryset(TreeQuerySet, FilterByUUIDQuerysetMixin):
    def dedupe_by_content_id(self):
        # remove duplicate content nodes based on content_id
        if connection.vendor == "sqlite":
            # adapted from https://code.djangoproject.com/ticket/22696
            deduped_ids = (
                self.values("content_id")
                .annotate(node_id=Min("id"))
                .values_list("node_id", flat=True)
            )
            return self.filter_by_uuids(deduped_ids)

        # when using postgres, we can call distinct on a specific column
        elif connection.vendor == "postgresql":
            return self.order_by("content_id").distinct("content_id")

    def filter_by_content_ids(self, content_ids, validate=True):
        return self._by_uuids(content_ids, validate, "content_id", True)

    def exclude_by_content_ids(self, content_ids, validate=True):
        return self._by_uuids(content_ids, validate, "content_id", False)


class ContentNodeManager(
    models.Manager.from_queryset(ContentNodeQueryset), TreeManager
):
    def get_queryset(self, *args, **kwargs):
        """
        Ensures that this manager always returns nodes in tree order.
        """
        return (
            super(TreeManager, self)
            .get_queryset(*args, **kwargs)
            .order_by(self.tree_id_attr, self.left_attr)
        )


@python_2_unicode_compatible
class ContentNode(base_models.ContentNode):
    """
    The primary object type in a content database. Defines the properties that are shared
    across all content types.

    It represents videos, exercises, audio, documents, and other 'content items' that
    exist as nodes in content channels.
    """

    # Fields used only on Kolibri and not imported from a content database
    # Total number of coach only resources for this node
    num_coach_contents = models.IntegerField(default=0, null=True, blank=True)
    # Total number of available resources on the device under this topic - if this is not a topic
    # then it is 1 or 0 depending on availability
    on_device_resources = models.IntegerField(default=0, null=True, blank=True)

    objects = ContentNodeManager()

    class Meta:
        ordering = ("lft",)
        index_together = [
            ["level", "channel_id", "kind"],
            ["level", "channel_id", "available"],
        ]

    def __str__(self):
        return self.title

    def get_descendant_content_ids(self):
        """
        Retrieve a queryset of content_ids for non-topic content nodes that are
        descendants of this node.
        """
        return (
            ContentNode.objects.filter(lft__gte=self.lft, lft__lte=self.rght)
            .exclude(kind=content_kinds.TOPIC)
            .values_list("content_id", flat=True)
        )


@python_2_unicode_compatible
class Language(base_models.Language):
    def __str__(self):
        return self.lang_name or ""


class File(base_models.File):
    """
    The second to bottom layer of the contentDB schema, defines the basic building brick for content.
    Things it can represent are, for example, mp4, avi, mov, html, css, jpeg, pdf, mp3...
    """

    class Meta:
        ordering = ["priority"]

    class Admin:
        pass

    def get_extension(self):
        return self.local_file.extension

    def get_file_size(self):
        return self.local_file.file_size

    def get_storage_url(self):
        return self.local_file.get_storage_url()

    def get_preset(self):
        """
        Return the preset.
        """
        return PRESET_LOOKUP.get(self.preset, _("Unknown format"))

    def get_download_filename(self):
        """
        Return a valid filename to be downloaded as.
        """
        title = self.contentnode.title
        filename = "{} ({}).{}".format(title, self.get_preset(), self.get_extension())
        valid_filename = get_valid_filename(filename)
        return valid_filename

    def get_download_url(self):
        """
        Return the download url.
        """
        new_filename = self.get_download_filename()
        return reverse(
            "kolibri:core:downloadcontent",
            kwargs={
                "filename": self.local_file.get_filename(),
                "new_filename": new_filename,
            },
        )


class LocalFileManager(models.Manager):
    def delete_unused_files(self):
        for file in self.get_unused_files():
            try:
                os.remove(paths.get_content_storage_file_path(file.get_filename()))
                yield True, file
            except (IOError, OSError, InvalidStorageFilenameError):
                yield False, file
        self.get_unused_files().update(available=False)

    def get_orphan_files(self):
        return self.filter(files__isnull=True)

    def delete_orphan_file_objects(self):
        return self.filter(files__isnull=True).delete()

    def get_unused_files(self):
        return self.filter(
            ~Q(files__contentnode__available=True) | Q(files__isnull=True)
        ).filter(available=True)


@python_2_unicode_compatible
class LocalFile(base_models.LocalFile):
    """
    The bottom layer of the contentDB schema, defines the local state of files on the device storage.
    """

    objects = LocalFileManager()

    class Admin:
        pass

    def __str__(self):
        return paths.get_content_file_name(self)

    def get_filename(self):
        return self.__str__()

    def get_storage_url(self):
        """
        Return a url for the client side to retrieve the content file.
        The same url will also be exposed by the file serializer.
        """
        return paths.get_local_content_storage_file_url(self)

    def delete_stored_file(self):
        """
        Delete the stored file from disk.
        """
        deleted = False

        try:
            os.remove(paths.get_content_storage_file_path(self.get_filename()))
            deleted = True
        except (IOError, OSError, InvalidStorageFilenameError):
            deleted = False

        self.available = False
        self.save()
        return deleted


class AssessmentMetaData(base_models.AssessmentMetaData):
    """
    A model to describe additional metadata that characterizes assessment behaviour in Kolibri.
    This model contains additional fields that are only revelant to content nodes that probe a
    user's state of knowledge and allow them to practice to Mastery.
    ContentNodes with this metadata may also be able to be used within quizzes and exams.
    """

    pass


@python_2_unicode_compatible
class ChannelMetadata(base_models.ChannelMetadata):
    """
    Holds metadata about all existing content databases that exist locally.
    """

    # precalculated fields during annotation/migration
    published_size = models.BigIntegerField(default=0, null=True, blank=True)
    total_resource_count = models.IntegerField(default=0, null=True, blank=True)
    included_languages = models.ManyToManyField(
        "Language", related_name="channels", verbose_name="languages", blank=True
    )
    order = models.PositiveIntegerField(default=0, null=True, blank=True)
    public = models.NullBooleanField()

    class Admin:
        pass

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return self.name

    def delete_content_tree_and_files(self):
        # Use Django ORM to ensure cascading delete:
        self.root.delete()
        ContentCacheKey.update_cache_key()
