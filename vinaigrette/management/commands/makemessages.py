# Copyright (c) Ecometrica. All rights reserved.
# Distributed under the BSD license. See LICENSE for details.
from __future__ import print_function

import codecs
import os
import re
import glob

from django.core.management.base import CommandError
from django.core.management.commands import makemessages as django_makemessages
from django.conf import settings

import vinaigrette


class Command(django_makemessages.Command):
    help = ('Runs over the entire source tree of the current directory and pulls out '
            'all strings marked for translation. It creates (or updates) a message file '
            'in the conf/locale (in the django tree) or locale (for project and application) '
            'directory. Also includes strings from database fields handled by vinaigrette.')

    TEMP_FILE_NAME = 'makemessages-temp-file.py'
    requires_system_checks = True

    # Collects sources (line with `model/field:id`) for each line in the temporary file (`TEMP_FILE_NAME`)
    # It will be used to get more descriptive references in the final `po` file
    # Reserve elements for the first two lines and "zero-line"
    po_file_sources = ['', '', '']

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument('--no-data-messages', default=True, action='store_false', dest='no-data-messages',
                            help='Don\'t include strings from database fields handled by vinaigrette.'),
        parser.add_argument('--keep-obsolete', default=False, action='store_true', dest='keep-obsolete',
                            help='Don\'t obsolete strings no longer referenced in code or Viniagrette\'s fields.')
        parser.add_argument('--keep-data-file', default=False, action='store_true', dest='keep-data-file',
                            help='Keep the temporary {} file.'.format(self.TEMP_FILE_NAME))

    def handle(self, *args, **options):
        if not options.get('no-data-messages'):
            return super().handle(*args, **options)

        self.create_tmp_file(file_path=self.TEMP_FILE_NAME)

        try:
            super().handle(*args, **options)
        finally:
            if not options.get('keep-data-file'):
                os.unlink(self.TEMP_FILE_NAME)

        self.update_po_references(options)

    def get_all_locales(self):
        # Copied from super().handle()
        locale_dirs = filter(os.path.isdir, glob.glob('{}/*'.format(self.default_locale_path)))
        return map(os.path.basename, locale_dirs)

    def create_tmp_file(self, file_path):
        # Because Django makemessages isn't very extensible, we're writing a
        # fake Python file, calling ???, then deleting it after.
        vinfile = codecs.open(file_path, 'w', encoding='utf8')

        try:
            self.stdout.write('Vinaigrette is processing database values...')

            vinfile.write('#coding:utf-8\n')
            vinfile.write('from django.utils.translation import ugettext\n')

            for model in sorted(vinaigrette._registry.keys(), key=lambda m: m._meta.object_name):
                strings_seen = set()
                modelname = '{}.{}'.format(model._meta.app_label, model._meta.object_name)

                reg = vinaigrette._registry[model]
                fields = reg['fields']  # strings to be translated
                properties = reg['properties']

                # make query_fields a set to avoid duplicates
                # only these fields will be retrieved from the db instead of all model's field
                query_fields = set(fields)

                # if there are properties, we update the needed query fields and
                # update the string that will be translated
                if properties:
                    fields += properties.keys()
                    for prop in properties.values():
                        query_fields.update(prop)

                manager = reg['manager'] or model._default_manager
                qs = manager.filter(reg['restrict_to']) if reg['restrict_to'] else manager.all()

                for instance in qs.order_by('pk').only('pk', *query_fields):
                    try:
                        idnum = int(instance.pk)
                    except (ValueError, TypeError):
                        idnum = 0
                    # iterate over fields to translate
                    for field in fields:
                        # In the reference comment in the po file, use the object's primary
                        # key as the line number, but only if it's an integer primary key
                        val = getattr(instance, field)
                        if val and val not in strings_seen:
                            strings_seen.add(val)
                            self.po_file_sources.append('{}/{}:{}'.format(modelname, field, idnum))
                            vinfile.write('ugettext({!r})\n'.format(val.replace('\r', '').replace('%', '%%')))

        finally:
            vinfile.close()

    def update_po_references(self, options):
        # The PO file has been generated and now references in this file are like:
        # # : makemessages-temp-file.py:3
        # # : makemessages-temp-file.py:4
        # etc...
        #
        # Lets swap out the line-number references to our fake python file for more descriptive references like:
        # #: products.Product/name:1
        # #: products.Product/description:1
        # #: products.Product/name:2

        def replace_line_reference(match):
            try:
                return self.po_file_sources[int(match.group(1))]
            except (IndexError, ValueError):
                return match.group(0)

        re_line_reference = re.compile(r'{!s}:(\d+)'.format(re.escape(self.TEMP_FILE_NAME)))

        if options.get('all'):
            locales = self.get_all_locales()
        else:
            locales = options.get('locale')

            # In django 1.6+ one or more locales can be specified, so we
            # make sure to handle both versions here.

            # Also, there is no simple way to differentiate a string from a
            # sequence of strings that works in both python2 (for str and
            # unicode) and python3 so we query for a string method on locales.
            if hasattr(locales, 'capitalize'):
                locales = [locales]

        po_paths = self.get_po_paths(locales)

        if options.get('keep-obsolete'):
            obsolete_warning = [
                '#. Obsolete translation kept alive\n',
                '#: obsolete:0\n'
            ]

        for po_path in po_paths:
            with open(po_path, 'r') as po_file:
                new_contents = []
                last_line = ''

                for line in po_file:
                    if line.startswith('#: '):
                        new_contents.append(re_line_reference.sub(replace_line_reference, line))
                    else:
                        if line.startswith('#, python-format') \
                                and last_line.startswith('#: ') \
                                and self.TEMP_FILE_NAME in last_line:

                            # A database string got labelled as being python-format;
                            # it shouldn't be. Skip the line.
                            continue

                        if options.get('keep-obsolete'):
                            if line in obsolete_warning:
                                # Don't preserve old obsolete warnings we inserted
                                continue
                            if line.startswith('#~ msgid '):
                                new_contents.extend(obsolete_warning)
                            if line.startswith('#~ '):
                                line = re.sub(r'^#~ ', '', line)

                        new_contents.append(line)
                    last_line = line

            with open(po_path, 'w') as po_file:
                for line in new_contents:
                    po_file.write(line)

    @staticmethod
    def get_po_paths(locales):
        """Returns paths to all relevant `po` files in the current project."""
        basedirs = [os.path.join('conf', 'locale'), 'locale']
        basedirs.extend(settings.LOCALE_PATHS)

        # Gather existing directories.
        basedirs = set(map(os.path.abspath, filter(os.path.isdir, basedirs)))

        if not basedirs:
            raise CommandError('This script should be run from the Django SVN tree or your '
                               'project or app tree, or with the settings module specified.')

        po_paths = []
        for basedir in basedirs:
            for locale in locales:
                for dirpath, dirnames, filenames in os.walk(os.path.join(basedir, locale, 'LC_MESSAGES')):
                    for f in filenames:
                        if f.endswith('.po'):
                            po_paths.append(os.path.join(dirpath, f))

        return po_paths