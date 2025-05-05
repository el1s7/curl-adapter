import typing
from typing import TYPE_CHECKING
import warnings

import pycurl
import gevent
from gevent.event import AsyncResult
from gevent.lock import Semaphore

if TYPE_CHECKING:
	from gevent._types import _TimerWatcher


GEVENT_READ = 1
GEVENT_WRITE = 2

class GeventPyCurl:
	'''
		Usage:

		multi_curl = GeventPyCurl()
		result = multi_curl.add_handle(curl_handle)

		result.wait()
	'''

	def __init__(self):
		"""
		Parameters:
			cacert: CA cert path to use, by default, certs from ``certifi`` are used.
			loop: EventLoop to use.
		"""
		self._curl_multi = pycurl.CurlMulti()
		
		self.loop = gevent.get_hub().loop

		self._timers: set[gevent.Greenlet] = set()
		self._watchers = {}

		self._handles_list: typing.List[pycurl.Curl] = []
		self._results: dict[pycurl.Curl, AsyncResult] = {}
		self._callbacks: dict[pycurl.Curl, callable] = {}

		self._checker = gevent.spawn(self._force_timeout)
		self._lock = Semaphore()

		self._set_options()
	
	def add_handle(self, curl: pycurl.Curl, cleanup_after_perform: typing.Callable[[typing.Optional[Exception]], None]=None):
		"""Add a curl handle to be managed by curl_multi. This is the equivalent of
		`perform` in the async world."""

		self._curl_multi.add_handle(curl)
	
		result = AsyncResult()
		self._results[curl] = result
		self._callbacks[curl] = cleanup_after_perform
		self._handles_list.append(curl)

		return result
	
	def cancel_handle(self, curl: pycurl.Curl):
		"""Cancel is not natively supported in gevent.AsyncResult."""
		self._set_exception(curl, RuntimeError("Cancelled"))
	
	def close(self):
		"""Close and cleanup running timers, readers, writers and handles."""
		 # Close and wait for the force timeout checker to complete

		if self._checker and not self._checker.dead:
			self._checker.kill()

		# Close all pending futures (if any)
		for curl in self._results:
			self.cancel_handle(curl)
			
		# Cleanup curl_multi handle
		self._curl_multi.close()
		self._curl_multi = None

		# Remove add readers and writers
		for sockfd in self._watchers:
			self._remove_reader(sockfd)
			self._remove_writer(sockfd)

		# Cancel all time functions
		for timer in list(self._timers):
			timer.kill()
	
	def _timer_function(self, timeout_ms: int):
		with self._lock:
			# A timeout_ms value of -1 means you should delete the timer.
			if timeout_ms == -1:
				for timer in self._timers:
					timer.kill(block=False)
				self._timers = set()
			else:
				if timeout_ms >= 0:
					# spawn a greenlet to run after timeout_ms milliseconds
					timer = gevent.spawn_later(
						timeout_ms / 1000.0,
						self._process_data,
						pycurl.SOCKET_TIMEOUT,
						pycurl.POLL_NONE,
					)
					self._timers.add(timer)
	
	def _socket_function(self, event, sockfd, multi, data):
		gevent.spawn(self._socket_function_handler, event, sockfd, multi, data)
	
	def _socket_function_handler(self, event, sockfd, multi, data):
		with self._lock:
			if event & pycurl.POLL_IN:
				self._add_reader(sockfd, self._process_data, sockfd, pycurl.CSELECT_IN)
			if event & pycurl.POLL_OUT:
				self._add_writer(sockfd, self._process_data, sockfd, pycurl.CSELECT_OUT)
			if event & pycurl.POLL_REMOVE:
				self._watchers.pop(sockfd, None)

	def _set_options(self):
		self._curl_multi.setopt(pycurl.M_TIMERFUNCTION, self._timer_function)
		self._curl_multi.setopt(pycurl.M_SOCKETFUNCTION, self._socket_function)

	def _socket_action(self, sockfd: int, ev_bitmask: int) -> int:
		"""Call libcurl _socket_action function"""
		ret, num_handles = self._curl_multi.socket_action(sockfd, ev_bitmask)
		return ret

	def _process_data(self, sockfd: int, ev_bitmask: int):
		"""Call curl_multi_info_read to read data for given socket."""
		if not self._curl_multi:
			warnings.warn(
				"Curlm already closed! quitting from _process_data",
				stacklevel=2,
			)
			return

		self._socket_action(sockfd, ev_bitmask)

		while True:
			num_q, ok_list, err_list = self._curl_multi.info_read()
			for curl in ok_list:
				self._set_result(curl)

			for curl, errno, errmsg in err_list:
				curl_error = pycurl.error(errno, errmsg)
				self._set_exception(curl, curl_error)
			if num_q == 0:
				break
	
	def _force_timeout(self):
		while self._curl_multi:
			gevent.sleep(1)
			#print("force timeout")
			self._socket_action(pycurl.SOCKET_TIMEOUT, pycurl.POLL_NONE)
			
	def _callback(self, curl: pycurl.Curl, error: Exception=None):
		if curl in self._callbacks:
			callback = self._callbacks.pop(curl)
			if callable(callback):
				callback(error)
	
	def _pop_future(self, curl: pycurl.Curl):
		self._curl_multi.remove_handle(curl)
		self._handles_list.remove(curl)
		return self._results.pop(curl, None)
	
	def _set_result(self, curl: pycurl.Curl):
		result = self._pop_future(curl)
		self._callback(curl)
		if result and not result.ready():
			result.set(None)

	def _set_exception(self, curl: pycurl.Curl, exception):
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
	