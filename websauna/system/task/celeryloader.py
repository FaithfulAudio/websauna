"""Celery process."""

import os
import sys
import logging

from celery.loaders.base import BaseLoader
from celery.signals import setup_logging as _setup_logging_signal

import plaster
from websauna.system.devop.cmdline import init_websauna
from websauna.system.http.utils import make_routable_request
from websauna.system.model.retry import ensure_transactionless
from websauna.utils.config.includer import IncludeAwareConfigParser
from websauna.system.devop.cmdline import setup_logging


from .celery import parse_celery_config


logger = logging.getLogger(__name__)


#: Passed through Celery loader mechanism
ini_file = None


class WebsaunaLoader(BaseLoader):
    """Celery command line loader for Websauna.

    Support binding request object to Celery tasks and loading Celery settings through Pyramid INI configuration.
    """

    def get_celery_config(self, config_file: str) -> dict:
        """Return celery configuration, from given config_file"""
        ws_config = 'ws://{0}'.format(config_file)

        loader = plaster.get_loader(ws_config)
        settings = loader.get_settings('app:main')

        # TODO: We have ugly app:main hardcode hack here
        value = settings.get('websauna.celery_config')
        if not value:
            raise RuntimeError("Could not find websauna.celery_config in {}".format(ini_file))
        return value


    def read_configuration(self) -> dict:
        """Load Celery config from Pyramid INI file.

        We need to be able to do this without ramping up full Websauna, because that's the order of the evens Celery worker wants. This way we avoid circular dependencies during Celery worker start up.
        """
        value = self.get_celery_config(ini_file)
        config = parse_celery_config(value)
        return config

    def import_task_module(self, module):
        raise RuntimeError("imports Celery config directive is not supported. Use config.scan() to pick up tasks.")

    def register_tasks(self):
        """Inform Celery of all tasks registered through our Venusian-compatible task decorator."""

        # @task() decorator pick ups
        tasks = getattr(self.request.registry, "celery_tasks", [])

        for func, args, kwargs in tasks:
            decorator_args = [func] + list(args)
            self.app.task(*decorator_args, **kwargs)

    def on_worker_init(self):
        """This method is called when a child process starts."""

        # TODO Make sanity_check True by default,
        # but make it sure we can disable it in tests,
        # because otherwise tests won't run
        self.request = init_websauna(ini_file, sanity_check=False)

        #: Associate this process as Celery app object of the environment
        self.request.registry.celery = self.app

        #: Associate request for celery app, so
        #: task executor knows about Request object
        self.app.cmdline_request = self.request

        self.register_tasks()

    def on_task_init(self, task_id, task):
        """This method is called before a task is executed.

        Pass our request context to the task.

        http://docs.celeryproject.org/en/latest/userguide/tasks.html#context

        .. note ::

            The same request object is recycled over and over again. Pyramid does not have correctly mechanisms for having retryable request factory.

        """

        # TODO: How Celery handles retries?

        # We must not have on-going transaction when worker spawns a task
        # - otherwise it means init code has left transaction open
        ensure_transactionless("Thread local TX was ongoing when Celery fired up a new task {}: {}".format(task_id, task))

        # Each tasks gets a new request with its own transaction manager and dbsession
        request = make_routable_request(dbsession=None, registry=self.request.registry)

        task.request.update(request=request)


@_setup_logging_signal.connect
def fix_celery_logging(loglevel, logfile, format, colorize, **kwargs):
    """Fix Celery logging by re-enforcing our loggers after Celery messes up them."""
    setup_logging(ini_file)


def main():
    """Celery process entry point.

    Wrap celery command line script with our INI reader.

    .. note ::

        Make sure there is no global app = Celery() in any point of your code base,
        or this doesn't work.

    """
    global ini_file
    global request

    if len(sys.argv) < 2:
        sys.exit("Example usage: ws-celery myapp/conf/development.ini -- worker")

    ini_file = sys.argv[1]
    if not ini_file.endswith(".ini"):
        sys.exit("The first argument must be a configuration file")

    if len(sys.argv) >= 3:
        if not sys.argv[2] == "--":
            raise RuntimeError("The second argument must be -- to signal command line argument passthrough")
        celery_args = sys.argv[3:]
    else:
        celery_args = []

    # https://github.com/celery/celery/issues/3405
    os.environ["CELERY_LOADER"]  = "websauna.system.task.celeryloader.WebsaunaLoader"
    argv = ["celery"] + celery_args
    # Directly jump to Celery 4.0+ entry point
    from celery.bin.celery import main
    main(argv)


