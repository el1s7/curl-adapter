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
	from gevent._types import _IoWatcher # type: ignore

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
	if timeout_ms == -1:
		for timer in gevent_curl._timers:
			timer.kill(block=False)
		gevent_curl._timers = set()
	elif timeout_ms == 0:
		# immediate timeout; invoke directly
		timer = gevent.spawn(gevent_curl._process_data, CURL_SOCKET_TIMEOUT, CURL_POLL_NONE)
		gevent_curl._timers.add(timer)
	else:
		if timeout_ms > 0:
			# schedule one timer
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
	want_read  = bool(what & CURL_POLL_IN)
	want_write = bool(what & CURL_POLL_OUT)

	# compute the new mask for gevent
	new_mask = 0
	if want_read:  new_mask |= GEVENT_READ
	if want_write: new_mask |= GEVENT_WRITE

	# teardown if libcurl says “remove”
	if what & CURL_POLL_REMOVE:
		gevent_curl._update_watcher(sockfd, 0)
		return

	# otherwise install/update the watcher
	gevent_curl._update_watcher(sockfd, new_mask)
	
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
		self._watchers: dict[typing.Any, dict[str, _IoWatcher]] = {}

		self._results: dict[Curl, AsyncResult] = {}
		self._handles: dict[ffi.CData, Curl] = {}
		self._callbacks: dict[Curl, callable] = {}
		
		self._checker = gevent.spawn(self._force_timeout)

		self._start_closing = False

		self._set_options()

	def add_handle(self, curl: Curl, cleanup_after_perform: typing.Callable[[typing.Optional[Exception]], None]=None):
		"""Add a curl handle to be managed by curl_multi. This is the equivalent of
		`perform` in the async world."""

		if self._start_closing:
			raise RuntimeError("This curl_multi instance is closed.")

		code = lib.curl_multi_add_handle(self._curl_multi, curl._curl)
		
		if code != 0:
			curl_error = curl._get_error(code, "perform")
			self._set_exception(curl, RuntimeError(f"curl_multi_add_handle failed: {curl_error}"))
			
			# return a future that’s already failed
			result = AsyncResult()
			result.set_exception(RuntimeError(curl_error))
			return result
		
		result = AsyncResult()
		self._results[curl] = result
		self._callbacks[curl] = cleanup_after_perform
		self._handles[curl._curl] = curl

		return result

	def cancel_handle(self, curl: Curl):
		"""Cancel is not natively supported in gevent.AsyncResult."""

		# No true cancellation; set an exception or drop reference
		self._set_exception(curl, RuntimeError("Cancelled"))

	def graceful_close(self):
		self._start_closing = True

	def close(self):
		"""Close and cleanup running timers, readers, writers and handles."""
		 # Close and wait for the force timeout checker to complete

		if self._checker and not self._checker.dead:
			self._checker.kill(block=False)

		# Close all pending futures
		for curl in list(self._results.keys()):
			self.cancel_handle(curl)
			
		# Cleanup curl_multi handle
		if self._curl_multi:
			ref_curl_multi = self._curl_multi
			self._curl_multi = None
			lib.curl_multi_cleanup(ref_curl_multi)
			ref_curl_multi = None
			

		# Remove watchers
		for sockfd, entry in list(self._watchers.items()):
			if entry.get("watcher"):
				entry.get("watcher").stop()      # stop monitoring
				entry.get("watcher").close()      # dispose of the watcher
				del self._watchers[sockfd]

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
			if not self._curl_multi:
				break
			
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
					curl_error = curl._get_error(retcode, "perform")
					self._set_exception(curl, curl_error)
	
	def _force_timeout(self):
		while self._curl_multi:
			gevent.sleep(1)
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

		if self._start_closing and not self._results:
			self.close()
		
	def _set_exception(self, curl: Curl, exception):
		result = self._pop_future(curl)
		self._callback(curl, exception)
		
		if result and not result.ready():
			result.set_exception(exception)

		if self._start_closing and not self._results:
			self.close()

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
			ev_bitmask |= CURL_CSELECT_IN
		if mask & GEVENT_WRITE:
			ev_bitmask |= CURL_CSELECT_OUT

		# this is exactly what you were doing before:
		# call socket_action + info_read → _process_data
		self._process_data(fd, ev_bitmask)
