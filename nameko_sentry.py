import logging
import re
from abc import ABCMeta
from collections import defaultdict
from threading import local

import six
from nameko.extensions import DependencyProvider
from nameko.web.handlers import HttpRequestHandler
from raven import Client, breadcrumbs
from raven.context import Context as RavenContext
from raven.utils.wsgi import get_environ, get_headers
from six.moves.urllib.parse import urlsplit  # pylint: disable=E0401
from werkzeug.exceptions import ClientDisconnected

USER_TYPE_CONTEXT_KEYS = (
    re.compile("user|email|session"),
)
TAG_TYPE_CONTEXT_KEYS = (
    re.compile("call_id$"),
)


class RemoveLocalMeta(ABCMeta, type):
    """ Metaclass that *removes* the thread-local base from our `RavenContext`
    subclass.

    This avoids us having to reimplement the Context object ourselves, and
    means any breadcrumbs emitted via the `raven.breadcrumbs.capture` helper
    in the worker thread are captured.
    """
    def mro(cls):
        chain = [cls]
        chain.extend(
            # pylint: disable=E1101
            base for base in RavenContext.mro() if base not in local.mro()
        )
        chain.append(object)
        return tuple(chain)


class SentryReporter(DependencyProvider):
    """ Send exceptions generated by entrypoints to a sentry server.
    """

    @six.add_metaclass(RemoveLocalMeta)
    class Context(RavenContext):
        pass

    def setup(self):
        sentry_config = self.container.config.get('SENTRY')

        dsn = sentry_config['DSN']
        kwargs = sentry_config.get('CLIENT_CONFIG', {})
        self.client = Client(dsn, **kwargs)

        report_expected_exceptions = sentry_config.get(
            'REPORT_EXPECTED_EXCEPTIONS', True
        )
        user_type_context_keys = sentry_config.get(
            'USER_TYPE_CONTEXT_KEYS', USER_TYPE_CONTEXT_KEYS
        )
        tag_type_context_keys = sentry_config.get(
            'TAG_TYPE_CONTEXT_KEYS', TAG_TYPE_CONTEXT_KEYS
        )

        self.report_expected_exceptions = report_expected_exceptions
        self.user_type_context_keys = user_type_context_keys
        self.tag_type_context_keys = tag_type_context_keys

        # TODO better to put this at the module level
        self.contexts = defaultdict(self.Context)

    def format_message(self, worker_ctx, exc_info):
        exc_type, exc, _ = exc_info
        return (
            'Unhandled exception in call {}: '
            '{} {!r}'.format(worker_ctx.call_id, exc_type.__name__, str(exc))
        )

    def is_expected_exception(self, worker_ctx, exc_info):
        _, exc, _ = exc_info
        expected_exceptions = getattr(
            worker_ctx.entrypoint, 'expected_exceptions', tuple())
        return isinstance(exc, expected_exceptions)

    def get_raven_context(self, worker_ctx):
        return self.contexts[worker_ctx]

    def get_dependency(self, worker_ctx):
        """ Return `context` for worker to use
        """
        return self.get_raven_context(worker_ctx)

    def http_context(self, worker_ctx):
        """ Attempt to extract HTTP context if an HTTP entrypoint was used.
        """
        http = {}
        if isinstance(worker_ctx.entrypoint, HttpRequestHandler):
            try:
                request = worker_ctx.args[0]
                try:
                    if request.mimetype == 'application/json':
                        data = request.data
                    else:
                        data = request.form
                except ClientDisconnected:
                    data = {}

                urlparts = urlsplit(request.url)
                http.update({
                    'url': '{}://{}{}'.format(
                        urlparts.scheme, urlparts.netloc, urlparts.path
                    ),
                    'query_string': urlparts.query,
                    'method': request.method,
                    'data': data,
                    'headers': dict(get_headers(request.environ)),
                    'env': dict(get_environ(request.environ)),
                })
            except:
                pass  # probably not a compatible entrypoint

        self.get_raven_context(worker_ctx).merge({"request": http})

    def user_context(self, worker_ctx, exc_info):
        """ Merge any user context to include in the sentry payload.

        Extracts user identifiers from the worker context data by matching
        context keys with
        """
        user = {}
        for key in worker_ctx.context_data:
            for matcher in self.user_type_context_keys:
                if re.search(matcher, key):
                    user[key] = worker_ctx.context_data[key]
                    break

        self.get_raven_context(worker_ctx).merge({"user": user})

    def tags_context(self, worker_ctx, exc_info):
        """ Merge any tags to include in the sentry payload.
        """
        tags = {
            'call_id': worker_ctx.call_id,
            'parent_call_id': worker_ctx.immediate_parent_call_id,
            'service_name': worker_ctx.container.service_name,
            'method_name': worker_ctx.entrypoint.method_name
        }
        for key in worker_ctx.context_data:
            for matcher in self.tag_type_context_keys:
                if re.search(matcher, key):
                    tags[key] = worker_ctx.context_data[key]
                    break

        self.get_raven_context(worker_ctx).merge({"tags": tags})

    def extra_context(self, worker_ctx, exc_info):
        """ Merge any extra context to include in the sentry payload.

        Includes all available worker context data.
        """
        extra = {}
        extra.update(worker_ctx.context_data)

        self.get_raven_context(worker_ctx).merge({"extra": extra})

    def worker_setup(self, worker_ctx):
        self.http_context(worker_ctx)

    def worker_result(self, worker_ctx, result, exc_info):
        if exc_info is None:
            return

        self.user_context(worker_ctx, exc_info)
        self.tags_context(worker_ctx, exc_info)
        self.extra_context(worker_ctx, exc_info)

        self.capture_exception(worker_ctx, exc_info)

    def worker_teardown(self, worker_ctx):
        del self.contexts[worker_ctx]

    def capture_exception(self, worker_ctx, exc_info):

        message = self.format_message(worker_ctx, exc_info)

        logger = '{}.{}'.format(
            worker_ctx.service_name, worker_ctx.entrypoint.method_name
        )

        if self.is_expected_exception(worker_ctx, exc_info):
            if not self.report_expected_exceptions:
                return  # nothing to do
            level = logging.WARNING
        else:
            level = logging.ERROR

        data = {
            'logger': logger,
            'level': level
        }

        raven_context = self.get_raven_context(worker_ctx)

        self.client.context.merge(raven_context)
        self.client.context.merge({
            'breadcrumbs': {
                'values': raven_context.breadcrumbs.get_buffer()
            }
        })
        self.client.captureException(exc_info, message=message, data=data)
