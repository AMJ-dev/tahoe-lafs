
__all__ = [
    "CLINodeAPI",
    "Expect",
    "on_stdout",
    "on_stdout_and_stderr",
    "wait_for_exit",
]

import os
import sys
from errno import ENOENT

import attr

from twisted.internet.error import (
    ProcessDone,
    ProcessTerminated,
)
from twisted.python.filepath import (
    FilePath,
)
from twisted.internet.protocol import (
    Protocol,
    ProcessProtocol,
)
from twisted.internet.defer import (
    Deferred,
    succeed,
)

from ..client import (
    _Client,
)
from ..scripts.tahoe_stop import (
    COULD_NOT_STOP,
)

class Expect(Protocol):
    def __init__(self):
        self._expectations = []

    def get_buffered_output(self):
        return self._buffer

    def expect(self, expectation):
        if expectation in self._buffer:
            return succeed(None)
        d = Deferred()
        self._expectations.append((expectation, d))
        return d

    def connectionMade(self):
        self._buffer = b""

    def dataReceived(self, data):
        self._buffer += data
        for i in range(len(self._expectations) - 1, -1, -1):
            expectation, d = self._expectations[i]
            if expectation in self._buffer:
                del self._expectations[i]
                d.callback(None)

    def connectionLost(self, reason):
        for ignored, d in self._expectations:
            d.errback(reason)


class _ProcessProtocolAdapter(ProcessProtocol):
    def __init__(self, stdout_protocol, fds):
        self._stdout_protocol = stdout_protocol
        self._fds = fds

    def connectionMade(self):
        self._stdout_protocol.makeConnection(self.transport)

    def childDataReceived(self, childFD, data):
        if childFD in self._fds:
            self._stdout_protocol.dataReceived(data)

    def processEnded(self, reason):
        self._stdout_protocol.connectionLost(reason)


def on_stdout(protocol):
    return _ProcessProtocolAdapter(protocol, {1})

def on_stdout_and_stderr(protocol):
    return _ProcessProtocolAdapter(protocol, {1, 2})


@attr.s
class CLINodeAPI(object):
    reactor = attr.ib()
    basedir = attr.ib(type=FilePath)

    @property
    def twistd_pid_file(self):
        return self.basedir.child(u"twistd.pid")

    @property
    def node_url_file(self):
        return self.basedir.child(u"node.url")

    @property
    def storage_furl_file(self):
        return self.basedir.child(u"private").child(u"storage.furl")

    @property
    def config_file(self):
        return self.basedir.child(u"tahoe.cfg")

    @property
    def exit_trigger_file(self):
        return self.basedir.child(_Client.EXIT_TRIGGER_FILE)

    def _execute(self, process_protocol, argv):
        exe = sys.executable
        argv = [
            exe,
            u"-m",
            u"allmydata.scripts.runner",
        ] + argv
        return self.reactor.spawnProcess(
            processProtocol=process_protocol,
            executable=exe,
            args=argv,
            env=os.environ,
        )

    def run(self, protocol):
        """
        Start the node running.

        :param ProcessProtocol protocol: This protocol will be hooked up to
            the node process and can handle output or generate input.
        """
        self.process = self._execute(
            protocol,
            [u"run", self.basedir.asTextMode().path],
        )
        # Don't let the process run away forever.
        try:
            self.active()
        except OSError as e:
            if ENOENT != e.errno:
                raise

    def stop(self, protocol):
        self._execute(
            protocol,
            [u"stop", self.basedir.asTextMode().path],
        )

    def stop_and_wait(self):
        protocol, ended = wait_for_exit()
        self.stop(protocol)
        return ended

    def active(self):
        # By writing this file, we get two minutes before the client will
        # exit. This ensures that even if the 'stop' command doesn't work (and
        # the test fails), the client should still terminate.
        self.exit_trigger_file.touch()

    def _check_cleanup_reason(self, reason):
        # Let it fail because the process has already exited.
        reason.trap(ProcessTerminated)
        if reason.value.exitCode != COULD_NOT_STOP:
            return reason
        return None

    def cleanup(self):
        stopping = self.stop_and_wait()
        stopping.addErrback(self._check_cleanup_reason)
        return stopping


class _WaitForEnd(ProcessProtocol):
    def __init__(self, ended):
        self._ended = ended

    def processEnded(self, reason):
        if reason.check(ProcessDone):
            self._ended.callback(None)
        else:
            self._ended.errback(reason)


def wait_for_exit():
    ended = Deferred()
    protocol = _WaitForEnd(ended)
    return protocol, ended
