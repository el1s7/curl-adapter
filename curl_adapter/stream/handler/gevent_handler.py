import gevent.event
import gevent.queue
from gevent.event import AsyncResult

import pycurl
import curl_cffi.curl
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
			
		if isinstance(self.curl, curl_cffi.Curl):

			self.curl.setopt(CurlOpt.WRITEFUNCTION, self._write_callback)
			self.curl._ensure_cacert()

			self._future: AsyncResult = self.gevent_curl_cffi.add_handle(
				self.curl,
				cleanup_after_perform=self._cleanup_after_perform
			)

		elif isinstance(self.curl, pycurl.Curl):
			self.curl.setopt(CurlOpt.WRITEFUNCTION, self._write_callback)

			self._future = self.gevent_pycurl.add_handle(
				self.curl,
				cleanup_after_perform=self._cleanup_after_perform
			)
		else:
			raise TypeError("Cannot perform on invalid Curl object.")
	
	def close(self):
		if self.closed:
			return
		if self.debug:
			print("[DEBUG] Starting to close...")
		
		
		if self._future and not self._future.ready():
			self._future.result()

		if self.debug:
			print("[DEBUG] Closing.")

		return super().close()
