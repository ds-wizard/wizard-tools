from __future__ import annotations

import contextlib
import logging
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import typing

from . import consts
from .documents import FileFormat, FileFormats
from .exceptions import require


if typing.TYPE_CHECKING:
    from .settings import PandocSettings


LOG = logging.getLogger(__name__)


#: how much of the end of stderr goes into the error message
STDERR_LIMIT = 64 * 1024


def run_conversion(*, args: list, workdir: str, input_data: bytes, name: str,
                   source_format: FileFormat, target_format: FileFormat, timeout=None) -> bytes:
    command = ' '.join(args)
    LOG.info('Calling "%s" to convert from %s to %s',
             command, source_format, target_format)
    # output goes to temporary files (deleted on close), not pipes, so nothing
    # blocks on a full pipe and stderr is read only up to STDERR_LIMIT
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            # own process group (POSIX), so a timeout stops also the filters it started
            proc = subprocess.Popen(args, cwd=workdir, stdin=subprocess.PIPE,
                                    stdout=stdout, stderr=stderr,
                                    start_new_session=True)
        except FileNotFoundError as e:
            raise FormatConversionError(
                name, source_format, target_format,
                f'Executable "{args[0]}" not found, is it installed?',
            ) from e
        try:
            proc.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            _kill(proc)
            raise FormatConversionError(
                name, source_format, target_format,
                f'Exceeded the time limit of {timeout} seconds and was stopped',
            ) from e
        except BaseException:
            _kill(proc)
            raise
        if proc.returncode != consts.EXIT_SUCCESS:
            raise FormatConversionError(
                name, source_format, target_format,
                f'Failed to execute (exit code: {proc.returncode}): '
                f'{_read_tail(stderr, STDERR_LIMIT).decode(consts.DEFAULT_ENCODING, "replace")}',
            )
        stdout.seek(0)
        return stdout.read()


def _kill(proc: subprocess.Popen):
    """Kill the process with everything it started, and reap it."""
    if sys.platform == 'win32':
        proc.kill()
    else:
        # the group is still there, the leader has not been reaped yet
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    if proc.stdin:
        with contextlib.suppress(OSError):
            proc.stdin.close()
    proc.wait()


def _read_tail(file: typing.BinaryIO, limit: int) -> bytes:
    size = file.seek(0, os.SEEK_END)
    file.seek(max(0, size - limit))
    return file.read()


class FormatConversionError(Exception):

    def __init__(self, convertor, source_format, target_format, message):
        self.convertor = convertor
        self.source_format = source_format
        self.target_format = target_format
        self.message = message

    def __str__(self):
        return f'{self.convertor} failed to convert {self.source_format}' \
               f' to {self.target_format} - {self.message}'


class Pandoc:

    def __init__(self, settings: PandocSettings, filter_names: list[str],
                 template_name: str | None):
        self.settings = settings
        self.filter_names = filter_names
        self.template_name = template_name
        self._check_filters()
        self._check_template()

    def _check_filters(self):
        for name in self.filter_names:
            if self.settings.filter_path(name) is None:
                raise RuntimeError(f'Pandoc filter "{name}" not found')

    def _check_template(self):
        if self.template_name and self.settings.template_path(self.template_name) is None:
            raise RuntimeError(f'Pandoc template "{self.template_name}" not found')

    def _extra_args(self) -> list[str]:
        # paths are passed as they are (not re-split), they may contain spaces
        args: list[str] = []
        if self.template_name:
            args.extend(['--template', str(self.settings.template_path(self.template_name))])
        for filter_name in self.filter_names:
            option = '--lua-filter' if filter_name.endswith('.lua') else '--filter'
            args.extend([option, str(self.settings.filter_path(filter_name))])
        return args

    def __call__(self, *, source_format: FileFormat, target_format: FileFormat,
                 data: bytes, metadata: dict, workdir: str) -> bytes:
        args = ['-f', source_format.name, '-t', target_format.name, '-o', '-']
        template_args = self.extract_template_args(metadata)
        extra_args = self._extra_args()
        command = self.settings.command + template_args + extra_args + args
        return run_conversion(
            args=command,
            workdir=workdir,
            input_data=data,
            name=type(self).__name__,
            source_format=source_format,
            target_format=target_format,
            timeout=self.settings.timeout,
        )

    @staticmethod
    def extract_template_args(metadata: dict):
        return shlex.split(metadata.get('args', ''))


class RdfLibConvert:

    FORMATS = {
        FileFormats.RDF_XML: 'xml',
        FileFormats.N3: 'n3',
        FileFormats.NTRIPLES: 'ntriples',
        FileFormats.TURTLE: 'turtle',
        FileFormats.TRIG: 'trig',
        FileFormats.JSONLD: 'json-ld',
    }

    def __init__(self):
        self.rdflib = require('rdflib', 'rdf')

    def __call__(self, *, source_format: FileFormat, target_format: FileFormat,
                 data: bytes, metadata: dict) -> bytes:
        g = self.rdflib.Dataset()
        g.parse(
            data=data.decode(consts.DEFAULT_ENCODING),
            format=self.FORMATS.get(source_format) or 'turtle',
        )
        return g.serialize(
            format=self.FORMATS.get(target_format) or 'turtle',
            encoding=consts.DEFAULT_ENCODING,
        )
