import logging
import re

from nameko.extensions import DependencyProvider
from nameko.web.handlers import HttpRequestHandler
from raven import Client
from raven.utils.wsgi import get_environ, get_headers
from six.moves.urllib.parse import urlsplit  # pylint: disable=E0401
from werkzeug.exceptions import ClientDisconnected

USER_TYPE_CONTEXT_KEYS = (
    re.compile("user|email|session"),
)
TAG_TYPE_CONTEXT_KEYS = (
    re.compile("call_id$"),
)


class SentryReporter(DependencyProvider):
    """ Send exceptions generated by entrypoints to a sentry server.
    """

    def setup(self):
        sentry_config = self.container.config.get('SENTRY')

        sentry_config = sentry_config or {}
        dsn = sentry_config.get('DSN', None)
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

    def get_dependency(self, worker_ctx):
        """ Expose the Raven Client directly to the worker
        """
        return self.client

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

        self.client.http_context(http)

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

        self.client.user_context(user)

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

        self.client.tags_context(tags)

    def extra_context(self, worker_ctx, exc_info):
        """ Merge any extra context to include in the sentry payload.

        Includes all available worker context data.
        """
        extra = {}
        extra.update(worker_ctx.context_data)

        self.client.extra_context(extra)

    def worker_setup(self, worker_ctx):
        self.http_context(worker_ctx)

    def worker_result(self, worker_ctx, result, exc_info):
        if exc_info is None:
            return

        self.user_context(worker_ctx, exc_info)
        self.tags_context(worker_ctx, exc_info)
        self.extra_context(worker_ctx, exc_info)

        self.capture_exception(worker_ctx, exc_info)

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

        self.client.captureException(exc_info, message=message, data=data)
