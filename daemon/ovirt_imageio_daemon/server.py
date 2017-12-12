# ovirt-imageio-daemon
# Copyright (C) 2015-2017 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

from __future__ import absolute_import

import celery
import json
import logging
import logging.config
import os
import signal
import sys
import time

from wsgiref import simple_server

from six.moves import socketserver

import systemd.daemon
import webob

from webob.exc import (
    HTTPBadRequest,
    HTTPNotFound,
)

from ovirt_imageio_common import configloader
from ovirt_imageio_common import directio
from ovirt_imageio_common import errors
from ovirt_imageio_common import ssl
from ovirt_imageio_common import util
from ovirt_imageio_common import version
from ovirt_imageio_common import web

from . import config
from . import pki
from . import uhttp
from . import tickets

from . import celery_tasks

CONF_DIR = "/etc/ovirt-imageio-daemon"

log = logging.getLogger("server")
image_server = None
ticket_server = None
task_server = None
running = True


def main(args):
    configure_logger()
    try:
        log.info("Starting (pid=%s, version=%s)", os.getpid(), version.string)
        configloader.load(config, [os.path.join(CONF_DIR, "daemon.conf")])
        signal.signal(signal.SIGINT, terminate)
        signal.signal(signal.SIGTERM, terminate)
        start(config)
        try:
            systemd.daemon.notify("READY=1")
            log.info("Ready for requests")
            while running:
                time.sleep(30)
        finally:
            stop()
        log.info("Stopped")
    except Exception:
        log.exception(
            "Service failed (image_server=%s, ticket_server=%s, running=%s)",
            image_server, ticket_server, running)
        sys.exit(1)


def configure_logger():
    conf = os.path.join(CONF_DIR, "logger.conf")
    logging.config.fileConfig(conf, disable_existing_loggers=False)


def terminate(signo, frame):
    global running
    log.info("Received signal %d, shutting down", signo)
    running = False


def start(config):
    global image_server, ticket_server, task_server
    assert not (image_server or ticket_server or task_server)

    log.debug("Starting images service on port %d", config.images.port)
    image_server = ThreadedWSGIServer((config.images.host, config.images.port),
                                      WSGIRequestHandler)
    secure_server(config, image_server)

    image_app = web.Application(config, [(r"/images/(.*)", Images),
                                         (r"/tasks/(.*)", Tasks)])
    image_server.set_app(image_app)
    start_server(config, image_server, "image.server")

    log.debug("Starting tickets service on socket %s", config.tickets.socket)
    ticket_server = uhttp.UnixWSGIServer(config.tickets.socket,
                                         UnixWSGIRequestHandler)
    ticket_app = web.Application(config, [(r"/tickets/(.*)", Tickets)])
    ticket_server.set_app(ticket_app)
    start_server(config, ticket_server, "ticket.server")


def stop():
    global image_server, ticket_server
    log.debug("Stopping services")
    image_server.shutdown()
    ticket_server.shutdown()
    task_server.shutdown()
    image_server = None
    ticket_server = None


def secure_server(config, server):
    key_file = pki.key_file(config)
    cert_file = pki.cert_file(config)
    log.debug("Securing server (certfile=%s, keyfile=%s)", cert_file, key_file)
    context = ssl.server_context(cert_file, cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)


def start_server(config, server, name):
    log.debug("Starting thread %s", name)
    util.start_thread(server.serve_forever,
                      kwargs={"poll_interval": config.daemon.poll_interval},
                      name=name)


def response(status=200, payload=None):
    """
    Return WSGI application for sending response in JSON format.
    """
    body = json.dumps(payload) if payload else ""
    return webob.Response(status=status, body=body,
                          content_type="application/json")


class Images(object):
    """
    Request handler for the /images/ resource.
    """
    log = logging.getLogger("images")

    def __init__(self, config, request):
        self.config = config
        self.request = request

    def put(self, ticket_id):
        if not ticket_id:
            raise HTTPBadRequest("Ticket id is required")
        size = self.request.content_length
        if size is None:
            raise HTTPBadRequest("Content-Length header is required")
        if size < 0:
            raise HTTPBadRequest("Invalid Content-Length header: %r" % size)
        content_range = web.content_range(self.request)
        offset = content_range.start or 0
        ticket = tickets.authorize(ticket_id, "write", offset + size)
        # TODO: cancel copy if ticket expired or revoked
        self.log.info("Writing %d bytes at offset %d to %s for ticket %s",
                      size, offset, ticket.url.path, ticket_id)
        op = directio.Receive(ticket.url.path,
                              self.request.body_file_raw,
                              size,
                              offset=offset,
                              buffersize=self.config.daemon.buffer_size)
        ticket.add_operation(op)
        op.run()
        return response()

    def post(self, ticket_id):
        if not ticket_id:
            raise HTTPBadRequest("Ticket id is required")

        body = self.request.body
        methodargs = json.loads(body)
        if not 'backup_path' in methodargs:
            raise HTTPBadRequest("Malformed request. Requires backup_path in the body")

        destdir = os.path.split(methodargs['backup_path'])[0]
        if not os.path.exists(destdir):
            raise HTTPBadRequest("Backup_path does not exists")

        # TODO: cancel copy if ticket expired or revoked
        if methodargs['method'] == 'backup':
            ticket = tickets.authorize(ticket_id, "read", 0)
            self.log.debug("disk %s to %s for ticket %s",
                            body, ticket.url.path, ticket_id)

            ctask = celery_tasks.backup.delay(ticket_id,
                                              ticket.url.path,
                                              methodargs['backup_path'],
                                              tickets.get(ticket_id).size,
                                              self.config.daemon.buffer_size)
        elif methodargs['method'] == 'restore':
            ticket = tickets.authorize(ticket_id, "write", 0)
            self.log.debug("disk %s to %s for ticket %s",
                            body, ticket.url.path, ticket_id)

            ctask = celery_tasks.restore.delay(ticket_id,
                                               ticket.url.path,
                                               methodargs['backup_path'],
                                               tickets.get(ticket_id).size,
                                               self.config.daemon.buffer_size)
        else:
            raise HTTPBadRequest("Invalid method")

        r = response(status=206)
        r.headers["Location"] = "/tasks/%s" % ctask.id
        return r

    def get(self, ticket_id):
        # TODO: cancel copy if ticket expired or revoked
        if not ticket_id:
            raise HTTPBadRequest("Ticket id is required")
        # TODO: support partial range (e.g. bytes=0-*)
        if self.request.range:
            offset = self.request.range.start
            if self.request.range.end is None:
                size = tickets.get(ticket_id).size - offset
            else:
                size = self.request.range.end - offset
            status = 206
        else:
            offset = 0
            size = tickets.get(ticket_id).size
            status = 200

        ticket = tickets.authorize(ticket_id, "read", offset + size)
        self.log.info("Reading %d bytes at offset %d from %s for ticket %s",
                      size, offset, ticket.url.path, ticket_id)
        op = directio.Send(ticket.url.path,
                           None,
                           size,
                           offset=offset,
                           buffersize=self.config.daemon.buffer_size)

        filename = os.path.split(ticket.url.path)[1]
        with open(os.path.join("/tmp", filename), "a+") as f:
            f.seek(offset)
            for data in op:
                f.write(data)
        op = directio.Send(ticket.url.path,
                           None,
                           size,
                           offset=offset,
                           buffersize=self.config.daemon.buffer_size)
        ticket.add_operation(op)
        content_disposition = "attachment"
        if ticket.filename:
            filename = ticket.filename.encode("utf-8")
            content_disposition += "; filename=%s" % filename

        resp = webob.Response(
            status=status,
            app_iter=op,
            content_type="application/octet-stream",
            content_length=str(size),
            content_disposition=content_disposition,
        )
        if self.request.range:
            content_range = self.request.range.content_range(size)
            resp.headers["content_range"] = str(content_range)

        return resp


class Tickets(object):
    """
    Request handler for the /tickets/ resource.
    """
    log = logging.getLogger("tickets")

    def __init__(self, config, request):
        self.config = config
        self.request = request

    def get(self, ticket_id):
        if not ticket_id:
            raise HTTPBadRequest("Ticket id is required")
        try:
            ticket = tickets.get(ticket_id)
        except KeyError:
            raise HTTPNotFound("No such ticket %r" % ticket_id)
        self.log.info("Retrieving ticket %s", ticket_id)
        return response(payload=ticket.info())

    def put(self, ticket_id):
        # TODO
        # - reject invalid or expired ticket
        # - start expire timer
        if not ticket_id:
            raise HTTPBadRequest("Ticket id is required")

        try:
            ticket_dict = self.request.json
        except ValueError as e:
            raise HTTPBadRequest("Ticket is not in a json format: %s" % e)

        try:
            ticket = tickets.Ticket(ticket_dict)
        except errors.InvalidTicket as e:
            raise HTTPBadRequest("Invalid ticket: %s" % e)

        tickets.add(ticket_id, ticket)
        return response()

    def patch(self, ticket_id):
        # TODO: restart expire timer
        if not ticket_id:
            raise HTTPBadRequest("Ticket id is required")
        try:
            patch = self.request.json
        except ValueError as e:
            raise HTTPBadRequest("Invalid patch: %s" % e)
        try:
            timeout = patch["timeout"]
        except KeyError:
            raise HTTPBadRequest("Missing timeout key")
        try:
            timeout = int(timeout)
        except ValueError as e:
            raise HTTPBadRequest("Invalid timeout value: %s" % e)
        try:
            ticket = tickets.get(ticket_id)
        except KeyError:
            raise HTTPNotFound("No such ticket: %s" % ticket_id)
        ticket.extend(timeout)
        return response()

    def delete(self, ticket_id):
        # TODO: cancel requests using deleted tickets
        if ticket_id:
            try:
                tickets.remove(ticket_id)
            except KeyError:
                raise HTTPNotFound("No such ticket %r" % ticket_id)
        else:
            tickets.clear()
        return response(status=204)


class Tasks(object):
    """
    Request handler for the /tasks/ resource.
    """
    log = logging.getLogger("tasks")

    def __init__(self, config, request):
        self.config = config
        self.request = request

    def get(self, task_id):
        if not task_id:
            raise HTTPBadRequest("Task id is required")

        status = 200

        self.log.info("Retrieving task %s", task_id)
        try:
            ctasks = celery.result.AsyncResult(task_id, app=celery_tasks.app)
        except KeyError:
            raise HTTPNotFound("No such task %r" % task_id)

        result = ctasks.result

        if not result:
            result = {}

        if isinstance(result, Exception):
            result = {'Exception': result.message}

        result['status'] = ctasks.status

        return response(payload=result)


class ThreadedWSGIServer(socketserver.ThreadingMixIn,
                         simple_server.WSGIServer):
    """
    Threaded WSGI HTTP server.
    """
    daemon_threads = True


class WSGIRequestHandler(simple_server.WSGIRequestHandler):
    """
    WSGI request handler using HTTP/1.1.
    """

    protocol_version = "HTTP/1.1"

    def address_string(self):
        """
        Override to avoid slow and unneeded name lookup.
        """
        return self.client_address[0]

    def handle(self):
        """
        Override to use fixed ServerHandler.

        Copied from wsgiref/simple_server.py, using our ServerHandler.
        """
        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
            self.requestline = ''
            self.request_version = ''
            self.command = ''
            self.send_error(414)
            return

        if not self.parse_request():  # An error code has been sent, just exit
            return

        handler = ServerHandler(
            self.rfile, self.wfile, self.get_stderr(), self.get_environ()
        )
        handler.request_handler = self      # backpointer for logging
        handler.run(self.server.get_app())

    def log_message(self, format, *args):
        """
        Override to avoid unwanted logging to stderr.
        """


class ServerHandler(simple_server.ServerHandler):

    # wsgiref handers ignores the http request handler's protocol_version, and
    # uses its own version. This results in requests returning HTTP/1.0 instead
    # of HTTP/1.1 - see https://bugzilla.redhat.com/1512317
    #
    # Looking at python source we need to define here:
    #
    #   http_version = "1.1"
    #
    # Bug adding this break some tests.
    # TODO: investigate this.

    def write(self, data):
        """
        Override to allow writing buffer object.

        Copied from wsgiref/handlers.py, removing the check for StringType.
        """
        if not self.status:
            raise AssertionError("write() before start_response()")

        elif not self.headers_sent:
            # Before the first output, send the stored headers
            self.bytes_sent = len(data)    # make sure we know content-length
            self.send_headers()
        else:
            self.bytes_sent += len(data)

        self._write(data)
        self._flush()


class UnixWSGIRequestHandler(uhttp.UnixWSGIRequestHandler):
    """
    WSGI over unix domain socket request handler using HTTP/1.1.
    """
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        """
        Override to avoid unwanted logging to stderr.
        """
