#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Implements simple file system browsing operations.
#
# Useful resources:
#   django/views/static.py manages django's internal directory index

import errno
import logging
import mimetypes
import operator
import posixpath
import stat as stat_module
import os
import simplejson

from django.core import urlresolvers
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.views.static import was_modified_since
from django.utils.http import http_date, urlquote
from django.utils.html import escape
from cStringIO import StringIO
from gzip import GzipFile
from avro import datafile, io

from desktop.lib import i18n, paginator
from desktop.lib.conf import coerce_bool
from desktop.lib.django_util import make_absolute, render, render_json
from desktop.lib.django_util import PopupException, format_preserving_redirect
from filebrowser.lib.rwx import filetype, rwx
from filebrowser.lib import xxd
from filebrowser.forms import RenameForm, UploadForm, MkDirForm, RmDirForm, RmTreeForm,\
    RemoveForm, ChmodForm, ChownForm, EditorForm
from hadoop.fs.hadoopfs import Hdfs
from hadoop.fs.exceptions import WebHdfsException

from django.utils.translation import ugettext as _


DEFAULT_CHUNK_SIZE_BYTES = 1024 * 4 # 4KB
MAX_CHUNK_SIZE_BYTES = 1024 * 1024 # 1MB
DOWNLOAD_CHUNK_SIZE = 32 * 1024 # 32KB

# Defaults for "xxd"-style output.
# Sentences refer to groups of bytes printed together, within a line.
BYTES_PER_LINE = 16
BYTES_PER_SENTENCE = 2

# The maximum size the file editor will allow you to edit
MAX_FILEEDITOR_SIZE = 256 * 1024

logger = logging.getLogger(__name__)


def index(request):
  # Redirect to home directory by default
  path = request.user.get_home_directory()
  if not request.fs.isdir(path):
    path = '/'
  return view(request, path)


def _file_reader(fh):
    """Generator that reads a file, chunk-by-chunk."""
    while True:
        chunk = fh.read(DOWNLOAD_CHUNK_SIZE)
        if chunk == '':
            fh.close()
            break
        yield chunk


def download(request, path):
    """
    Downloads a file.

    This is inspired by django.views.static.serve.
    """
    if not request.fs.exists(path):
        raise Http404(_("File not found: %(path)s") % {'path': escape(path)})
    if not request.fs.isfile(path):
        raise PopupException(_("'%(path)s' is not a file") % {'path': path})

    mimetype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    stats = request.fs.stats(path)
    mtime = stats['mtime']
    size = stats['size']
    if not was_modified_since(request.META.get('HTTP_IF_MODIFIED_SINCE'), mtime, size):
        return HttpResponseNotModified()
        # TODO(philip): Ideally a with statement would protect from leaks,
    # but tricky to do here.
    fh = request.fs.open(path)

    response = HttpResponse(_file_reader(fh), mimetype=mimetype)
    response["Last-Modified"] = http_date(stats['mtime'])
    response["Content-Length"] = stats['size']
    response["Content-Disposition"] = "attachment"
    return response


def view(request, path):
    """Dispatches viewing of a path to either index() or fileview(), depending on type."""

    # default_to_home is set in bootstrap.js
    if 'default_to_home' in request.GET:
        home_dir_path = request.user.get_home_directory()
        if request.fs.isdir(home_dir_path):
            return format_preserving_redirect(request, urlresolvers.reverse(view, kwargs=dict(path=home_dir_path)))

    try:
        stats = request.fs.stats(path)
        if stats.isDir:
            return listdir_paged(request, path)
        else:
            return display(request, path)
    except (IOError, WebHdfsException), e:
        msg = _("Cannot access: %(path)s.") % {'path': escape(path)}
        if request.user.is_superuser and not request.user == request.fs.superuser:
            msg += _(' Note: you are a Hue admin but not a HDFS superuser (which is "%(superuser)s").') % {'superuser': request.fs.superuser}
        raise PopupException(msg , detail=e)


def edit(request, path, form=None):
    """Shows an edit form for the given path. Path does not necessarily have to exist."""

    try:
        stats = request.fs.stats(path)
    except IOError, ioe:
        # A file not found is OK, otherwise re-raise
        if ioe.errno == errno.ENOENT:
            stats = None
        else:
            raise

    # Can't edit a directory
    if stats and stats['mode'] & stat_module.S_IFDIR:
        raise PopupException(_("Cannot edit a directory: %(path)s") % {'path': path})

    # Maximum size of edit
    if stats and stats['size'] > MAX_FILEEDITOR_SIZE:
        raise PopupException(_("File too big to edit: %(path)s") % {'path': path})

    if not form:
        encoding = request.REQUEST.get('encoding') or i18n.get_site_encoding()
        if stats:
            f = request.fs.open(path)
            try:
                try:
                    current_contents = unicode(f.read(), encoding)
                except UnicodeDecodeError:
                    raise PopupException(_("File is not encoded in %(encoding)s; cannot be edited: %(path)s") % {'encoding': encoding, 'path': path})
            finally:
                f.close()
        else:
            current_contents = u""

        form = EditorForm(dict(path=path, contents=current_contents, encoding=encoding))

    data = dict(
        exists=(stats is not None),
        form=form,
        path=path,
        filename=os.path.basename(path),
        dirname=os.path.dirname(path),
        breadcrumbs = parse_breadcrumbs(path))
    return render("edit.mako", request, data)


def save_file(request):
    """
    The POST endpoint to save a file in the file editor.

    Does the save and then redirects back to the edit page.
    """
    form = EditorForm(request.POST)
    is_valid = form.is_valid()
    path = form.cleaned_data.get('path')

    if request.POST.get('save') == "Save As":
        if not is_valid:
            return edit(request, path, form=form)
        else:
            data = dict(form=form)
            return render("saveas.mako", request, data)

    if not path:
        raise PopupException("No path specified")
    if not is_valid:
        return edit(request, path, form=form)

    if request.fs.exists(path):
        _do_overwrite_save(request.fs, path,
                           form.cleaned_data['contents'],
                           form.cleaned_data['encoding'])
    else:
        _do_newfile_save(request.fs, path,
                         form.cleaned_data['contents'],
                         form.cleaned_data['encoding'])

    request.flash.put(_('Saved %(path)s.') % {'path': os.path.basename(path)})
    """ Changing path to reflect the request path of the JFrame that will actually be returned."""
    request.path = urlresolvers.reverse("filebrowser.views.edit", kwargs=dict(path=path))
    return edit(request, path, form)


def _do_overwrite_save(fs, path, data, encoding):
    """
    Atomically (best-effort) save the specified data to the given path
    on the filesystem.

    TODO(todd) should this be in some fsutil.py?
    """
    # TODO(todd) Should probably do an advisory permissions check here to
    # see if we're likely to fail (eg make sure we own the file
    # and can write to the dir)

    # First write somewhat-kinda-atomically to a staging file
    # so that if we fail, we don't clobber the old one
    path_dest = path + "._hue_new"

    new_file = fs.open(path_dest, "w")
    try:
        try:
            new_file.write(data.encode(encoding))
            logging.info("Wrote to " + path_dest)
        finally:
            new_file.close()
    except Exception, e:
        # An error occurred in writing, we should clean up
        # the tmp file if it exists, before re-raising
        try:
            fs.remove(path_dest)
        except:
            pass
        raise e

    # Try to match the permissions and ownership of the old file
    cur_stats = fs.stats(path)
    try:
        fs.chmod(path_dest, stat_module.S_IMODE(cur_stats['mode']))
    except:
        logging.warn("Could not chmod new file %s to match old file %s" % (
            path_dest, path), exc_info=True)
        # but not the end of the world - keep going

    try:
        fs.chown(path_dest, cur_stats['user'], cur_stats['group'])
    except:
        logging.warn("Could not chown new file %s to match old file %s" % (
            path_dest, path), exc_info=True)
        # but not the end of the world - keep going

    # Now delete the old - nothing we can do here to recover
    fs.remove(path)

    # Now move the new one into place
    # If this fails, then we have no reason to assume
    # we can do anything to recover, since we know the
    # destination shouldn't already exist (we just deleted it above)
    fs.rename(path_dest, path)


def _do_newfile_save(fs, path, data, encoding):
    """
    Save data to the path 'path' on the filesystem 'fs'.

    There must not be a pre-existing file at that path.
    """
    new_file = fs.open(path, "w")
    try:
        new_file.write(data.encode(encoding))
    finally:
        new_file.close()


def parse_breadcrumbs(path):
    breadcrumbs_parts = Hdfs.normpath(path).split('/')
    i = 1
    breadcrumbs = [{'url': '', 'label': '/'}]
    while (i < len(breadcrumbs_parts)):
        breadcrumb_url = breadcrumbs[i - 1]['url'] + '/' + breadcrumbs_parts[i]
        if breadcrumb_url != '/':
            breadcrumbs.append({'url': breadcrumb_url, 'label': breadcrumbs_parts[i]})
        i = i + 1
    return breadcrumbs


def listdir(request, path, chooser):
    """
    Implements directory listing (or index).

    Intended to be called via view().
    """
    if not request.fs.isdir(path):
        raise PopupException(_("Not a directory: %(path)s") % {'path': path})

    file_filter = request.REQUEST.get('file_filter', 'any')

    assert file_filter in ['any', 'file', 'dir']

    home_dir_path = request.user.get_home_directory()

    breadcrumbs = parse_breadcrumbs(path)

    data = {
        'path': path,
        'file_filter': file_filter,
        'breadcrumbs': breadcrumbs,
        'current_dir_path': path,
        # These could also be put in automatically via
        # http://docs.djangoproject.com/en/dev/ref/templates/api/#django-core-context-processors-request,
        # but manually seems cleaner, since we only need it here.
        'current_request_path': request.path,
        'home_directory': request.fs.isdir(home_dir_path) and home_dir_path or None,
        'cwd_set': True,
        'show_upload': (request.REQUEST.get('show_upload') == 'false' and (False,) or (True,))[0]
    }

    stats = request.fs.listdir_stats(path)

    # Include parent dir, unless at filesystem root.
    if Hdfs.normpath(path) != posixpath.sep:
        parent_path = request.fs.join(path, "..")
        parent_stat = request.fs.stats(parent_path)
        # The 'path' field would be absolute, but we want its basename to be
        # actually '..' for display purposes. Encode it since _massage_stats expects byte strings.
        parent_stat['path'] = parent_path
        stats.insert(0, parent_stat)

    data['files'] = [_massage_stats(request, stat) for stat in stats]
    if chooser:
        return render('chooser.mako', request, data)
    else:
        return render('listdir.mako', request, data)

def listdir_paged(request, path):
    """
    A paginated version of listdir.

    Query parameters:
      pagenum           - The page number to show. Defaults to 1.
      pagesize          - How many to show on a page. Defaults to 15.
      sortby=?          - Specify attribute to sort by. Accepts:
                            (type, name, atime, mtime, size, user, group)
                          Defaults to name.
      descending        - Specify a descending sort order.
                          Default to false.
      filter=?          - Specify a substring filter to search for in
                          the filename field.
    """
    if not request.fs.isdir(path):
        raise PopupException("Not a directory: %s" % (path,))

    pagenum = int(request.GET.get('pagenum', 1))
    pagesize = int(request.GET.get('pagesize', 30))

    home_dir_path = request.user.get_home_directory()
    breadcrumbs = parse_breadcrumbs(path)

    all_stats = request.fs.listdir_stats(path)

    # Include parent dir, unless at filesystem root.
    if Hdfs.normpath(path) != posixpath.sep:
        parent_path = request.fs.join(path, "..")
        parent_stat = request.fs.stats(parent_path)
        # The 'path' field would be absolute, but we want its basename to be
        # actually '..' for display purposes. Encode it since _massage_stats expects byte strings.
        parent_stat['path'] = parent_path
        parent_stat['name'] = ".."
        all_stats.insert(0, parent_stat)

    # Filter first
    filter_str = request.GET.get('filter', None)
    if filter_str:
        filtered_stats = filter(lambda sb: filter_str in sb['name'], all_stats)
        all_stats = filtered_stats

    # Sort next
    sortby = request.GET.get('sortby', None)
    descending_param = request.GET.get('descending', None)
    if sortby is not None:
        if sortby not in ('type', 'name', 'atime', 'mtime', 'user', 'group', 'size'):
            logger.info("Invalid sort attribute '%s' for listdir." %
                        (sortby,))
        else:
            all_stats = sorted(all_stats,
                               key=operator.attrgetter(sortby),
                               reverse=coerce_bool(descending_param))

    # Do pagination
    page = paginator.Paginator(all_stats, pagesize).page(pagenum)
    shown_stats = page.object_list
    page.object_list = [ _massage_stats(request, s) for s in shown_stats ]

    data = {
        'path': path,
        'breadcrumbs': breadcrumbs,
        'current_request_path': request.path,
        'files': page.object_list,
        'page': page,
        'pagesize': pagesize,
        'home_directory': request.fs.isdir(home_dir_path) and home_dir_path or None,
        'filter_str': filter_str,
        'sortby': sortby,
        'descending': descending_param,
        # The following should probably be deprecated
        'cwd_set': True,
        'file_filter': 'any',
        'current_dir_path': path,
    }
    return render('listdir.mako', request, data)


def chooser(request, path):
    """
    Returns the html to JFrame that will display a file prompt.

    Dispatches viewing of a path to either index() or fileview(), depending on type.
    """
    # default_to_home is set in bootstrap.js
    home_dir_path = request.user.get_home_directory()
    if 'default_to_home' in request.GET and request.fs.isdir(home_dir_path):
        return listdir(request, home_dir_path, True)

    if request.fs.isdir(path):
        return listdir(request, path, True)
    elif request.fs.isfile(path):
        return display(request, path)
    else:
        raise Http404(_("File not found: %(path)s") % {'path': escape(path)})


def _massage_stats(request, stats):
    """
    Massage a stats record as returned by the filesystem implementation
    into the format that the views would like it in.
    """
    path = stats['path']
    normalized = Hdfs.normpath(path)
    return {
        'path': normalized,
        'name': stats['name'],
        'stats': stats.to_json_dict(),
        'type': filetype(stats['mode']),
        'rwx': rwx(stats['mode']),
        'url': make_absolute(request, "view", dict(path=urlquote(normalized))),
        }


def stat(request, path):
    """
    Returns just the generic stats of a file.

    Intended for use via AJAX (and hence doesn't provide
    an HTML view).
    """
    if not request.fs.exists(path):
        raise Http404(_("File not found: %(path)s") % {'path': escape(path)})
    stats = request.fs.stats(path)
    return render_json(_massage_stats(request, stats))


def display(request, path):
    """
    Implements displaying part of a file.

    GET arguments are length, offset, mode, compression and encoding
    with reasonable defaults chosen.

    Note that display by length and offset are on bytes, not on characters.

    TODO(philip): Could easily built-in file type detection
    (perhaps using something similar to file(1)), as well
    as more advanced binary-file viewing capability (de-serialize
    sequence files, decompress gzipped text files, etc.).
    There exists a python-magic package to interface with libmagic.
    """
    if not request.fs.isfile(path):
        raise PopupException(_("Not a file: '%(path)s'") % {'path': path})

    stats = request.fs.stats(path)
    encoding = request.GET.get('encoding') or i18n.get_site_encoding()

    # I'm mixing URL-based parameters and traditional
    # HTTP GET parameters, since URL-based parameters
    # can't naturally be optional.

    # Need to deal with possibility that length is not present
    # because the offset came in via the toolbar manual byte entry.
    end = request.GET.get("end")
    if end:
        end = int(end)
    begin = request.GET.get("begin", 1)
    if begin:
        # Subtract one to zero index for file read
        begin = int(begin) - 1
    if end:
        offset = begin
        length = end - begin
        if begin >= end:
            raise PopupException(_("First byte to display must be before last byte to display."))
    else:
        length = int(request.GET.get("length", DEFAULT_CHUNK_SIZE_BYTES))
        # Display first block by default.
        offset = int(request.GET.get("offset", 0))

    mode = request.GET.get("mode")
    compression = request.GET.get("compression")

    if mode and mode not in ["binary", "text"]:
        raise PopupException(_("Mode must be one of 'binary' or 'text'."))
    if offset < 0:
        raise PopupException(_("Offset may not be less than zero."))
    if length < 0:
        raise PopupException(_("Length may not be less than zero."))
    if length > MAX_CHUNK_SIZE_BYTES:
        raise PopupException(_("Cannot request chunks greater than %(bytes)d bytes") % {'bytes': MAX_CHUNK_SIZE_BYTES})

    # Do not decompress in binary mode.
    if mode == 'binary':
        compression = 'none'
        # Read out based on meta.
    compression, offset, length, contents =\
    read_contents(compression, path, request.fs, offset, length)

    # Get contents as string for text mode, or at least try
    uni_contents = None
    if not mode or mode == 'text':
        uni_contents = unicode(contents, encoding, errors='replace')
        is_binary = uni_contents.find(i18n.REPLACEMENT_CHAR) != -1
        # Auto-detect mode
        if not mode:
            mode = is_binary and 'binary' or 'text'

    # Get contents as bytes
    if mode == "binary":
        xxd_out = list(xxd.xxd(offset, contents, BYTES_PER_LINE, BYTES_PER_SENTENCE))

    dirname = posixpath.dirname(path)
    # Start with index-like data:
    data = _massage_stats(request, request.fs.stats(path))
    # And add a view structure:
    data["success"] = True
    data["view"] = {
        'offset': offset,
        'length': length,
        'end': offset + len(contents),
        'dirname': dirname,
        'mode': mode,
        'compression': compression,
        'size': stats['size']
    }
    data["filename"] = os.path.basename(path)
    data["editable"] = stats['size'] < MAX_FILEEDITOR_SIZE
    if mode == "binary":
        # This might be the wrong thing for ?format=json; doing the
        # xxd'ing in javascript might be more compact, or sending a less
        # intermediate representation...
        logger.debug("xxd: " + str(xxd_out))
        data['view']['xxd'] = xxd_out
        data['view']['masked_binary_data'] = False
    else:
        data['view']['contents'] = uni_contents
        data['view']['masked_binary_data'] = is_binary

    data['breadcrumbs'] = parse_breadcrumbs(path)

    return render("display.mako", request, data)


def read_contents(codec_type, path, fs, offset, length):
    """
    Reads contents of a passed path, by appropriately decoding the data.
    Arguments:
       codec_type - The type of codec to use to decode. (Auto-detected if None).
       path - The path of the file to read.
       fs - The FileSystem instance to use to read.
       offset - Offset to seek to before read begins.
       length - Amount of bytes to read after offset.
       Returns: A tuple of codec_type, offset, length and contents read.
    """
    # Auto codec detection for [gzip, avro, none]
    # Only done when codec_type is unset
    if not codec_type:
        if path.endswith('.gz') and detect_gzip(fs.open(path).read(2)):
            codec_type = 'gzip'
            offset = 0
        elif path.endswith('.avro') and detect_avro(fs.open(path).read(3)):
            codec_type = 'avro'
        else:
            codec_type = 'none'

    f = fs.open(path)
    contents = ''

    if codec_type == 'gzip':
        contents = _read_gzip(fs, path, offset, length)
    elif codec_type == 'avro':
        contents = _read_avro(fs, path, offset, length)
    else:
        # for 'none' type.
        contents = _read_simple(fs, path, offset, length)

    return (codec_type, offset, length, contents)


def _read_avro(fs, path, offset, length):
    contents = ''
    try:
        fhandle = fs.open(path)
        try:
            fhandle.seek(offset)
            data_file_reader = datafile.DataFileReader(fhandle, io.DatumReader())
            contents_list = []
            read_start = fhandle.tell()
            # Iterate over the entire sought file.
            for datum in data_file_reader:
                read_length = fhandle.tell() - read_start
                if read_length > length and len(contents_list) > 0:
                    break
                else:
                    datum_str = str(datum) + "\n"
                    contents_list.append(datum_str)
            data_file_reader.close()
            contents = "".join(contents_list)
        except:
            logging.warn("Could not read avro file at %s" % path, exc_info=True)
            raise PopupException(_("Failed to read Avro file."))
    finally:
        fhandle.close()
    return contents


def _read_gzip(fs, path, offset, length):
    contents = ''
    if offset and offset != 0:
        raise PopupException(_("Offsets are not supported with Gzip compression."))
    try:
        fhandle = fs.open(path)
        try:
            contents = GzipFile('', 'r', 0, StringIO(fhandle.read())).read(length)
        except:
            logging.warn("Could not decompress file at %s" % path, exc_info=True)
            raise PopupException(_("Failed to decompress file."))
    finally:
        fhandle.close()
    return contents


def _read_simple(fs, path, offset, length):
    contents = ''
    try:
        fhandle = fs.open(path)
        try:
            fhandle.seek(offset)
            contents = fhandle.read(length)
        except:
            logging.warn("Could not read file at %s" % path, exc_info=True)
            raise PopupException(_("Failed to read file."))
    finally:
        fhandle.close()
    return contents


def detect_gzip(contents):
    '''This is a silly small function which checks to see if the file is Gzip'''
    return contents[:2] == '\x1f\x8b'


def detect_avro(contents):
    '''This is a silly small function which checks to see if the file is Avro'''
    # Check if the first three bytes are 'O', 'b' and 'j'
    return contents[:3] == '\x4F\x62\x6A'


def _calculate_navigation(offset, length, size):
    """
    List of (offset, length, string) tuples for suggested navigation through the file.
    If offset is -1, then this option is already "selected".  (Whereas None would
    be the natural pythonic way, Django's template syntax doesn't let us test
    against None (since its truth value is the same as 0).)

    By all means this logic ought to be in the template, but the template
    language is too limiting.
    """
    if offset == 0:
        first, prev = (-1, None, _("First Block")), (-1, None, _("Previous Block"))
    else:
        first, prev = (0, length, _("First Block")), (max(0, offset - length), length, _("Previous Block"))

    if offset + length >= size:
        next, last = (-1, None, _("Next Block")), (-1, None, _("Last Block"))
    else:
        # 1-off Reasoning: if length is the same as size, you want to start at 0.
        next, last = (offset + length, length, _("Next Block")), (max(0, size - length), length, _("Last Block"))

    return first, prev, next, last


def generic_op(form_class, request, op, parameter_names, piggyback=None, template="fileop.mako", extra_params=None):
    """
    Generic implementation for several operations.

    @param form_class form to instantiate
    @param request incoming request, used for parameters
    @param op callable with the filesystem operation
    @param parameter_names list of form parameters that are extracted and then passed to op
    @param piggyback list of form parameters whose file stats to look up after the operation
    @param extra_params dictionary of extra parameters to send to the template for rendering
    """
    # Use next for non-ajax requests, when available.
    next = request.GET.get("next")
    if next is None:
        next = request.POST.get("next")

    ret = dict({
        'next': next
    })

    if extra_params is not None:
        ret['extra_params'] = extra_params

    for p in parameter_names:
        val = request.REQUEST.get(p)
        if val:
            ret[p] = val

    if request.method == 'POST':
        form = form_class(request.POST)
        ret['form'] = form
        if form.is_valid():
            args = [form.cleaned_data[p] for p in parameter_names]
            try:
                op(*args)
            except (IOError, WebHdfsException), e:
                msg = _("Cannot perform operation.")
                if request.user.is_superuser and not request.user == request.fs.superuser:
                    msg += _(' Note: you are a Hue admin but not a HDFS superuser (which is "%(superuser)s").') \
                           % {'superuser': request.fs.superuser}
                raise PopupException(msg, detail=e)
            if next:
                logging.debug("Next: %s" % next)
                # Doesn't need to be quoted: quoting is done by HttpResponseRedirect.
                return format_preserving_redirect(request, next)
            ret["success"] = True
            try:
                if piggyback:
                    piggy_path = form.cleaned_data[piggyback]
                    ret["result"] = _massage_stats(request, request.fs.stats(piggy_path))
            except Exception, e:
                # Hard to report these more naturally here.  These happen either
                # because of a bug in the piggy-back code or because of a
                # race condition.
                logger.exception("Exception while processing piggyback data")
                ret["result_error"] = True

            return render(template, request, ret)
    else:
        # Initial parameters may be specified with get
        initial_values = {}
        for p in parameter_names:
            val = request.GET.get(p)
            if val:
                initial_values[p] = val
        form = form_class(initial=initial_values)
        ret['form'] = form
    return render(template, request, ret)


def move(request):
    return generic_op(RenameForm, request, request.fs.rename, ["src_path", "dest_path"], None, template="move.mako")


def rename(request):
    def smart_rename(src_path, dest_path):
        """If dest_path doesn't have a directory specified, use same dir."""
        if "/" not in dest_path:
            src_dir = os.path.dirname(src_path)
            dest_path = os.path.join(src_dir, dest_path)
        request.fs.rename(src_path, dest_path)

    return generic_op(RenameForm, request, smart_rename, ["src_path", "dest_path"], None)


def mkdir(request):
    def smart_mkdir(path, name):
        # Make sure only one directory is specified at a time.
        # No absolute directory specification allowed.
        if posixpath.sep in name:
            raise PopupException(_("Sorry, could not name folder \"%s\": Slashes are not allowed in filenames." % name))
        request.fs.mkdir(os.path.join(path, name))

    return generic_op(MkDirForm, request, smart_mkdir, ["path", "name"], "path")


def remove(request):
    return generic_op(RemoveForm, request, request.fs.remove, ["path"], None)


def rmdir(request):
    return generic_op(RmDirForm, request, request.fs.rmdir, ["path"], None)


def rmtree(request):
    return generic_op(RmTreeForm, request, request.fs.rmtree, ["path"], None)


def chmod(request):
    # mode here is abused: on input, it's a string, but when retrieved,
    # it's an int.
    return generic_op(ChmodForm, request, request.fs.chmod, ["path", "mode"], "path", template="chmod.mako")


def chown(request):
    # This is a bit clever: generic_op takes an argument (here, args), indicating
    # which POST parameters to pick out and pass to the given function.
    # We update that mapping based on whether or not the user selected "other".
    args = ["path", "user", "group"]
    if request.POST.get("user") == "__other__":
        args[1] = "user_other"
    if request.POST.get("group") == "__other__":
        args[2] = "group_other"

    return generic_op(ChownForm, request, request.fs.chown, args, "path", template="chown.mako",
                      extra_params=dict(current_user=request.user, superuser=request.fs.superuser))


def upload_flash(request):
    """
    Our flash uploader is bad at handling errors, so, instead
    of using the regular exception-handling, we use a
    special signifier.
    """
    try:
        r = upload(request)
        if r.status_code == 200:
            return HttpResponse("{}", content_type="application/json")
        else:
            raise Exception("Unknown error")
    except Exception, e:
        return HttpResponse(simplejson.dumps(dict(error=unicode(e))),
                            content_type="application/json")


def upload(request):
    """
    A wrapper around the actual upload view function to clean up the
    temporary file afterwards.
    """
    try:
        return _upload(request)
    finally:
        if request.method == 'POST':
            try:
                upload_file = request.FILES['hdfs_file']
                upload_file.remove()
            except KeyError:
                pass


def _upload(request):
    """
    Handles file uploads. The uploaded file is stored in HDFS. We just
    need to rename it to the right path.
    """
    if request.method == 'POST':
        form = UploadForm(request.POST, request.FILES)
        if not form.is_valid():
            logger.error("Error in upload form: %s" % (form.errors,))
        else:
            uploaded_file = request.FILES['hdfs_file']
            dest = form.cleaned_data["dest"]
            if request.fs.isdir(dest):
                assert posixpath.sep not in uploaded_file.name
                dest = request.fs.join(dest, uploaded_file.name)

            # Temp file is created by superuser. Chown the file.
            tmp_file = uploaded_file.get_temp_path()
            username = request.user.username
            try:
                try:
                    request.fs.setuser(request.fs.superuser)
                    request.fs.chmod(tmp_file, 0644)
                    request.fs.chown(tmp_file, username, username)
                except IOError, ex:
                    msg = _('Failed to chown uploaded file ("%(file)s") as superuser %(superuser)s.') %\
                          {'file': tmp_file, 'superuser': request.fs.superuser}
                    logger.exception(msg)
                    raise PopupException(msg, detail=str(ex))
            finally:
                request.fs.setuser(username)

            # Move the file to where it belongs
            try:
                request.fs.rename(uploaded_file.get_temp_path(), dest)
            except IOError, ex:
                raise PopupException(
                    _('Failed to rename uploaded temporary file ("%(file)s") to "%(name)s": %(error)s') %
                    {'file': tmp_file, 'name': dest, 'error': ex})

            dest_stats = request.fs.stats(dest)
            return render('upload_done.mako', request, {
                # status is used by "fancy uploader"
                'status': 1,
                'path': dest,
                'result': _massage_stats(request, dest_stats),
                'next': request.GET.get("next")
            })
    else:
        dest = request.GET.get("dest")
        initial_values = {}
        if dest:
            initial_values["dest"] = dest
        form = UploadForm(initial=initial_values)
    return render('upload.mako', request,
            {'form': form, 'next': request.REQUEST.get("dest")})


def status(request):
    status = request.fs.status()
    data = {
        # Beware: "messages" is special in the context browser.
        'msgs': status.get_messages(),
        'health': status.get_health(),
        'datanode_report': status.get_datanode_report(),
        'name': request.fs.name
    }
    return render("status.mako", request, data)


def location_to_url(request, location, strict=True):
    """
    If possible, returns a file browser URL to the location.
    Location is a URI, if strict is True.

    Python doesn't seem to have a readily-available URI-comparison
    library, so this is quite hacky.
    """
    if location is None:
      return None
    split_path = request.fs.urlsplit(location)
    if strict and not split_path[1]:
      # No netloc, not full url
      return None
    return urlresolvers.reverse("filebrowser.views.view", kwargs=dict(path=split_path[2]))

def truncate(toTruncate, charsToKeep=50):
    """
    Returns a string truncated to 'charsToKeep' length plus ellipses.
    """
    if len(toTruncate) > charsToKeep:
        truncated = toTruncate[:charsToKeep] + "..."
        return truncated
    else:
        return toTruncate
