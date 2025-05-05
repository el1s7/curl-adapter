import queue
import sys
import threading
import time
import typing


def _detect_environment() -> typing.Tuple:
	## -eventlet-
	if "eventlet" in sys.modules:
		try:
			import eventlet
			import eventlet.queue
			from eventlet.patcher import is_monkey_patched as is_eventlet
			import socket
			import eventlet.event

			if is_eventlet(socket):
				return ("eventlet", eventlet, eventlet.sleep, eventlet.event.Event, eventlet.queue)

		except ImportError:
			pass

	# -gevent-
	if "gevent" in sys.modules:
		try:
			import gevent
			import gevent.queue
			from gevent import socket as _gsocket
			import socket
			import gevent.event

			if socket.socket is _gsocket.socket:
				return ("gevent", gevent, gevent.sleep, gevent.event.Event, gevent.queue)
		except ImportError:
			pass

	return ("default", threading, time.sleep, threading.Event, queue)

_THREAD_ENV, _THREAD_CLASS, _THREAD_SLEEP, _THREAD_EVENT, _THREAD_QUEUE_MODULE = _detect_environment()