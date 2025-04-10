# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""This package facilitates HTTP/REST requests to the registry."""

from __future__ import absolute_import
from __future__ import division

from __future__ import print_function

import json
import re
import threading

from containerregistry.client import docker_creds
from containerregistry.client import docker_name
from containerregistry.client.v2_2 import docker_creds as v2_2_creds
import httplib2
import six.moves.http_client
import six.moves.urllib.parse

# Options for docker_http.Transport actions
PULL = 'pull'
PUSH = 'push,pull'
# For now DELETE is PUSH, which is the read/write ACL.
DELETE = PUSH
CATALOG = 'catalog'
ACTIONS = [PULL, PUSH, DELETE, CATALOG]

MANIFEST_SCHEMA1_MIME = 'application/vnd.docker.distribution.manifest.v1+json'
MANIFEST_SCHEMA1_SIGNED_MIME = 'application/vnd.docker.distribution.manifest.v1+prettyjws'  # pylint disable=line-too-long
MANIFEST_SCHEMA2_MIME = 'application/vnd.docker.distribution.manifest.v2+json'
MANIFEST_LIST_MIME = 'application/vnd.docker.distribution.manifest.list.v2+json'
LAYER_MIME = 'application/vnd.docker.image.rootfs.diff.tar.gzip'
FOREIGN_LAYER_MIME = 'application/vnd.docker.image.rootfs.foreign.diff.tar.gzip'
CONFIG_JSON_MIME = 'application/vnd.docker.container.image.v1+json'

OCI_MANIFEST_MIME = 'application/vnd.oci.image.manifest.v1+json'
OCI_IMAGE_INDEX_MIME = 'application/vnd.oci.image.index.v1+json'
OCI_LAYER_MIME = 'application/vnd.oci.image.layer.v1.tar'
OCI_GZIP_LAYER_MIME = 'application/vnd.oci.image.layer.v1.tar+gzip'
OCI_NONDISTRIBUTABLE_LAYER_MIME = 'application/vnd.oci.image.layer.nondistributable.v1.tar'  # pylint disable=line-too-long
OCI_NONDISTRIBUTABLE_GZIP_LAYER_MIME = 'application/vnd.oci.image.layer.nondistributable.v1.tar+gzip'  # pylint disable=line-too-long
OCI_CONFIG_JSON_MIME = 'application/vnd.oci.image.config.v1+json'

MANIFEST_SCHEMA1_MIMES = [MANIFEST_SCHEMA1_MIME, MANIFEST_SCHEMA1_SIGNED_MIME]
MANIFEST_SCHEMA2_MIMES = [MANIFEST_SCHEMA2_MIME]
OCI_MANIFEST_MIMES = [OCI_MANIFEST_MIME]

# OCI and Schema2 are compatible formats.
SUPPORTED_MANIFEST_MIMES = [OCI_MANIFEST_MIME, MANIFEST_SCHEMA2_MIME]

# OCI Image Index and Manifest List are compatible formats.
MANIFEST_LIST_MIMES = [OCI_IMAGE_INDEX_MIME, MANIFEST_LIST_MIME]

# Docker & OCI layer mime types indicating foreign/non-distributable layers.
NON_DISTRIBUTABLE_LAYER_MIMES = [
    FOREIGN_LAYER_MIME, OCI_NONDISTRIBUTABLE_LAYER_MIME,
    OCI_NONDISTRIBUTABLE_GZIP_LAYER_MIME
]


class Diagnostic(object):
  """Diagnostic encapsulates a Registry v2 diagnostic message.

  This captures one of the "errors" from a v2 Registry error response
  message, as outlined here:
    https://github.com/docker/distribution/blob/master/docs/spec/api.md#errors

  Args:
    error: the decoded JSON of the "errors" array element.
  """

  def __init__(self, error):
    self._error = error

  def __eq__(self, other):
    return (self.code == other.code and
            self.message == other.message and
            self.detail == other.detail)

  @property
  def code(self):
    return self._error.get('code')

  @property
  def message(self):
    return self._error.get('message')

  @property
  def detail(self):
    return self._error.get('detail')


def _DiagnosticsFromContent(content):
  """Extract and return the diagnostics from content."""
  try:
    content = content.decode('utf8')
  except:  # pylint: disable=bare-except
    # Assume it's already decoded. Defensive coding for old py2 habits that
    # are hard to break. Passing does not make the problem worse.
    pass
  try:
    o = json.loads(content)
    return [Diagnostic(d) for d in o.get('errors', [])]
  except:  # pylint: disable=bare-except
    return [Diagnostic({
        'code': 'UNKNOWN',
        'message': content,
    })]


class V2DiagnosticException(Exception):
  """Exceptions when an unexpected HTTP status is returned."""

  def __init__(self, resp, content):
    self._resp = resp
    self._diagnostics = _DiagnosticsFromContent(content)
    message = '\n'.join(
        ['response: %s' % resp] +
        ['%s: %s' % (d.message, d.detail) for d in self._diagnostics])
    super(V2DiagnosticException, self).__init__(message)

  @property
  def diagnostics(self):
    return self._diagnostics

  @property
  def response(self):
    return self._resp

  @property
  def status(self):
    return self._resp.status


class BadStateException(Exception):
  """Exceptions when we have entered an unexpected state."""


class TokenRefreshException(BadStateException):
  """Exception when token refresh fails."""


def _CheckState(predicate, message = None):
  if not predicate:
    raise BadStateException(message if message else 'Unknown')


_ANONYMOUS = ''
_BASIC = 'Basic'
_BEARER = 'Bearer'

_REALM_PFX = 'realm='
_SERVICE_PFX = 'service='


class Transport(object):
  """HTTP Transport abstraction to handle automatic v2 reauthentication.

  In the v2 Registry protocol, all of the API endpoints expect to receive
  'Bearer' authentication.  These Bearer tokens are generated by exchanging
  'Basic' or 'Anonymous' authentication with an authentication endpoint
  designated by the opening ping request.

  The Bearer tokens are scoped to a resource (typically repository), and
  are generated with a set of capabilities embedded (e.g. push, pull).

  The Docker client has a baked in 60-second expiration for Bearer tokens,
  and upon expiration, registries can reject any request with a 401.  The
  transport should automatically refresh the Bearer token and reissue the
  request.

  Args:
     name: the structured name of the docker resource being referenced.
     creds: the basic authentication credentials to use for authentication
            challenge exchanges.
     transport: the HTTP transport to use under the hood.
     action: One of docker_http.ACTIONS, for which we plan to use this transport
  """

  def __init__(self, name,
               creds,
               transport, action):
    self._name = name
    self._basic_creds = creds
    self._transport = transport
    self._action = action
    self._lock = threading.Lock()

    _CheckState(action in ACTIONS,
                'Invalid action supplied to docker_http.Transport: %s' % action)

    # Ping once to establish realm, and then get a good credential
    # for use with this transport.
    self._Ping()
    if self._authentication == _BEARER:
      self._Refresh()
    elif self._authentication == _BASIC:
      self._creds = self._basic_creds
    else:
      self._creds = docker_creds.Anonymous()

  def _Ping(self):
    """Ping the v2 Registry.

    Only called during transport construction, this pings the listed
    v2 registry.  The point of this ping is to establish the "realm"
    and "service" to use for Basic for Bearer-Token exchanges.
    """
    # This initiates the pull by issuing a v2 ping:
    #   GET H:P/v2/
    headers = {
        'content-type': 'application/json',
        'user-agent': docker_name.USER_AGENT,
    }
    resp, content = self._transport.request(
        '{scheme}://{registry}/v2/'.format(
            scheme=Scheme(self._name.registry), registry=self._name.registry),
        'GET',
        body=None,
        headers=headers)

    # We expect a www-authenticate challenge.
    _CheckState(
        resp.status in [
            six.moves.http_client.OK, six.moves.http_client.UNAUTHORIZED
        ], 'Unexpected response pinging the registry: {}\nBody: {}'.format(
            resp.status, content or '<empty>'))

    # The registry is authenticated iff we have an authentication challenge.
    if resp.status == six.moves.http_client.OK:
      self._authentication = _ANONYMOUS
      self._service = 'none'
      self._realm = 'none'
      return

    challenge = resp['www-authenticate']
    _CheckState(' ' in challenge,
                'Unexpected "www-authenticate" header form: %s' % challenge)

    (self._authentication, remainder) = challenge.split(' ', 1)

    # Normalize the authentication scheme to have exactly the first letter
    # capitalized. Scheme matching is required to be case insensitive:
    # https://tools.ietf.org/html/rfc7235#section-2.1
    self._authentication = self._authentication.capitalize()

    _CheckState(self._authentication in [_BASIC, _BEARER],
                'Unexpected "www-authenticate" challenge type: %s' %
                self._authentication)

    # Default "_service" to the registry
    self._service = self._name.registry

    tokens = remainder.split(',')
    for t in tokens:
      if t.startswith(_REALM_PFX):
        self._realm = t[len(_REALM_PFX):].strip('"')
      elif t.startswith(_SERVICE_PFX):
        self._service = t[len(_SERVICE_PFX):].strip('"')

    # Make sure these got set.
    _CheckState(self._realm, 'Expected a "%s" in "www-authenticate" '
                'header: %s' % (_REALM_PFX, challenge))

  def _Scope(self):
    """Construct the resource scope to pass to a v2 auth endpoint."""
    return self._name.scope(self._action)

  def _Refresh(self):
    """Refreshes the Bearer token credentials underlying this transport.

    This utilizes the "realm" and "service" established during _Ping to
    set up _creds with up-to-date credentials, by passing the
    client-provided _basic_creds to the authorization realm.

    This is generally called under two circumstances:
      1) When the transport is created (eagerly)
      2) When a request fails on a 401 Unauthorized

    Raises:
      TokenRefreshException: Error during token exchange.
    """
    headers = {
        'content-type': 'application/json',
        'user-agent': docker_name.USER_AGENT,
        'Authorization': self._basic_creds.Get()
    }
    parameters = {
        'scope': self._Scope(),
        'service': self._service,
    }
    resp, content = self._transport.request(
        # 'realm' includes scheme and path
        '{realm}?{query}'.format(
            realm=self._realm,
            query=six.moves.urllib.parse.urlencode(parameters)),
        'GET',
        body=None,
        headers=headers)

    if resp.status != six.moves.http_client.OK:
      raise TokenRefreshException('Bad status during token exchange: %d\n%s' %
                                  (resp.status, content))

    try:
      content = content.decode('utf8')
    except:  # pylint: disable=bare-except
      # Assume it's already decoded. Defensive coding for old py2 habits that
      # are hard to break. Passing does not make the problem worse.
      pass
    wrapper_object = json.loads(content)
    token = wrapper_object.get('token') or wrapper_object.get('access_token')
    _CheckState(token is not None, 'Malformed JSON response: %s' % content)

    with self._lock:
      # We have successfully reauthenticated.
      self._creds = v2_2_creds.Bearer(token)

  # pylint: disable=invalid-name
  def Request(self,
              url,
              accepted_codes = None,
              method = None,
              body = None,
              content_type = None,
              accepted_mimes = None
             ):
    """Wrapper containing much of the boilerplate REST logic for Registry calls.

    Args:
      url: the URL to which to talk
      accepted_codes: the list of acceptable http status codes
      method: the HTTP method to use (defaults to GET/PUT depending on
              whether body is provided)
      body: the body to pass into the PUT request (or None for GET)
      content_type: the mime-type of the request (or None for JSON).
              content_type is ignored when body is None.
      accepted_mimes: the list of acceptable mime-types

    Raises:
      BadStateException: an unexpected internal state has been encountered.
      V2DiagnosticException: an error has occurred interacting with v2.

    Returns:
      The response of the HTTP request, and its contents.
    """
    if not method:
      method = 'GET' if not body else 'PUT'

    # If the first request fails on a 401 Unauthorized, then refresh the
    # Bearer token and retry, if the authentication mode is bearer.
    for retry_unauthorized in [self._authentication == _BEARER, False]:
      # self._creds may be changed by self._Refresh(), so do
      # not hoist this.
      headers = {
          'user-agent': docker_name.USER_AGENT,
      }
      auth = self._creds.Get()
      if auth:
        headers['Authorization'] = auth

      if body:  # Requests w/ bodies should have content-type.
        headers['content-type'] = (
            content_type if content_type else 'application/json')

      if accepted_mimes is not None:
        headers['Accept'] = ','.join(accepted_mimes)

      # POST/PUT require a content-length, when no body is supplied.
      if method in ('POST', 'PUT') and not body:
        headers['content-length'] = '0'

      resp, content = self._transport.request(
          url, method, body=body, headers=headers)

      if (retry_unauthorized and
          resp.status == six.moves.http_client.UNAUTHORIZED):
        # On Unauthorized, refresh the credential and retry.
        self._Refresh()
        continue
      break

    if resp.status not in accepted_codes:
      # Use the content returned by GCR as the error message.
      raise V2DiagnosticException(resp, content)

    return resp, content

  def PaginatedRequest(self,
                       url,
                       accepted_codes = None,
                       method = None,
                       body = None,
                       content_type = None
                      ):
    """Wrapper around Request that follows Link headers if they exist.

    Args:
      url: the URL to which to talk
      accepted_codes: the list of acceptable http status codes
      method: the HTTP method to use (defaults to GET/PUT depending on
              whether body is provided)
      body: the body to pass into the PUT request (or None for GET)
      content_type: the mime-type of the request (or None for JSON)

    Yields:
      The return value of calling Request for each page of results.
    """
    next_page = url

    while next_page:
      resp, content = self.Request(next_page, accepted_codes, method, body,
                                   content_type)
      yield resp, content

      next_page = ParseNextLinkHeader(resp)


def ParseNextLinkHeader(resp):
  """Returns "next" link from RFC 5988 Link header or None if not present."""
  link = resp.get('link')
  if not link:
    return None

  m = re.match(r'.*<(.+)>;\s*rel="next".*', link)
  if not m:
    return None

  return m.group(1)


def Scheme(endpoint):
  """Returns https scheme for all the endpoints except localhost."""
  if endpoint.startswith('localhost:'):
    return 'http'
  elif endpoint.startswith('registry-docker-registry'):
    return 'http'
  elif re.match(r'.*\.local(?:host)?(?::\d{1,5})?$', endpoint):
    return 'http'
  else:
    return 'https'
