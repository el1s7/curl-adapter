import gevent.event
import gevent.queue
from gevent.event import AsyncResult
from gevent.lock import Semaphore

from curl_cffi.curl import CurlOpt

from curl_adapter.stream.sockets.curl_cffi_socket import GeventCurlCffi
from curl_adapter.stream.sockets.pycurl_socket import GeventPyCurl

from .base import (
	CurlStreamHandlerBase, 
	QueueContinueRead, 
	QueueBreakRead,
)

class CurlStreamHandlerGevent(CurlStreamHandlerBase):
	'''
		Curl Stream Handler (c) 2025 by Elis K.

		Gevent only. Uses low-level curl socket handlers & multi interface.
	'''
	
	gevent_curl_cffi = GeventCurlCffi()
	gevent_pycurl = GeventPyCurl()

	_requests = 0 # track the number of requests handled
	_rotate_every = 1000 # create a new multi handle every 1000 requests
	_lock = Semaphore()

	def __init__(self, curl_instance, callback_after_perform=None, timeout=None, debug=False):
		
		super().__init__(curl_instance, callback_after_perform, timeout, debug)
		
		# Events
		self.quit_event = gevent.event.Event()  # Signal to stop streaming
		self.initialized = gevent.event.Event() # Event to set when we receive the first bytes of body, that's how we know that the headers are ready
		self.perform_finished = gevent.event.Event() # Body has finished reading

		self._future = None

		self.chunk_queue = gevent.queue.Queue()

	def _wait_for_headers(self):
		_done = self.initialized.wait(timeout=self.event_timeout)

		if not _done:
			raise self.read_timeout_error
		
		if self.debug:
			print("[DEBUG] Headers received.")
	
	def _wait_for_body(self):
		self.perform_finished.wait()

	def _dequeue_chunks(self):
		
		try:
			chunk = self.chunk_queue.get(timeout=1)
			return chunk
		except gevent.queue.Empty:
			raise QueueBreakRead()

	def _perform(self):
		if self.debug:
			print("[DEBUG] Using Gevent Stream Handler.")
		

		if self.curl_type == "curl_cffi":
			self.curl.setopt(CurlOpt.WRITEFUNCTION, self._write_callback)
			self.curl._ensure_cacert()

			self._future: AsyncResult = self.gevent_curl_cffi.add_handle(
				self.curl,
				cleanup_after_perform=self._cleanup_after_perform
			)

		elif self.curl_type == "pycurl":
			self.curl.setopt(CurlOpt.WRITEFUNCTION, self._write_callback)

			self._future = self.gevent_pycurl.add_handle(
				self.curl,
				cleanup_after_perform=self._cleanup_after_perform
			)
	
	@classmethod
	def check_rotate(cls):
		'''
			Re-initialize the `curl_multi` instances every once in a while
		'''
		with cls._lock:
			cls._requests += 1
			if cls._requests >= cls._rotate_every:
				old_gevent_curl_cffi = cls.gevent_curl_cffi
				old_gevent_pycurl = cls.gevent_pycurl

				cls.gevent_curl_cffi = GeventCurlCffi()
				cls.gevent_pycurl = GeventPyCurl()

				old_gevent_curl_cffi.graceful_close()
				old_gevent_pycurl.graceful_close()

				cls._requests = 0
				return True
			return False
		
	def close(self):
		if self.closed:
			return
		if self.debug:
			print("[DEBUG] Starting to close...")
		
		if self._future and not self._future.ready():
			if self.curl_type == "curl_cffi":
				self.gevent_curl_cffi.cancel_handle(self.curl)
			elif self.curl_type == "pycurl":
				self.gevent_pycurl.cancel_handle(self.curl)
			self._future.result()

		rotate_multi = self.__class__.check_rotate()
		if rotate_multi and self.debug:
			print("[DEBUG] Re-initalized the curl multi stream handler instance.")

		if self.debug:
			print("[DEBUG] Closing.")

		return super().close()
