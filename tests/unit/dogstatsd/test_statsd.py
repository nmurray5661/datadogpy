# -*- coding: utf-8 -*-

# Unless explicitly stated otherwise all files in this repository are licensed under the BSD-3-Clause License.
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2015-Present Datadog, Inc
"""
Tests for dogstatsd.py
"""
# Standard libs
from collections import deque
import os
import socket
import errno

import time
import unittest

# Third-party libs
import mock
from mock import call, mock_open, patch
import pytest

# Datadog libs
from datadog import initialize, statsd
from datadog import __version__ as version
from datadog.dogstatsd.base import DogStatsd, UDP_OPTIMAL_PAYLOAD_LENGTH
from datadog.dogstatsd.context import TimedContextManagerDecorator
from datadog.util.compat import is_higher_py35, is_p3k
from tests.util.contextmanagers import preserve_environment_variable, EnvVars
from tests.unit.dogstatsd.fixtures import load_fixtures


class FakeSocket(object):
    """ A fake socket for testing. """

    def __init__(self):
        self.payloads = deque()

    def send(self, payload):
        if is_p3k():
            assert type(payload) == bytes
        else:
            assert type(payload) == str
        self.payloads.append(payload)

    def recv(self, n = 1):
        try:
            out = []
            for i in range(n):
                out.append(self.payloads.popleft().decode('utf-8'))
            return '\n'.join(out)
        except IndexError:
            return None

    def close(self):
        pass

    def __repr__(self):
        return str(self.payloads)


class BrokenSocket(FakeSocket):

    def send(self, payload):
        raise socket.error("Socket error")


class OverflownSocket(FakeSocket):

    def send(self, payload):
        error = socket.error("Socket error")
        error.errno = errno.EAGAIN
        raise error


def telemetry_metrics(metrics=1, events=0, service_checks=0, bytes_sent=0, bytes_dropped=0, packets_sent=1, packets_dropped=0, transport="udp", tags=""):
    tags = "," + tags if tags else ""

    return "\n".join([
        "datadog.dogstatsd.client.metrics:{}|c|#client:py,client_version:{},client_transport:{}{}".format(metrics, version, transport, tags),
        "datadog.dogstatsd.client.events:{}|c|#client:py,client_version:{},client_transport:{}{}".format(events, version, transport, tags),
        "datadog.dogstatsd.client.service_checks:{}|c|#client:py,client_version:{},client_transport:{}{}".format(service_checks, version, transport, tags),
        "datadog.dogstatsd.client.bytes_sent:{}|c|#client:py,client_version:{},client_transport:{}{}".format(bytes_sent, version, transport, tags),
        "datadog.dogstatsd.client.bytes_dropped:{}|c|#client:py,client_version:{},client_transport:{}{}".format(bytes_dropped, version, transport, tags),
        "datadog.dogstatsd.client.packets_sent:{}|c|#client:py,client_version:{},client_transport:{}{}".format(packets_sent, version, transport, tags),
        "datadog.dogstatsd.client.packets_dropped:{}|c|#client:py,client_version:{},client_transport:{}{}".format(packets_dropped, version, transport, tags)
    ])


class TestDogStatsd(unittest.TestCase):
    def setUp(self):
        """
        Set up a default Dogstatsd instance and mock the proc filesystem.
        """
        #
        self.statsd = DogStatsd(telemetry_min_flush_interval=0)
        self.statsd.socket = FakeSocket()

        # Mock the proc filesystem
        route_data = load_fixtures('route')
        self._procfs_mock = patch('datadog.util.compat.builtins.open', mock_open())
        self._procfs_mock.start().return_value.readlines.return_value = route_data.split("\n")

    #def setup_method(self, method):
    #    self.statsd._reset_telemetry()

    def tearDown(self):
        """
        Unmock the proc filesystem.
        """
        self._procfs_mock.stop()

    def assertEqualTelemetry(self, expected_payload, actual_payload, telemetry=None):
        if telemetry is None:
            telemetry = telemetry_metrics(bytes_sent=len(expected_payload))

        if expected_payload:
            expected_payload = "\n".join([expected_payload, telemetry])
        else:
            expected_payload = telemetry

        return self.assertEqual(expected_payload, actual_payload)

    def assertAlmostEqual(self, val1, val2, delta):
        assert 0 <= abs(val1 - val2) <= delta, "%s - %s not within %s" % (val1, val2, delta)

    def recv(self, n=1):
        packets = []
        for _ in range(n):
            packets.append(self.statsd.socket.recv())
        return "\n".join(packets)

    def test_initialization(self):
        """
        `initialize` overrides `statsd` default instance attributes.
        """
        options = {
            'statsd_host': "myhost",
            'statsd_port': 1234
        }

        # Default values
        self.assertEqual(statsd.host, "localhost")
        self.assertEqual(statsd.port, 8125)

        # After initialization
        initialize(**options)
        self.assertEqual(statsd.host, "myhost")
        self.assertEqual(statsd.port, 1234)

        # Add namespace
        options['statsd_namespace'] = "mynamespace"
        initialize(**options)
        self.assertEqual(statsd.host, "myhost")
        self.assertEqual(statsd.port, 1234)
        self.assertEqual(statsd.namespace, "mynamespace")

        # Set `statsd` host to the system's default route
        initialize(statsd_use_default_route=True, **options)
        self.assertEqual(statsd.host, "172.17.0.1")
        self.assertEqual(statsd.port, 1234)

        # Add UNIX socket
        options['statsd_socket_path'] = '/var/run/dogstatsd.sock'
        initialize(**options)
        self.assertEqual(statsd.socket_path, options['statsd_socket_path'])
        self.assertIsNone(statsd.host)
        self.assertIsNone(statsd.port)

    def test_dogstatsd_initialization_with_env_vars(self):
        """
        Dogstatsd can retrieve its config from env vars when
        not provided in constructor.
        """
        # Setup
        with preserve_environment_variable('DD_AGENT_HOST'):
            os.environ['DD_AGENT_HOST'] = 'myenvvarhost'
            with preserve_environment_variable('DD_DOGSTATSD_PORT'):
                os.environ['DD_DOGSTATSD_PORT'] = '4321'
                statsd = DogStatsd()

        # Assert
        self.assertEqual(statsd.host, "myenvvarhost")
        self.assertEqual(statsd.port, 4321)

    def test_default_route(self):
        """
        Dogstatsd host can be dynamically set to the default route.
        """
        # Setup
        statsd = DogStatsd(use_default_route=True)

        # Assert
        self.assertEqual(statsd.host, "172.17.0.1")

    def test_set(self):
        self.statsd.set('set', 123)
        self.assertEqualTelemetry('set:123|s', self.recv(2))

    def test_gauge(self):
        self.statsd.gauge('gauge', 123.4)
        self.assertEqualTelemetry('gauge:123.4|g', self.recv(2))

    def test_counter(self):
        self.statsd.increment('page.views')
        self.assertEqualTelemetry('page.views:1|c', self.recv(2))

        self.statsd._reset_telemetry()
        self.statsd.increment('page.views', 11)
        self.assertEqualTelemetry('page.views:11|c', self.recv(2))

        self.statsd._reset_telemetry()
        self.statsd.decrement('page.views')
        self.assertEqualTelemetry('page.views:-1|c', self.recv(2))

        self.statsd._reset_telemetry()
        self.statsd.decrement('page.views', 12)
        self.assertEqualTelemetry('page.views:-12|c', self.recv(2))

    def test_histogram(self):
        self.statsd.histogram('histo', 123.4)
        self.assertEqualTelemetry('histo:123.4|h', self.recv(2))

    def test_pipe_in_tags(self):
        self.statsd.gauge('gt', 123.4, tags=['pipe|in:tag', 'red'])
        self.assertEqualTelemetry('gt:123.4|g|#pipe_in:tag,red', self.recv(2))

    def test_tagged_gauge(self):
        self.statsd.gauge('gt', 123.4, tags=['country:china', 'age:45', 'blue'])
        self.assertEqualTelemetry('gt:123.4|g|#country:china,age:45,blue', self.recv(2))

    def test_tagged_counter(self):
        self.statsd.increment('ct', tags=[u'country:españa', 'red'])
        self.assertEqualTelemetry(u'ct:1|c|#country:españa,red', self.recv(2))

    def test_tagged_histogram(self):
        self.statsd.histogram('h', 1, tags=['red'])
        self.assertEqualTelemetry('h:1|h|#red', self.recv(2))

    def test_sample_rate(self):
        self.statsd._telemetry = False # disabling telemetry since sample_rate imply randomness
        self.statsd.increment('c', sample_rate=0)
        self.assertFalse(self.statsd.socket.recv())
        for i in range(10000):
            self.statsd.increment('sampled_counter', sample_rate=0.3)
        self.assertAlmostEqual(3000, len(self.statsd.socket.payloads), 150)
        self.assertEqual('sampled_counter:1|c|@0.3', self.recv())

    def test_default_sample_rate(self):
        self.statsd._telemetry = False # disabling telemetry since sample_rate imply randomness
        self.statsd.default_sample_rate = 0.3
        for i in range(10000):
            self.statsd.increment('sampled_counter')
        self.assertAlmostEqual(3000, len(self.statsd.socket.payloads), 150)
        self.assertEqual('sampled_counter:1|c|@0.3', self.recv())

    def test_tags_and_samples(self):
        self.statsd._telemetry = False # disabling telemetry since sample_rate imply randomness
        for i in range(100):
            self.statsd.gauge('gst', 23, tags=["sampled"], sample_rate=0.9)

        def test_tags_and_samples(self):
            for i in range(100):
                self.statsd.gauge('gst', 23, tags=["sampled"], sample_rate=0.9)
            self.assertEqual('gst:23|g|@0.9|#sampled')

    def test_timing(self):
        self.statsd.timing('t', 123)
        self.assertEqualTelemetry('t:123|ms', self.recv(2))

    def test_event(self):
        self.statsd.event('Title', u'L1\nL2', priority='low', date_happened=1375296969)
        event = u'_e{5,6}:Title|L1\\nL2|d:1375296969|p:low'
        self.assertEqualTelemetry(event, self.recv(2), telemetry=telemetry_metrics(metrics=0, events=1, bytes_sent=len(event)))

        self.statsd._reset_telemetry()

        self.statsd.event('Title', u'♬ †øU †øU ¥ºu T0µ ♪',
                          aggregation_key='key', tags=['t1', 't2:v2'])
        event = u'_e{5,19}:Title|♬ †øU †øU ¥ºu T0µ ♪|k:key|#t1,t2:v2'
        self.assertEqualTelemetry(event, self.recv(2), telemetry=telemetry_metrics(metrics=0, events=1, bytes_sent=len(event)))

    def test_event_constant_tags(self):
        self.statsd.constant_tags = ['bar:baz', 'foo']
        self.statsd.event('Title', u'L1\nL2', priority='low', date_happened=1375296969)
        event = u'_e{5,6}:Title|L1\\nL2|d:1375296969|p:low|#bar:baz,foo'
        self.assertEqualTelemetry(event, self.recv(2), telemetry=telemetry_metrics(metrics=0, events=1, tags="bar:baz,foo", bytes_sent=len(event)))

        self.statsd._reset_telemetry()

        self.statsd.event('Title', u'♬ †øU †øU ¥ºu T0µ ♪',
                          aggregation_key='key', tags=['t1', 't2:v2'])
        event = u'_e{5,19}:Title|♬ †øU †øU ¥ºu T0µ ♪|k:key|#t1,t2:v2,bar:baz,foo'
        self.assertEqualTelemetry(event, self.recv(2), telemetry=telemetry_metrics(metrics=0, events=1, tags="bar:baz,foo", bytes_sent=len(event)))

    def test_service_check(self):
        now = int(time.time())
        self.statsd.service_check(
            'my_check.name', self.statsd.WARNING,
            tags=['key1:val1', 'key2:val2'], timestamp=now,
            hostname='i-abcd1234', message=u"♬ †øU \n†øU ¥ºu|m: T0µ ♪")
        check = u'_sc|my_check.name|{0}|d:{1}|h:i-abcd1234|#key1:val1,key2:val2|m:{2}'.format(self.statsd.WARNING, now, u"♬ †øU \\n†øU ¥ºu|m\\: T0µ ♪")
        self.assertEqualTelemetry(check, self.recv(2), telemetry=telemetry_metrics(metrics=0, service_checks=1, bytes_sent=len(check)))

    def test_service_check_constant_tags(self):
        self.statsd.constant_tags = ['bar:baz', 'foo']
        now = int(time.time())
        self.statsd.service_check(
            'my_check.name', self.statsd.WARNING,
            timestamp=now,
            hostname='i-abcd1234', message=u"♬ †øU \n†øU ¥ºu|m: T0µ ♪")
        check = u'_sc|my_check.name|{0}|d:{1}|h:i-abcd1234|#bar:baz,foo|m:{2}'.format(self.statsd.WARNING, now, u"♬ †øU \\n†øU ¥ºu|m\\: T0µ ♪")
        self.assertEqualTelemetry(check, self.recv(2),
            telemetry=telemetry_metrics(metrics=0, service_checks=1, tags="bar:baz,foo", bytes_sent=len(check)))

        self.statsd._reset_telemetry()

        self.statsd.service_check(
            'my_check.name', self.statsd.WARNING,
            tags=['key1:val1', 'key2:val2'], timestamp=now,
            hostname='i-abcd1234', message=u"♬ †øU \n†øU ¥ºu|m: T0µ ♪")
        check = u'_sc|my_check.name|{0}|d:{1}|h:i-abcd1234|#key1:val1,key2:val2,bar:baz,foo|m:{2}'.format(self.statsd.WARNING, now, u"♬ †øU \\n†øU ¥ºu|m\\: T0µ ♪")
        self.assertEqualTelemetry(check, self.recv(2),
            telemetry=telemetry_metrics(metrics=0, service_checks=1, tags="bar:baz,foo", bytes_sent=len(check)))

    def test_metric_namespace(self):
        """
        Namespace prefixes all metric names.
        """
        self.statsd.namespace = "foo"
        self.statsd.gauge('gauge', 123.4)
        self.assertEqualTelemetry('foo.gauge:123.4|g', self.recv(2))

    # Test Client level contant tags
    def test_gauge_constant_tags(self):
        self.statsd.constant_tags=['bar:baz', 'foo']
        self.statsd.gauge('gauge', 123.4)
        metric = 'gauge:123.4|g|#bar:baz,foo'
        self.assertEqualTelemetry(metric, self.recv(2), telemetry=telemetry_metrics(tags="bar:baz,foo", bytes_sent=len(metric)))

    def test_counter_constant_tag_with_metric_level_tags(self):
        self.statsd.constant_tags=['bar:baz', 'foo']
        self.statsd.increment('page.views', tags=['extra'])
        metric = 'page.views:1|c|#extra,bar:baz,foo'
        self.assertEqualTelemetry(metric, self.recv(2), telemetry=telemetry_metrics(tags="bar:baz,foo", bytes_sent=len(metric)))

    def test_gauge_constant_tags_with_metric_level_tags_twice(self):
        metric_level_tag = ['foo:bar']
        self.statsd.constant_tags=['bar:baz']
        self.statsd.gauge('gauge', 123.4, tags=metric_level_tag)
        metric = 'gauge:123.4|g|#foo:bar,bar:baz'
        self.assertEqualTelemetry(metric, self.recv(2), telemetry=telemetry_metrics(tags="bar:baz", bytes_sent=len(metric)))

        self.statsd._reset_telemetry()

        # sending metrics multiple times with same metric-level tags
        # should not duplicate the tags being sent
        self.statsd.gauge('gauge', 123.4, tags=metric_level_tag)
        metric = "gauge:123.4|g|#foo:bar,bar:baz"
        self.assertEqualTelemetry(metric, self.recv(2), telemetry=telemetry_metrics(tags="bar:baz", bytes_sent=len(metric)))

    def test_socket_error(self):
        self.statsd.socket = BrokenSocket()
        with mock.patch("datadog.dogstatsd.base.log") as mock_log:
            self.statsd.gauge('no error', 1)
            mock_log.error.assert_not_called()
            mock_log.warning.assert_called_once_with(
                "Error submitting packet: %s, dropping the packet and closing the socket",
                mock.ANY,
            )

    def test_socket_overflown(self):
        self.statsd.socket = OverflownSocket()
        with mock.patch("datadog.dogstatsd.base.log") as mock_log:
            self.statsd.gauge('no error', 1)
            mock_log.error.assert_not_called()
            c = [call("Socket send would block: %s, dropping the packet", mock.ANY)]
            mock_log.debug.assert_has_calls(c * 2)

    def test_distributed(self):
        """
        Measure the distribution of a function's run time using distribution custom metric.
        """
        # In seconds
        @self.statsd.distributed('distributed.test')
        def func(a, b, c=1, d=1):
            """docstring"""
            time.sleep(0.5)
            return (a, b, c, d)

        self.assertEqual('func', func.__name__)
        self.assertEqual('docstring', func.__doc__)

        result = func(1, 2, d=3)
        # Assert it handles args and kwargs correctly.
        self.assertEqual(result, (1, 2, 1, 3))

        packet = self.recv(2).split("\n")[0] # ignore telemetry packet
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('d', type_)
        self.assertEqual('distributed.test', name)
        self.assertAlmostEqual(0.5, float(value), 0.1)

        # Repeat, force timer value in milliseconds
        @self.statsd.distributed('distributed.test', use_ms=True)
        def func(a, b, c=1, d=1):
            """docstring"""
            time.sleep(0.5)
            return (a, b, c, d)

        func(1, 2, d=3)

        packet = self.recv(2).split("\n")[0] # ignore telemetry packet
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('d', type_)
        self.assertEqual('distributed.test', name)
        self.assertAlmostEqual(500, float(value), 100)


    def test_timed(self):
        """
        Measure the distribution of a function's run time.
        """
        # In seconds
        @self.statsd.timed('timed.test')
        def func(a, b, c=1, d=1):
            """docstring"""
            time.sleep(0.5)
            return (a, b, c, d)

        self.assertEqual('func', func.__name__)
        self.assertEqual('docstring', func.__doc__)

        result = func(1, 2, d=3)
        # Assert it handles args and kwargs correctly.
        self.assertEqual(result, (1, 2, 1, 3))

        packet = self.recv(2).split("\n")[0] # ignore telemetry packet
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('ms', type_)
        self.assertEqual('timed.test', name)
        self.assertAlmostEqual(0.5, float(value), 0.1)

        # Repeat, force timer value in milliseconds
        @self.statsd.timed('timed.test', use_ms=True)
        def func(a, b, c=1, d=1):
            """docstring"""
            time.sleep(0.5)
            return (a, b, c, d)

        func(1, 2, d=3)

        packet = self.recv(2).split("\n")[0] # ignore telemetry packet
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('ms', type_)
        self.assertEqual('timed.test', name)
        self.assertAlmostEqual(500, float(value), 100)

    def test_timed_in_ms(self):
        """
        Timed value is reported in ms when statsd.use_ms is True.
        """
        # Arm statsd to use_ms
        self.statsd.use_ms = True

        # Sample a function run time
        @self.statsd.timed('timed.test')
        def func(a, b, c=1, d=1):
            """docstring"""
            time.sleep(0.5)
            return (a, b, c, d)

        func(1, 2, d=3)

        # Assess the packet
        packet = self.recv(2).split("\n")[0] # ignore telemetry packet
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('ms', type_)
        self.assertEqual('timed.test', name)
        self.assertAlmostEqual(500, float(value), 100)

        # Repeat, force timer value in seconds
        @self.statsd.timed('timed.test', use_ms=False)
        def func(a, b, c=1, d=1):
            """docstring"""
            time.sleep(0.5)
            return (a, b, c, d)

        func(1, 2, d=3)

        packet = self.recv()
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('ms', type_)
        self.assertEqual('timed.test', name)
        self.assertAlmostEqual(0.5, float(value), 0.1)

    def test_timed_no_metric(self, ):
        """
        Test using a decorator without providing a metric.
        """

        @self.statsd.timed()
        def func(a, b, c=1, d=1):
            """docstring"""
            time.sleep(0.5)
            return (a, b, c, d)

        self.assertEqual('func', func.__name__)
        self.assertEqual('docstring', func.__doc__)

        result = func(1, 2, d=3)
        # Assert it handles args and kwargs correctly.
        self.assertEqual(result, (1, 2, 1, 3))

        packet = self.recv(2).split("\n")[0] # ignore telemetry packet
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('ms', type_)
        self.assertEqual('tests.unit.dogstatsd.test_statsd.func', name)
        self.assertAlmostEqual(0.5, float(value), 0.1)

    @pytest.mark.skipif(not is_higher_py35(), reason="Coroutines are supported on Python 3.5 or higher.")
    def test_timed_coroutine(self):
        """
        Measure the distribution of a coroutine function's run time.

        Warning: Python > 3.5 only.
        """
        import asyncio

        source= """
@self.statsd.timed('timed.test')
async def print_foo():
    "docstring"
    import time
    time.sleep(0.5)
    print("foo")
        """
        exec(source, {}, locals())

        loop = asyncio.get_event_loop()
        loop.run_until_complete(locals()['print_foo']())
        loop.close()

        # Assert
        packet = self.recv(2).split("\n")[0] # ignore telemetry packet
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('ms', type_)
        self.assertEqual('timed.test', name)
        self.assertAlmostEqual(0.5, float(value), 0.1)

    def test_timed_context(self):
        """
        Measure the distribution of a context's run time.
        """
        # In seconds
        with self.statsd.timed('timed_context.test') as timer:
            self.assertTrue(isinstance(timer, TimedContextManagerDecorator))
            time.sleep(0.5)

        packet = self.recv(2).split("\n")[0] # ignore telemetry packet
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('ms', type_)
        self.assertEqual('timed_context.test', name)
        self.assertAlmostEqual(0.5, float(value), 0.1)
        self.assertAlmostEqual(0.5, timer.elapsed, 0.1)

        # In milliseconds
        with self.statsd.timed('timed_context.test', use_ms=True) as timer:
            time.sleep(0.5)

        packet = self.recv(2).split("\n")[0] # ignore telemetry packet
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('ms', type_)
        self.assertEqual('timed_context.test', name)
        self.assertAlmostEqual(500, float(value), 100)
        self.assertAlmostEqual(500, timer.elapsed, 100)

    def test_timed_context_exception(self):
        """
        Exception bubbles out of the `timed` context manager.
        """
        class ContextException(Exception):
            pass

        def func(self):
            with self.statsd.timed('timed_context.test.exception'):
                time.sleep(0.5)
                raise ContextException()

        # Ensure the exception was raised.
        with pytest.raises(ContextException):
            func(self)

        # Ensure the timing was recorded.
        packet = self.recv(2).split("\n")[0] # ignore telemetry packet
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('ms', type_)
        self.assertEqual('timed_context.test.exception', name)
        self.assertAlmostEqual(0.5, float(value), 0.1)

    def test_timed_context_no_metric_exception(self):
        """Test that an exception occurs if using a context manager without a metric."""

        def func(self):
            with self.statsd.timed():
                time.sleep(0.5)

        # Ensure the exception was raised.
        with pytest.raises(TypeError):
            func(self)

        # Ensure the timing was recorded.
        packet = self.statsd.socket.recv()
        self.assertIsNone(packet)

    def test_timed_start_stop_calls(self):
        # In seconds
        timer = self.statsd.timed('timed_context.test')
        timer.start()
        time.sleep(0.5)
        timer.stop()

        packet = self.recv(2).split("\n")[0] # ignore telemetry packet
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('ms', type_)
        self.assertEqual('timed_context.test', name)
        self.assertAlmostEqual(0.5, float(value), 0.1)

        # In milliseconds
        timer = self.statsd.timed('timed_context.test', use_ms=True)
        timer.start()
        time.sleep(0.5)
        timer.stop()

        packet = self.recv(2).split("\n")[0] # ignore telemetry packet
        name_value, type_ = packet.split('|')
        name, value = name_value.split(':')

        self.assertEqual('ms', type_)
        self.assertEqual('timed_context.test', name)
        self.assertAlmostEqual(500, float(value), 100)

    def test_batched(self):
        self.statsd.open_buffer()
        self.statsd.gauge('page.views', 123)
        self.statsd.timing('timer', 123)
        self.statsd.close_buffer()
        expected = "page.views:123|g\ntimer:123|ms"
        self.assertEqualTelemetry(expected, self.recv(2), telemetry=telemetry_metrics(metrics=2, bytes_sent=len(expected)))

    def test_telemetry(self):
        self.statsd.metrics_count = 1
        self.statsd.events_count = 2
        self.statsd.service_checks_count = 3
        self.statsd.bytes_sent = 4
        self.statsd.bytes_dropped = 5
        self.statsd.packets_sent = 6
        self.statsd.packets_dropped = 7

        self.statsd.open_buffer()
        self.statsd.gauge('page.views', 123)
        self.statsd.close_buffer()

        payload = "page.views:123|g"
        telemetry = telemetry_metrics(metrics=2, events=2, service_checks=3, bytes_sent=4 + len(payload),
                                          bytes_dropped=5,  packets_sent=7, packets_dropped=7)

        self.assertEqualTelemetry(payload, self.recv(2), telemetry=telemetry)

        self.assertEqual(0, self.statsd.metrics_count)
        self.assertEqual(0, self.statsd.events_count)
        self.assertEqual(0, self.statsd.service_checks_count)
        self.assertEqual(len(telemetry), self.statsd.bytes_sent)
        self.assertEqual(0, self.statsd.bytes_dropped)
        self.assertEqual(1, self.statsd.packets_sent)
        self.assertEqual(0, self.statsd.packets_dropped)

    def test_telemetry_flush_interval(self):
        statsd = DogStatsd()
        fake_socket = FakeSocket()
        statsd.socket = fake_socket

        # set the last flush time in the future to be sure we won't flush
        statsd._last_flush_time = time.time() + statsd._telemetry_flush_interval
        statsd.gauge('gauge', 123.4)

        metric = 'gauge:123.4|g'
        self.assertEqual(metric, fake_socket.recv())

        t1 = time.time()
        # setting the last flush time in the past to trigger a telemetry flush
        statsd._last_flush_time = t1 - statsd._telemetry_flush_interval -1
        statsd.gauge('gauge', 123.4)
        self.assertEqualTelemetry(metric, fake_socket.recv(2), telemetry=telemetry_metrics(metrics=2, bytes_sent=2*len(metric), packets_sent=2))

        # assert that _last_flush_time has been updated
        self.assertTrue(t1 < statsd._last_flush_time)

    def test_telemetry_flush_interval_alternate_destination(self):
        statsd = DogStatsd(telemetry_host='foo')
        fake_socket = FakeSocket()
        statsd.socket = fake_socket
        fake_telemetry_socket = FakeSocket()
        statsd.telemetry_socket = fake_telemetry_socket

        self.assertIsNotNone(statsd.telemetry_host)
        self.assertIsNotNone(statsd.telemetry_port)
        self.assertTrue(statsd._dedicated_telemetry_destination())

        # set the last flush time in the future to be sure we won't flush
        statsd._last_flush_time = time.time() + statsd._telemetry_flush_interval
        statsd.gauge('gauge', 123.4)

        self.assertEqual('gauge:123.4|g', fake_socket.recv())

        t1 = time.time()
        # setting the last flush time in the past to trigger a telemetry flush
        statsd._last_flush_time = t1 - statsd._telemetry_flush_interval - 1
        statsd.gauge('gauge', 123.4)

        self.assertEqual('gauge:123.4|g', fake_socket.recv())
        self.assertEqualTelemetry('', fake_telemetry_socket.recv(), telemetry=telemetry_metrics(metrics=2, bytes_sent=13*2, packets_sent=2))
        # assert that _last_flush_time has been updated
        assert t1 < statsd._last_flush_time

    def test_telemetry_flush_interval_batch(self):
        statsd = DogStatsd()

        fake_socket = FakeSocket()
        statsd.socket = fake_socket

        statsd.open_buffer()
        statsd.gauge('gauge1', 1)
        statsd.gauge('gauge2', 2)

        t1 = time.time()
        # setting the last flush time in the past to trigger a telemetry flush
        statsd._last_flush_time = t1 - statsd._telemetry_flush_interval -1

        statsd.close_buffer()

        metric = 'gauge1:1|g\ngauge2:2|g'
        self.assertEqualTelemetry(metric, fake_socket.recv(2), telemetry=telemetry_metrics(metrics=2, bytes_sent=len(metric)))
        # assert that _last_flush_time has been updated
        self.assertTrue(t1 < statsd._last_flush_time)

    def test_context_manager(self):
        fake_socket = FakeSocket()
        with DogStatsd(telemetry_min_flush_interval=0) as statsd:
            statsd.socket = fake_socket
            statsd.gauge('page.views', 123)
            statsd.timing('timer', 123)
        metric = "page.views:123|g\ntimer:123|ms"
        self.assertEqual(metric, fake_socket.recv())
        self.assertEqual(telemetry_metrics(metrics=2, bytes_sent=len(metric)), fake_socket.recv())
        # self.assertEqualTelemetry("page.views:123|g\ntimer:123|ms", fake_socket.recv(2), telemetry=telemetry_metrics(metrics=2))

    def test_batched_buffer_autoflush(self):
        fake_socket = FakeSocket()
        bytes_sent = 0
        with DogStatsd(telemetry_min_flush_interval=0) as statsd:
            single_metric = 'mycounter:1|c'
            self.assertEqual(statsd._max_payload_size, UDP_OPTIMAL_PAYLOAD_LENGTH)
            metrics_per_packet = statsd._max_payload_size // (len(single_metric) + 1)
            statsd.socket = fake_socket
            for i in range(metrics_per_packet + 1):
                statsd.increment('mycounter')
            payload = '\n'.join([single_metric for i in range(metrics_per_packet)])

            telemetry = telemetry_metrics(metrics=metrics_per_packet+1, bytes_sent=len(payload))
            bytes_sent += len(payload) + len(telemetry)
            self.assertEqual(payload, fake_socket.recv())
            self.assertEqual(telemetry, fake_socket.recv())
        self.assertEqual(single_metric, fake_socket.recv())
        telemetry = telemetry_metrics(metrics=0, packets_sent=2, bytes_sent=len(single_metric) + len(telemetry))
        self.assertEqual(telemetry, fake_socket.recv())

    def test_module_level_instance(self):
        self.assertTrue(isinstance(statsd, DogStatsd))

    def test_instantiating_does_not_connect(self):
        dogpound = DogStatsd()
        self.assertIsNone(dogpound.socket)

    def test_accessing_socket_opens_socket(self):
        dogpound = DogStatsd()
        try:
            self.assertIsNotNone(dogpound.get_socket())
        finally:
            dogpound.socket.close()

    def test_accessing_socket_multiple_times_returns_same_socket(self):
        dogpound = DogStatsd()
        fresh_socket = FakeSocket()
        dogpound.socket = fresh_socket
        self.assertEqual(fresh_socket, dogpound.get_socket())
        self.assertNotEqual(FakeSocket(), dogpound.get_socket())

    def test_tags_from_environment(self):
        with preserve_environment_variable('DATADOG_TAGS'):
            os.environ['DATADOG_TAGS'] = 'country:china,age:45,blue'
            statsd = DogStatsd(telemetry_min_flush_interval=0)
        statsd.socket = FakeSocket()
        statsd.gauge('gt', 123.4)
        metric = 'gt:123.4|g|#country:china,age:45,blue'
        self.assertEqual(metric, statsd.socket.recv())
        self.assertEqual(telemetry_metrics(tags="country:china,age:45,blue", bytes_sent=len(metric)), statsd.socket.recv())

    def test_tags_from_environment_and_constant(self):
        with preserve_environment_variable('DATADOG_TAGS'):
            os.environ['DATADOG_TAGS'] = 'country:china,age:45,blue'
            statsd = DogStatsd(constant_tags=['country:canada', 'red'], telemetry_min_flush_interval=0)
        statsd.socket = FakeSocket()
        statsd.gauge('gt', 123.4)
        tags="country:canada,red,country:china,age:45,blue"
        metric = 'gt:123.4|g|#'+tags
        self.assertEqual(metric, statsd.socket.recv())
        self.assertEqual(telemetry_metrics(tags=tags, bytes_sent=len(metric)), statsd.socket.recv())

    def test_entity_tag_from_environment(self):
        with preserve_environment_variable('DD_ENTITY_ID'):
            os.environ['DD_ENTITY_ID'] = '04652bb7-19b7-11e9-9cc6-42010a9c016d'
            statsd = DogStatsd(telemetry_min_flush_interval=0)
        statsd.socket = FakeSocket()
        statsd.gauge('gt', 123.4)
        metric = 'gt:123.4|g|#dd.internal.entity_id:04652bb7-19b7-11e9-9cc6-42010a9c016d'
        self.assertEqual(metric, statsd.socket.recv())
        self.assertEqual(
            telemetry_metrics(tags="dd.internal.entity_id:04652bb7-19b7-11e9-9cc6-42010a9c016d", bytes_sent=len(metric)),
            statsd.socket.recv())

    def test_entity_tag_from_environment_and_constant(self):
        with preserve_environment_variable('DD_ENTITY_ID'):
            os.environ['DD_ENTITY_ID'] = '04652bb7-19b7-11e9-9cc6-42010a9c016d'
            statsd = DogStatsd(constant_tags=['country:canada', 'red'], telemetry_min_flush_interval=0)
        statsd.socket = FakeSocket()
        statsd.gauge('gt', 123.4)
        metric = 'gt:123.4|g|#country:canada,red,dd.internal.entity_id:04652bb7-19b7-11e9-9cc6-42010a9c016d'
        self.assertEqual(metric, statsd.socket.recv())
        self.assertEqual(
            telemetry_metrics(tags="country:canada,red,dd.internal.entity_id:04652bb7-19b7-11e9-9cc6-42010a9c016d",
            bytes_sent=len(metric)), statsd.socket.recv())

    def test_entity_tag_and_tags_from_environment_and_constant(self):
        with preserve_environment_variable('DATADOG_TAGS'):
            os.environ['DATADOG_TAGS'] = 'country:china,age:45,blue'
            with preserve_environment_variable('DD_ENTITY_ID'):
                os.environ['DD_ENTITY_ID'] = '04652bb7-19b7-11e9-9cc6-42010a9c016d'
                statsd = DogStatsd(constant_tags=['country:canada', 'red'], telemetry_min_flush_interval=0)
        statsd.socket = FakeSocket()
        statsd.gauge('gt', 123.4)
        tags = "country:canada,red,country:china,age:45,blue,dd.internal.entity_id:04652bb7-19b7-11e9-9cc6-42010a9c016d"
        metric = 'gt:123.4|g|#'+tags
        self.assertEqual(metric, statsd.socket.recv())
        self.assertEqual(telemetry_metrics(tags=tags, bytes_sent=len(metric)), statsd.socket.recv())

    def test_dogstatsd_initialization_with_dd_env_service_version(self):
        """
        Dogstatsd should automatically use DD_ENV, DD_SERVICE, and DD_VERSION (if present)
        to set {env, service, version} as global tags for all metrics emitted.
        """
        cases = [
            # Test various permutations of setting DD_* env vars, as well as other global tag configuration.
            # An empty string signifies that the env var either isn't set or that it is explicitly set to empty string.
            ('', '', '', '', [], []),
            ('prod', '', '', '', [], ['env:prod']),
            ('prod', 'dog', '', '', [], ['env:prod', 'service:dog']),
            ('prod', 'dog', 'abc123', '', [], ['env:prod', 'service:dog', 'version:abc123']),
            ('prod', 'dog', 'abc123', 'env:prod,type:app', [], ['env:prod', 'env:prod', 'service:dog', 'type:app', 'version:abc123']),
            ('prod', 'dog', 'abc123', 'env:prod2,type:app', [], ['env:prod', 'env:prod2', 'service:dog', 'type:app', 'version:abc123']),
            ('prod', 'dog', 'abc123', '', ['env:prod', 'type:app'], ['env:prod', 'env:prod', 'service:dog', 'type:app', 'version:abc123']),
            ('prod', 'dog', 'abc123', '', ['env:prod2', 'type:app'], ['env:prod', 'env:prod2', 'service:dog', 'type:app', 'version:abc123']),
            ('prod', 'dog', 'abc123', 'env:prod3,custom_tag:cat', ['env:prod2', 'type:app'], ['custom_tag:cat', 'env:prod', 'env:prod2', 'env:prod3', 'service:dog', 'type:app', 'version:abc123']),
        ]
        for c in cases:
            dd_env, dd_service, dd_version, datadog_tags, constant_tags, global_tags = c
            with EnvVars(
                env_vars={
                    'DATADOG_TAGS': datadog_tags,
                    'DD_ENV': dd_env,
                    'DD_SERVICE': dd_service,
                    'DD_VERSION': dd_version,
                }
            ):
                statsd = DogStatsd(constant_tags=constant_tags, telemetry_min_flush_interval=0)
                statsd.socket = FakeSocket()

            # Guarantee consistent ordering, regardless of insertion order.
            statsd.constant_tags.sort()
            self.assertEqual(global_tags, statsd.constant_tags)

            # Make call with no tags passed; only the globally configured tags will be used.
            global_tags_str = ','.join([t for t in global_tags])
            statsd.gauge('gt', 123.4)

            # Protect against the no tags case.
            metric = 'gt:123.4|g|#{}'.format(global_tags_str) if global_tags_str else 'gt:123.4|g'
            self.assertEqual(metric, statsd.socket.recv())
            self.assertEqual(telemetry_metrics(tags=global_tags_str, bytes_sent=len(metric)), statsd.socket.recv())
            statsd._reset_telemetry()

            # Make another call with local tags passed.
            passed_tags = ['env:prod', 'version:def456', 'custom_tag:toad']
            all_tags_str = ','.join([t for t in passed_tags + global_tags])
            statsd.gauge('gt', 123.4, tags=passed_tags)

            metric = 'gt:123.4|g|#{}'.format(all_tags_str)
            self.assertEqual(metric, statsd.socket.recv())
            self.assertEqual(telemetry_metrics(tags=global_tags_str, bytes_sent=len(metric)), statsd.socket.recv())

    def test_gauge_doesnt_send_None(self):
        self.statsd.gauge('metric', None)
        self.assertIsNone(self.statsd.socket.recv())

    def test_increment_doesnt_send_None(self):
        self.statsd.increment('metric', None)
        self.assertIsNone(self.statsd.socket.recv())

    def test_decrement_doesnt_send_None(self):
        self.statsd.decrement('metric', None)
        self.assertIsNone(self.statsd.socket.recv())

    def test_timing_doesnt_send_None(self):
        self.statsd.timing('metric', None)
        self.assertIsNone(self.statsd.socket.recv())

    def test_histogram_doesnt_send_None(self):
        self.statsd.histogram('metric', None)
        self.assertIsNone(self.statsd.socket.recv())
