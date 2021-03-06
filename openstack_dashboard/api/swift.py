# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
# Copyright 2012 Nebula, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import logging

import swiftclient

from django.conf import settings  # noqa
from django.utils.translation import ugettext_lazy as _  # noqa

from horizon import exceptions
from horizon import messages

from openstack_dashboard.api import base
from openstack_dashboard.openstack.common import timeutils


LOG = logging.getLogger(__name__)
FOLDER_DELIMITER = "/"


class Container(base.APIDictWrapper):
    pass


class StorageObject(base.APIDictWrapper):
    def __init__(self, apidict, container_name, orig_name=None, data=None):
        super(StorageObject, self).__init__(apidict)
        self.container_name = container_name
        self.orig_name = orig_name
        self.data = data

    @property
    def id(self):
        return self.name


class PseudoFolder(base.APIDictWrapper):
    def __init__(self, apidict, container_name):
        super(PseudoFolder, self).__init__(apidict)
        self.container_name = container_name

    @property
    def id(self):
        return '%s/%s' % (self.container_name, self.name)

    @property
    def name(self):
        return self.subdir.rstrip(FOLDER_DELIMITER)

    @property
    def bytes(self):
        return None

    @property
    def content_type(self):
        return "application/pseudo-folder"


def _objectify(items, container_name):
    """Splits a listing of objects into their appropriate wrapper classes."""
    objects = []

    # Deal with objects and object pseudo-folders first, save subdirs for later
    for item in items:
        if item.get("subdir", None) is not None:
            object_cls = PseudoFolder
        else:
            object_cls = StorageObject

        objects.append(object_cls(item, container_name))

    return objects


def swift_api(request):
    endpoint = base.url_for(request, 'object-store')
    cacert = getattr(settings, 'OPENSTACK_SSL_CACERT', None)
    LOG.debug('Swift connection created using token "%s" and url "%s"'
              % (request.user.token.id, endpoint))
    return swiftclient.client.Connection(None,
                                         request.user.username,
                                         None,
                                         preauthtoken=request.user.token.id,
                                         preauthurl=endpoint,
                                         cacert=cacert,
                                         auth_version="2.0")


def swift_container_exists(request, container_name):
    try:
        swift_api(request).head_container(container_name)
        return True
    except swiftclient.client.ClientException:
        return False


def swift_object_exists(request, container_name, object_name):
    try:
        swift_api(request).head_object(container_name, object_name)
        return True
    except swiftclient.client.ClientException:
        return False


def swift_get_containers(request, marker=None):
    limit = getattr(settings, 'API_RESULT_LIMIT', 1000)
    headers, containers = swift_api(request).get_account(limit=limit + 1,
                                                         marker=marker,
                                                         full_listing=True)
    container_objs = [Container(c) for c in containers]
    if(len(container_objs) > limit):
        return (container_objs[0:-1], True)
    else:
        return (container_objs, False)


def swift_get_container(request, container_name, with_data=True):
    if with_data:
        headers, data = swift_api(request).get_object(container_name, "")
    else:
        data = None
        headers = swift_api(request).head_container(container_name)
    timestamp = None
    try:
        ts_float = float(headers.get('x-timestamp'))
        timestamp = timeutils.iso8601_from_timestamp(ts_float)
    except Exception:
        pass
    container_info = {
        'name': container_name,
        'container_object_count': headers.get('x-container-object-count'),
        'container_bytes_used': headers.get('x-container-bytes-used'),
        'timestamp': timestamp,
        'data': data,
    }
    return Container(container_info)


def swift_create_container(request, name):
    if swift_container_exists(request, name):
        raise exceptions.AlreadyExists(name, 'container')
    swift_api(request).put_container(name)
    return Container({'name': name})


def swift_delete_container(request, name):
    # It cannot be deleted if it's not empty. The batch remove of objects
    # be done in swiftclient instead of Horizon.
    objects, more = swift_get_objects(request, name)
    if objects:
        messages.warning(request,
            _("The container cannot be deleted since it's not empty."))
        return False
    swift_api(request).delete_container(name)
    return True


def swift_get_objects(request, container_name, prefix=None, marker=None,
                      limit=None):
    limit = limit or getattr(settings, 'API_RESULT_LIMIT', 1000)
    kwargs = dict(prefix=prefix,
                  marker=marker,
                  limit=limit + 1,
                  delimiter=FOLDER_DELIMITER,
                  full_listing=True)
    headers, objects = swift_api(request).get_container(container_name,
                                                          **kwargs)
    object_objs = _objectify(objects, container_name)

    if(len(object_objs) > limit):
        return (object_objs[0:-1], True)
    else:
        return (object_objs, False)


def swift_filter_objects(request, filter_string, container_name, prefix=None,
                         marker=None):
    # FIXME(kewu): Swift currently has no real filtering API, thus the marker
    # parameter here won't actually help the pagination. For now I am just
    # getting the largest number of objects from a container and filtering
    # based on those objects.
    limit = 9999
    objects = swift_get_objects(request,
                                container_name,
                                prefix=prefix,
                                marker=marker,
                                limit=limit)
    filter_string_list = filter_string.lower().strip().split(' ')

    def matches_filter(obj):
        for q in filter_string_list:
            return wildcard_search(obj.name.lower(), q)

    return filter(matches_filter, objects[0])


def wildcard_search(string, q):
    q_list = q.split('*')
    if all(map(lambda x: x == '', q_list)):
        return True
    elif q_list[0] not in string:
        return False
    else:
        if q_list[0] == '':
            tail = string
        else:
            head, delimiter, tail = string.partition(q_list[0])
        return wildcard_search(tail, '*'.join(q_list[1:]))


def swift_copy_object(request, orig_container_name, orig_object_name,
                      new_container_name, new_object_name):
    if swift_object_exists(request, new_container_name, new_object_name):
        raise exceptions.AlreadyExists(new_object_name, 'object')

    headers = {"X-Copy-From": FOLDER_DELIMITER.join([orig_container_name,
                                                     orig_object_name])}
    return swift_api(request).put_object(new_container_name,
                                         new_object_name,
                                         None,
                                         headers=headers)


def swift_upload_object(request, container_name, object_name, object_file):
    headers = {}
    headers['X-Object-Meta-Orig-Filename'] = object_file.name
    etag = swift_api(request).put_object(container_name,
                                         object_name,
                                         object_file,
                                         headers=headers)
    obj_info = {'name': object_name, 'bytes': object_file.size, 'etag': etag}
    return StorageObject(obj_info, container_name)


def swift_create_pseudo_folder(request, container_name, pseudo_folder_name):
    headers = {}
    etag = swift_api(request).put_object(container_name,
                                         pseudo_folder_name,
                                         None,
                                         headers=headers)
    obj_info = {
        'name': pseudo_folder_name,
        'etag': etag
    }

    return PseudoFolder(obj_info, container_name)


def swift_delete_object(request, container_name, object_name):
    swift_api(request).delete_object(container_name, object_name)
    return True


def swift_get_object(request, container_name, object_name, with_data=True):
    if with_data:
        headers, data = swift_api(request).get_object(container_name,
                                                      object_name)
    else:
        data = None
        headers = swift_api(request).head_object(container_name,
                                                 object_name)
    orig_name = headers.get("x-object-meta-orig-filename")
    timestamp = None
    try:
        ts_float = float(headers.get('x-timestamp'))
        timestamp = timeutils.iso8601_from_timestamp(ts_float)
    except Exception:
        pass
    obj_info = {
        'name': object_name,
        'bytes': headers.get('content-length'),
        'content_type': headers.get('content-type'),
        'etag': headers.get('etag'),
        'timestamp': timestamp,
    }
    return StorageObject(obj_info,
                         container_name,
                         orig_name=orig_name,
                         data=data)
