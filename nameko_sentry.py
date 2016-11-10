import logging

from nameko.extensions import DependencyProvider
from raven import Client


class SentryReporter(DependencyProvider):
    """ Send exceptions generated by entrypoints to a sentry server.
    """
    def setup(self):
        sentry_config = self.container.config.get('SENTRY')

        dsn = sentry_config['DSN']
        kwargs = sentry_config.get('CLIENT_CONFIG', {})
        report_expected_exceptions = sentry_config.get(
            'REPORT_EXPECTED_EXCEPTIONS', True
        )

        self.client = Client(dsn, **kwargs)
        self.report_expected_exceptions = report_expected_exceptions

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

    def build_tags(self, worker_ctx, exc_info):
        return {
            'call_id': worker_ctx.call_id,
            'parent_call_id': worker_ctx.immediate_parent_call_id,
        }

    def build_extra(self, worker_ctx, exc_info):
        _, exc, _ = exc_info
        return {
            'exc': exc
        }

    def worker_result(self, worker_ctx, result, exc_info):
        if exc_info is None:
            return
        self.capture_exception(worker_ctx, exc_info)

    def capture_exception(self, worker_ctx, exc_info):

        logger = '{}.{}'.format(
            worker_ctx.service_name, worker_ctx.entrypoint.method_name
        )

        if self.is_expected_exception(worker_ctx, exc_info):
            if not self.report_expected_exceptions:
                return  # nothing to do
            level = logging.WARNING
        else:
            level = logging.ERROR

        message = self.format_message(worker_ctx, exc_info)
        extra = self.build_extra(worker_ctx, exc_info)
        tags = self.build_tags(worker_ctx, exc_info)

        data = {
            'logger': logger,
            'level': level,
            'message': message,
            'tags': tags
        }

        self.client.captureException(
            exc_info, message=message, extra=extra, data=data
        )
