import typing
import warnings
from typing import TYPE_CHECKING, Any

from curl_cffi._wrapper import ffi, lib
from curl_cffi.const import CurlMOpt
from curl_cffi.curl import Curl
from curl_cffi.utils import CurlCffiWarning

import gevent
from gevent.event import AsyncResult
from gevent.lock import Semaphore

if TYPE_CHECKING:
	from gevent._types import _TimerWatcher

CURL_POLL_NONE = 0
CURL_POLL_IN = 1
CURL_POLL_OUT = 2
CURL_POLL_INOUT = 3
CURL_POLL_REMOVE = 4

CURL_SOCKET_TIMEOUT = -1
CURL_SOCKET_BAD = -1

CURL_CSELECT_IN = 0x01
CURL_CSELECT_OUT = 0x02
CURL_CSELECT_ERR = 0x04

CURLMSG_DONE = 1

GEVENT_READ = 1
GEVENT_WRITE = 2

@ffi.def_extern()
def timer_function(curlm, timeout_ms: int, clientp: "GeventCurlCffi"):

	gevent_curl: "GeventCurlCffi" = ffi.from_handle(clientp)

	# A timeout_ms value of -1 means you should delete the timer.
	with gevent_curl._lock:
		# A timeout_ms value of -1 means you should delete the timer.
		if timeout_ms == -1:
			for timer in gevent_curl._timers:
				timer.kill(block=False)
			gevent_curl._timers = set()
		else:
			if timeout_ms >= 0:
				# spawn a greenlet to run after timeout_ms milliseconds
				timer = gevent.spawn_later(
					timeout_ms / 1000.0,
					gevent_curl._process_data,
					CURL_SOCKET_TIMEOUT,
					CURL_POLL_NONE,
				)
				gevent_curl._timers.add(timer)
				
@ffi.def_extern()
def socket_function(curlm, sockfd: int, what: int, clientp: "GeventCurlCffi", data: Any):
	gevent_curl: "GeventCurlCffi" = ffi.from_handle(clientp)

	gevent.spawn(gevent_curl._socket_function_handler(what, sockfd, curlm, data))
	
class GeventCurlCffi:
	'''
		Usage:

		multi_curl = GeventCurlCffi()
		result = multi_curl.add_handle(curl_handle)

		result.wait()
	'''

	def __init__(self):
		"""
		Parameters:
			cacert: CA cert path to use, by default, certs from ``certifi`` are used.
			loop: EventLoop to use.
		"""
		self._curl_multi = lib.curl_multi_init()
		
		self.loop = gevent.get_hub().loop

		self._timers: set[gevent.Greenlet] = set()
		self._watchers = {}

		self._results: dict[Curl, AsyncResult] = {}
		self._handles: dict[ffi.CData, Curl] = {}
		self._callbacks: dict[Curl, callable] = {}

		self._checker = gevent.spawn(self._force_timeout)

		self._lock = Semaphore()
		
		self._set_options()

	def add_handle(self, curl: Curl, cleanup_after_perform: typing.Callable[[typing.Optional[Exception]], None]=None):
		"""Add a curl handle to be managed by curl_multi. This is the equivalent of
		`perform` in the async world."""

		curl._ensure_cacert()

		lib.curl_multi_add_handle(self._curl_multi, curl._curl)
		result = AsyncResult()
		self._results[curl] = result
		self._callbacks[curl] = cleanup_after_perform
		self._handles[curl._curl] = curl

		return result

	def cancel_handle(self, curl: Curl):
		"""Cancel is not natively supported in gevent.AsyncResult."""

		# No true cancellation; set an exception or drop reference
		self._set_exception(curl, RuntimeError("Cancelled"))
		
	def close(self):
		"""Close and cleanup running timers, readers, writers and handles."""
		 # Close and wait for the force timeout checker to complete

		if self._checker and not self._checker.dead:
			self._checker.kill()

		# Close all pending futures
		for curl in self._results:
			self.cancel_handle(curl)
			
		# Cleanup curl_multi handle
		if self._curl_multi:
			lib.curl_multi_cleanup(self._curl_multi)
			self._curl_multi = None

		# Remove add readers and writers
		for sockfd in self._watchers:
			self._remove_reader(sockfd)
			self._remove_writer(sockfd)

		# Cancel all time functions
		for timer in list(self._timers):
			timer.kill()

	def _set_options(self):
		lib.curl_multi_setopt(self._curl_multi, CurlMOpt.TIMERFUNCTION, lib.timer_function)
		lib.curl_multi_setopt(self._curl_multi, CurlMOpt.SOCKETFUNCTION, lib.socket_function)

		self._self_handle = ffi.new_handle(self)
		lib.curl_multi_setopt(self._curl_multi, CurlMOpt.SOCKETDATA, self._self_handle)
		lib.curl_multi_setopt(self._curl_multi, CurlMOpt.TIMERDATA, self._self_handle)

	def _socket_action(self, sockfd: int, ev_bitmask: int) -> int:
		"""Call libcurl socket_action function"""
		running_handle = ffi.new("int *")
		lib.curl_multi_socket_action(self._curl_multi, sockfd, ev_bitmask, running_handle)
		return running_handle[0]

	def _socket_function_handler(self, event, sockfd, multi, data):
		with self._lock:
			if event & CURL_POLL_IN:
				self._add_reader(sockfd, self._process_data, sockfd, CURL_CSELECT_IN)
			if event & CURL_POLL_OUT:
				self._add_writer(sockfd, self._process_data, sockfd, CURL_CSELECT_OUT)
			if event & CURL_POLL_REMOVE:
				self._watchers.pop(sockfd, None)
	
	def _process_data(self, sockfd: int, ev_bitmask: int):
		"""Call curl_multi_info_read to read data for given socket."""
		if not self._curl_multi:
			warnings.warn(
				"Curlm already closed! quitting from _process_data",
				CurlCffiWarning,
				stacklevel=2,
			)
			return

		self._socket_action(sockfd, ev_bitmask)

		msg_in_queue = ffi.new("int *")
		while True:
			curl_msg = lib.curl_multi_info_read(self._curl_multi, msg_in_queue)
			# print("message in queue", msg_in_queue[0], curl_msg)
			if curl_msg == ffi.NULL:
				break
			if curl_msg.msg == CURLMSG_DONE:
				# print("curl_message", curl_msg.msg, curl_msg.data.result)
				curl = self._handles[curl_msg.easy_handle]
				retcode = curl_msg.data.result
				curl_error = None
				if retcode == 0:
					self._set_result(curl)
				else:
					# import pdb; pdb.set_trace()
					curl_error =  curl._get_error(retcode, "perform")
					self._set_exception(curl, curl_error)
	
	def _force_timeout(self):
		while self._curl_multi:
			gevent.sleep(1)
			#print("force timeout")
			self._socket_action(CURL_SOCKET_TIMEOUT, CURL_POLL_NONE)

	def _pop_future(self, curl: Curl):
		lib.curl_multi_remove_handle(self._curl_multi, curl._curl)
		self._handles.pop(curl._curl, None)
		return self._results.pop(curl, None)
	
	def _callback(self, curl: Curl, error: Exception=None):
		if curl in self._callbacks:
			callback = self._callbacks.pop(curl)
			if callable(callback):
				callback(error)
		
	def _set_result(self, curl: Curl):
		result = self._pop_future(curl)
		self._callback(curl)
		if result and not result.ready():
			result.set(None)
		
	def _set_exception(self, curl: Curl, exception):
		result = self._pop_future(curl)
		self._callback(curl, exception)
		if result and not result.ready():
			result.set_exception(exception)

	def _remove_reader(self, fd):
		watchers = self._watchers.get(fd)
		if watchers and 'read' in watchers:
			watchers['read'].stop()
			del watchers['read']
		if not watchers:
			self._watchers.pop(fd, None)

	def _remove_writer(self, fd):
		watchers = self._watchers.get(fd)
		if watchers and 'write' in watchers:
			watchers['write'].stop()
			del watchers['write']
		if not watchers:
			self._watchers.pop(fd, None)

	def _add_reader(self, fd, callback, *args):
		# mirror asyncio semantics: remove old first
		self._remove_reader(fd)
		# create & start a new read watcher
		w = self.loop.io(fd, GEVENT_READ, ref=True, priority=None)
		self._watchers.setdefault(fd, {})['read'] = w
		w.start(callback, *args)

	def _add_writer(self, fd, callback, *args):
		self._remove_writer(fd)
		w = self.loop.io(fd, GEVENT_WRITE, ref=True, priority=None)
		self._watchers.setdefault(fd, {})['write'] = w
		w.start(callback, *args)
