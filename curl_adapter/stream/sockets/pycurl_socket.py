import typing
from typing import TYPE_CHECKING
import warnings

import pycurl
import gevent
from gevent.event import AsyncResult
from gevent.lock import Semaphore

if TYPE_CHECKING:
	from gevent._types import _IoWatcher # type: ignore


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
		self._watchers: dict[typing.Any, dict[str, _IoWatcher]] = {}

		self._results: dict[pycurl.Curl, AsyncResult] = {}
		self._callbacks: dict[pycurl.Curl, callable] = {}

		self._checker: gevent.Greenlet = None #self._checker = gevent.spawn(self._force_timeout)
		
		self._start_closing = False

		self._set_options()
	
	def add_handle(self, curl: pycurl.Curl, cleanup_after_perform: typing.Callable[[typing.Optional[Exception]], None]=None):
		"""Add a curl handle to be managed by curl_multi. This is the equivalent of
		`perform` in the async world."""

		if self._start_closing:
			raise RuntimeError("This curl_multi instance is closed.")

		self._curl_multi.add_handle(curl)
	
		result = AsyncResult()
		self._results[curl] = result
		self._callbacks[curl] = cleanup_after_perform

		return result
	
	def cancel_handle(self, curl: pycurl.Curl):
		"""Cancel is not natively supported in gevent.AsyncResult."""
		self._set_exception(curl, RuntimeError("Cancelled"))
	
	def graceful_close(self):
		self._start_closing = True

	def close(self):
		"""Close and cleanup running timers, readers, writers and handles."""
		 # Close and wait for the force timeout checker to complete

		if self._checker and not self._checker.dead:
			self._checker.kill(block=False)

		# Close all pending futures (if any)
		for curl in list(self._results.keys()):
			self.cancel_handle(curl)
			
		# Cleanup curl_multi handle
		self._curl_multi.close()
		self._curl_multi = None

		# Remove watchers
		for sockfd, entry in list(self._watchers.items()):
			if entry.get("watcher"):
				entry.get("watcher").stop()      # stop monitoring
				entry.get("watcher").close()      # dispose of the watcher
				del self._watchers[sockfd]

		# Cancel all time functions
		for timer in list(self._timers):
			timer.kill()
	
	def _timer_function(self, timeout_ms: int):
		
		# A timeout_ms value of -1 means you should delete the timer.
		if timeout_ms == -1:
			for timer in self._timers:
				timer.kill(block=False)
			self._timers = set()
		elif timeout_ms == 0:
			# immediate timeout; invoke directly
			timer = gevent.spawn(self._process_data, pycurl.SOCKET_TIMEOUT, pycurl.POLL_NONE)
			self._timers.add(timer)
		else:

			if timeout_ms > 0:
				# spawn a greenlet to run after timeout_ms milliseconds
				timer = gevent.spawn_later(
					timeout_ms / 1000.0,
					self._process_data,
					pycurl.SOCKET_TIMEOUT,
					pycurl.POLL_NONE,
				)
				self._timers.add(timer)
				
	def _socket_function(self, event, sockfd, multi, data):
		"""Called by libcurl to tell us what it wants on this fd."""

		want_read  = bool(event & pycurl.POLL_IN)
		want_write = bool(event & pycurl.POLL_OUT)

		# compute the new mask for gevent
		new_mask = 0
		if want_read:  new_mask |= GEVENT_READ
		if want_write: new_mask |= GEVENT_WRITE

		# teardown if libcurl says “remove”
		if event & pycurl.POLL_REMOVE:
			self._update_watcher(sockfd, 0)
			return

		# otherwise install/update the watcher
		self._update_watcher(sockfd, new_mask)

	def _update_watcher(self, fd: int, mask: int):
		"""
		Ensure there's exactly one I/O watcher for `fd` with the given mask.
		If mask==0, we stop+remove it. If mask changes, we stop+recreate.
		"""
		entry = self._watchers.get(fd)

		# nothing to do if mask didn’t change
		if entry and entry["mask"] == mask:
			return

		# stop old watcher if any
		if entry:
			entry["watcher"].stop()
			entry["watcher"].close()
			del self._watchers[fd]

		# if new mask is zero, we’re done
		if mask == 0:
			return

		# create a new watcher for read/write as needed
		w = self.loop.io(fd, mask, ref=True, priority=None)
		# callback only gets fd; we’ll re-derive the libcurl bitmask from mask
		w.start(self._on_watcher_event, fd)

		# stash both watcher _and_ the mask we asked for
		self._watchers[fd] = {"watcher": w, "mask": mask}

	def _on_watcher_event(self, fd: int):
		"""
		A gevent-watcher fired on `fd`.  Look up its mask and call curl.
		"""
		entry = self._watchers.get(fd)
		if not entry:
			return

		mask = entry["mask"]
		ev_bitmask = 0
		if mask & GEVENT_READ:
			ev_bitmask |= pycurl.CSELECT_IN
		if mask & GEVENT_WRITE:
			ev_bitmask |= pycurl.CSELECT_OUT

		# this is exactly what you were doing before:
		# call socket_action + info_read → _process_data
		self._process_data(fd, ev_bitmask)

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
			if not self._curl_multi:
				break
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
			self._socket_action(pycurl.SOCKET_TIMEOUT, pycurl.POLL_NONE)
			
	def _callback(self, curl: pycurl.Curl, error: Exception=None):
		if curl in self._callbacks:
			callback = self._callbacks.pop(curl)
			if callable(callback):
				callback(error)
	
	def _pop_future(self, curl: pycurl.Curl):
		self._curl_multi.remove_handle(curl)
		return self._results.pop(curl, None)
	
	def _set_result(self, curl: pycurl.Curl):
		result = self._pop_future(curl)
		self._callback(curl)
		if result and not result.ready():
			result.set(None)

		if self._start_closing and not self._results:
			self.close()

	def _set_exception(self, curl: pycurl.Curl, exception):
		result = self._pop_future(curl)
		self._callback(curl, exception)
		if result and not result.ready():
			result.set_exception(exception)

		if self._start_closing and not self._results:
			self.close()