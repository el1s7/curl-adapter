import queue
import threading
import typing
import traceback

from curl_cffi import CurlECode, CurlError
import pycurl
import curl_cffi.curl
from curl_cffi.curl import CurlOpt

from ._thread_env import (
	_THREAD_ENV, _THREAD_CLASS, _THREAD_SLEEP, _THREAD_EVENT, _THREAD_QUEUE_MODULE
)

class QueueBreakRead(Exception):
	pass

class QueueContinueRead(Exception):
	pass

class CurlStreamHandlerBase():
	'''
		Curl Stream Handler (c) 2025 by Elis K.
	'''

	def __init__(self, 
		curl_instance: typing.Union[curl_cffi.Curl, pycurl.Curl], 
		callback_after_perform: typing.Callable[[typing.Union[curl_cffi.Curl, pycurl.Curl]], None]=None, 
		timeout: typing.Union[float, int, typing.Tuple[float, float], None]=None, 
		debug: bool=False
	):
		'''
			Initialize the stream handler.
		'''
		self.curl = curl_instance
		
		self.curl_type: typing.Literal["curl_cffi", "pycurl"] = "curl_cffi" if isinstance(self.curl, curl_cffi.Curl) else (
			 "pycurl" if isinstance(self.curl, pycurl.Curl) else None
		)

		self.read_timeout = timeout[1] if isinstance(timeout, tuple) else timeout

		self.event_timeout = self.read_timeout + 5 if self.read_timeout else None
		'''
			Timeout 'wait' events once the read timeout has already passed. To avoid blocking forever.
		'''

		self.read_timeout_error = (
			CurlError("Read timeout.", CurlECode.OPERATION_TIMEDOUT) if self.curl_type == "curl_cffi" 
			else pycurl.error(28, "Read timeout.")
		)
		
		if not self.curl_type:
			raise TypeError("Invalid curl class object.")
		
		# Events
		self.quit_event: threading.Event = _THREAD_EVENT()
		'''
			Signal to stop
		'''
		
		self.initialized: threading.Event = _THREAD_EVENT() 
		'''
			Event to set when we receive the first bytes of body, that's how we know that the headers are ready
		'''

		self.perform_finished: threading.Event = _THREAD_EVENT()
		'''
			Body has finished reading
		'''

		# Body streaming
		self.chunk_queue: queue.Queue = _THREAD_QUEUE_MODULE.Queue()
		'''
			Thread-safe queue for streaming data
		'''

		self._leftover = bytearray()
		'''
			buffer for leftover data when chunk > requested
		'''

		self.callback_after_perform = callback_after_perform
		self.debug = debug

		self.error = None 
		self.closed = False

	def _write_callback(self, chunk):
		'''
			Callback to handle incoming data chunks.
		'''	
		self.initialized.set()
			
		if self.quit_event.is_set():
			return -1  # Signal to stop

		self.chunk_queue.put(chunk)  # Add chunk to the queue
		return len(chunk)

	def _cleanup_after_perform(self, curl_error=None):

		if curl_error:
			self.error = curl_error

		# signal end of stream
		self.chunk_queue.put(None)
		if callable(self.callback_after_perform):
			try:
				self.callback_after_perform(self.curl)
			except Exception:
				if self.debug:
					traceback.print_exc()
		
		self.perform_finished.set()
		self.initialized.set()
	
	def _wait_for_headers(self):
		pass

	def _wait_for_body(self):
		pass

	def _perform(self):
		'''
			The initial basic, blocking way to call perform.
		'''

		try:
			self.curl.setopt(CurlOpt.WRITEFUNCTION, self._write_callback)
			self.curl.perform()
		except Exception as e: #(CurlError, pycurl.error)
			self.error = e
		finally:
			self._cleanup_after_perform()
		
	def start(self):
		self._perform()
		self._wait_for_headers()

		return self

	def _dequeue_chunks(self):
		try:
			chunk = self.chunk_queue.get(timeout=1)
			return chunk
		except _THREAD_QUEUE_MODULE.Empty:
			raise QueueBreakRead()

	def read(self, amt=None):
		"""
			A more 'file-like' read from the queue:

			- If `amt` is None, read all.
			- If `amt` is an integer, read exactly `amt` bytes.
			- Handles leftover data from previous chunk to avoid losing bytes.
		"""
		if self.closed:
			return b""

		if self.error:
			raise self.error

		# If amt is None, read everything:
		if amt is None:
			return self._read_all()

		# If amt is specified (and possibly 0 or > 0)
		return self._read_amt(amt)

	def _read_all(self):
		"""
			Read *all* remaining data from leftover + queue
		"""
		out = bytearray()

		# If there's leftover data, use it first
		out.extend(self._leftover)
		self._leftover.clear()

		# Then read new chunks until we hit None or are closed
		while not self.closed and not self.quit_event.is_set():
			if self.error:
				raise self.error
			
			try:
				chunk = self._dequeue_chunks()
			except QueueBreakRead:
				break
			except QueueContinueRead:
				continue
			
			if chunk is None:
				# End of stream. Close here?
				if self.perform_finished.is_set():
					self.close()
				break

			out.extend(chunk)
		return bytes(out)

	def _read_amt(self, amt):
		"""
			Read exactly `amt` bytes. Returns up to `amt`.
		"""
		out = bytearray()
		needed = amt

		# First, consume leftover if available
		if self._leftover:
			take = min(needed, len(self._leftover))
			out.extend(self._leftover[:take])
			del self._leftover[:take]
			needed -= take

		
		# Read additional chunks from the queue if we still need data
		while needed > 0 and not self.closed and not self.quit_event.is_set():
			if self.error:
				raise self.error

			try:
				chunk = self._dequeue_chunks()
			except QueueBreakRead:
				break
			except QueueContinueRead:
				continue

			if chunk is None:
				# End of stream. close here?
				if self.perform_finished.is_set():
					self.close()
				break

			# If the chunk is bigger than needed, take part of it
			# and store the remainder in _leftover.
			if len(chunk) > needed:
				out.extend(chunk[:needed])
				self._leftover.extend(chunk[needed:])
				needed = 0
			else:
				# Chunk fits entirely
				out.extend(chunk)
				needed -= len(chunk)
		
		return bytes(out)

	def flush(self):
		pass
	
	def close(self):
		'''
			Signal to stop the streaming and wait for the task to complete.
		'''
		if self.closed:
			return

		self.quit_event.set()
		
		if not self.perform_finished.is_set():
			raise Exception("Curl perform is not finished yet, cannot close.")
		
		self.closed = True

	def __del__(self):
		'''
			Destructor to ensure the response is properly closed when garbage-collected.
		'''
		if not self.closed:
			self.close()
	
	def __exit__(self, *args):
		if not self.closed:
			self.close()
