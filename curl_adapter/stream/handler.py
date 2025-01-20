from concurrent.futures import ThreadPoolExecutor
import queue
import threading
import typing

import pycurl
import curl_cffi.curl
from curl_cffi.curl import CurlOpt, CurlError

class CurlStreamHandler():
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
		
	def _write_callback(self, chunk):
		'''
		    Callback to handle incoming data chunks.
		'''
		if not self.initialized.is_set():
			self.initialized.set()
		if self.quit_event.is_set():
			return -1  # Signal to stop

		self.chunk_queue.put_nowait(chunk)  # Add chunk to the queue
		return len(chunk)

	def _download(self):

		self.curl.setopt(CurlOpt.WRITEFUNCTION, self._write_callback)

		try:
			self.curl.perform()
		except (CurlError, pycurl.error) as e:
			self.error = e
		finally:
			self.chunk_queue.put(None)  # End of stream

			self.perform_finished.set()

			# Set to avoid blocking 
			if not self.initialized.is_set():
				self.initialized.set()

			if self.callback_after_perform and callable(self.callback_after_perform):
				self.callback_after_perform()
			
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
		'''
			Read data from the queue in chunks. Returns a single chunk or all available data if amt is None.
		'''
		if amt is None:
			data = []
			while True:
				if self.error:
					raise self.error
				try:
					chunk = self.chunk_queue.get(timeout=1)
					if chunk is None:  # End of stream
						break
					data.append(chunk)
				except queue.Empty:
					if self.quit_event.is_set():
						break
			return b"".join(data)
		else:
			if self.error:
				raise self.error
			try:
				chunk = self.chunk_queue.get(timeout=1)
				if chunk is None:  # End of stream
					return b""
				return chunk[:amt]
			except queue.Empty:
				return b""

	def flush(self):
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