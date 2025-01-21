from concurrent.futures import ThreadPoolExecutor
import queue
import threading
import typing

import pycurl
import curl_cffi.curl
from curl_cffi.curl import CurlOpt, CurlError

class CurlStreamHandler():
	"""
		Curl Stream Handler

		:copyright: (c) 2025 by Elis K.
	"""


	def __init__(self, curl_instance: typing.Union[curl_cffi.Curl, pycurl.Curl], executor: ThreadPoolExecutor=None, callback_after_perform=None):
		'''
			Initialize the stream handler.
		'''
		self.curl = curl_instance
		self.executor = executor or ThreadPoolExecutor()
		self.chunk_queue = queue.Queue()  # Thread-safe queue for streaming data
		self.quit_event = threading.Event()  # Signal to stop streaming
		self.error = None  # Store errors encountered during streaming
		self._future = None  # To track the task execution
		self.closed = False
		self.initialized = threading.Event() #Event to set when we receive the first bytes of body, that's how we know that the headers are ready
		self.allow_cleanup = threading.Event()
		self.perform_finished = threading.Event()
		self.callback_after_perform = callback_after_perform
		self._leftover = bytearray()  # buffer for leftover data when chunk > requested

	def _write_callback(self, chunk):
		'''
			Callback to handle incoming data chunks.
		'''
		if not self.initialized.is_set():
			self.initialized.set()
		if self.quit_event.is_set():
			return -1  # Signal to stop

		self.chunk_queue.put(chunk)  # Add chunk to the queue
		return len(chunk)

	def _download(self):

		# Possible to set buffer size here
		# self.curl.setopt(CurlOpt.BUFFERSIZE, 8 * 1024)
		self.curl.setopt(CurlOpt.WRITEFUNCTION, self._write_callback)

		try:
			self.curl.perform()
		except (CurlError, pycurl.error) as e:
			self.error = e
		finally:
			self.chunk_queue.put(None)  # End of stream

			if self.callback_after_perform and callable(self.callback_after_perform):
				self.callback_after_perform()
			
			self.perform_finished.set()

			# Set to avoid blocking 
			if not self.initialized.is_set():
				self.initialized.set()

			
	def start(self):
		self._future = self.executor.submit(self._download)
		if self.error:
			raise self.error

		return self

	def wait_for_headers(self):
		'''
			Wait until headers are available (simply checking if we have started to read the body)
		'''
		self.initialized.wait()
		return self

	def set_headers_parsed(self):
		return self.allow_cleanup.set()

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
		while not self.closed:
			if self.error:
				raise self.error

			try:
				chunk = self.chunk_queue.get(timeout=1)
			except queue.Empty:
				# No data currently available
				break
			
			if chunk is None:
				# End of stream. Close here?
				if self.perform_finished.is_set():
					self.close()
				break

			out.extend(chunk)

			if self.quit_event.is_set():
				break

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
		while needed > 0 and not self.closed:
			if self.error:
				raise self.error

			try:
				chunk = self.chunk_queue.get(timeout=1)
			except queue.Empty:
				# Temporarily no data
				break

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

			if self.quit_event.is_set():
				break

		return bytes(out)

	def flush(self):
		#self._leftover.clear()
		pass
	
	def close(self):
		'''
			Signal to stop the streaming and wait for the task to complete.
		'''
		if self.closed:
			return
		
		self.closed = True

		self.quit_event.set()
		if self._future:
			self._future.result()  # Ensure the task completes

		# Clean up curl handle if needed
		# self.curl.close()
		self.allow_cleanup.wait(timeout=1)
		self.curl.reset()
			
	def __del__(self):
		'''
			Destructor to ensure the response is properly closed when garbage-collected.
		'''
		if not self.closed:
			self.close()
	
	def __exit__(self, *args):
		if not self.closed:
			self.close()