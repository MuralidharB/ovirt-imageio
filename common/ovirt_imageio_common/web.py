# ovirt-imageio-daemon
# Copyright (C) 2015-2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

from __future__ import absolute_import

import json
import logging
import re

from six.moves import http_client

import webob

from webob.exc import (
    HTTPBadRequest,
    HTTPException,
    HTTPMethodNotAllowed,
    HTTPNotFound,
    HTTPInternalServerError,
)

from . import util

log = logging.getLogger("web")


class RequestInfo(object):

    def __init__(self, r):
        self.r = r
        # Save request.path - modified during dispatching the request.
        self.path = r.path
        self.start = util.monotonic_time()

    def time(self):
        return util.monotonic_time() - self.start

    def __str__(self):
        return "[%s] %s %s" % (self.r.client_addr, self.r.method, self.path)


class Application(object):
    ALLOWED_METHODS = frozenset(['GET', 'PUT', 'PATCH', 'POST',
                                 'DELETE', 'OPTIONS', 'HEAD'])
    """
    WSGI application dispatching requests based on path and method to request
    handlers.
    """

    def __init__(self, config, routes):
        self.config = config
        self.routes = [(re.compile(pattern), cls) for pattern, cls in routes]

    def __call__(self, env, start_response):
        request = webob.Request(env)
        req = RequestInfo(request)
        self.log_start(req)
        try:
            resp = self.dispatch(request)
        except Exception as e:
            if not isinstance(e, HTTPException):
                e = HTTPInternalServerError(detail=str(e))
            resp = error_response(e)
            self.log_error(req, resp, e)
        else:
            self.log_finish(req, resp)
        return resp(env, start_response)

    def dispatch(self, request):
        if request.method not in self.ALLOWED_METHODS:
            raise HTTPMethodNotAllowed("Invalid method %r" %
                                       request.method)
        path = request.path_info
        for route, handler_class in self.routes:
            match = route.match(path)
            if match:
                handler = handler_class(self.config, request)
                try:
                    method = getattr(handler, request.method.lower())
                except AttributeError:
                    raise HTTPMethodNotAllowed(
                        "Method %r not defined for %r" %
                        (request.method, path))
                else:
                    request.path_info_pop()
                    return method(*match.groups())
        raise HTTPNotFound("No handler for %r" % path)

    def log_start(self, req):
        log.info("START %s", req)

    def log_finish(self, req, resp):
        log.info("FINISH %s: [%s] %s (%.2fs)",
                 req,
                 resp.status_code,
                 resp.content_length,
                 req.time())

    def log_error(self, req, resp, error):
        # Show exceptions only for internal errors (bugs in proxy), and warn
        # about anthing else (client error).
        meth = log.exception if resp.status_code >= 500 else log.warning
        meth("ERROR %s: [%s] %s (%.2fs)",
             req,
             resp.status_code,
             error,
             req.time())


def error_response(e):
    """
    Return WSGI application for sending error response using JSON format.
    """
    payload = {
        "code": e.code,
        "title": e.title,
        "explanation": e.explanation
    }
    if e.detail:
        payload["detail"] = e.detail
    return response(status=e.code, payload=payload)


def response(status=http_client.OK, payload=None):
    """
    Return WSGI application for sending response in JSON format.
    """
    body = json.dumps(payload) if payload else ""
    return webob.Response(status=status, body=body,
                          content_type="application/json")


def content_range(request):
    """
    Helper for parsing Content-Range header in request.

    WebOb support parsing of Content-Range header, but do not expose this
    header in webob.Request.
    """
    try:
        header = request.headers["content-range"]
    except KeyError:
        return webob.byterange.ContentRange(None, None, None)
    content_range = webob.byterange.ContentRange.parse(header)
    if content_range is None:
        raise HTTPBadRequest("Invalid content-range: %r" % header)
    return content_range


class CappedStream(object):
    """
    Stream limiting the amount of read data.

    This is a readonly file-like object limiting the amount of data read from
    the underlying stream. This is required when using http pipelining, and
    useful to avoid resurces exhaustion.

    The read() method will return empty string once the max_bytes bytes was
    read from the stream.

    Provides __iter__() method to make requests.Request use streaming.
    """

    def __init__(self, input_stream, max_bytes, buffer_size=1024**2):
        """
        Initialize a CappedStream.

        Arguments:
          input_stream (reader): An object implemneting read().
          max_bytes (int): maximum number of bytes to read from input_stream
          buffer_size (int): maximum number of bytes read will return
        """
        self.input_stream = input_stream
        self.max_bytes = max_bytes
        self.buffer_size = buffer_size
        self.bytes_read = 0

    def __iter__(self):
        while True:
            chunk = self.read(self.buffer_size)
            if not chunk:
                return
            yield chunk

    def read(self, size=None):
        if size is None:
            size = self.buffer_size
        to_read = min(size, self.max_bytes - self.bytes_read)
        chunk = self.input_stream.read(to_read)
        self.bytes_read += len(chunk)
        return chunk
