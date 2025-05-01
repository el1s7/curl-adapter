import queue
import select
import sys
import threading
import time
import typing
import traceback
import warnings

import pycurl
import curl_cffi.curl
from curl_cffi.curl import CurlOpt
from curl_cffi._wrapper import ffi, lib
from curl_cffi.aio import CURLMSG_DONE

def _detect_environment() -> typing.Tuple[str, typing.Callable]:
	## -eventlet-
	if "eventlet" in sys.modules:
		try:
			import eventlet
			from eventlet.patcher import is_monkey_patched as is_eventlet
			import socket
			import eventlet.event

			if is_eventlet(socket):
				return ("eventlet", eventlet.sleep, eventlet.event.Event)

		except ImportError:
			pass

	# -gevent-
	if "gevent" in sys.modules:
		try:
			import gevent
			from gevent import socket as _gsocket
			import socket
			import gevent.event

			if socket.socket is _gsocket.socket:
				return ("gevent", gevent.sleep, gevent.event.Event)
		except ImportError:
			pass

	return ("default", time.sleep, threading.Event)

_THREAD_ENV, _THREAD_SLEEP, _THREAD_EVENT = _detect_environment()

class CurlStreamHandler():
	"""
		Curl Stream Handler

		:copyright: (c) 2025 by Elis K.
	"""

	def __init__(self, curl_instance: typing.Union[curl_cffi.Curl, pycurl.Curl], callback_after_perform=None, timeout=None, debug=False):
		'''
			Initialize the stream handler.
		'''
		self.curl = curl_instance
		self.chunk_queue = queue.Queue()  # Thread-safe queue for streaming data
		self.quit_event: threading.Event = _THREAD_EVENT()  # Signal to stop streaming
		self.error = None  # Store errors encountered during streaming
		self._future = None  # To track the task execution
		self.closed = False
		self.initialized: threading.Event = _THREAD_EVENT() #Event to set when we receive the first bytes of body, that's how we know that the headers are ready
		self.perform_finished: threading.Event = _THREAD_EVENT()
		self.callback_after_perform = callback_after_perform
		self._leftover = bytearray()  # buffer for leftover data when chunk > requested
		self.timeout = timeout[1] if isinstance(timeout, tuple) else timeout
		self.debug = debug

		self.curl_multi = None

	def _write_callback(self, chunk):
		'''
			Callback to handle incoming data chunks.
		'''	
		if not self.initialized.is_set():
			# First chunk = headers ready
			self.initialized.set()
		
		if self.quit_event.is_set():
			return -1  # Signal to stop

		self.chunk_queue.put(chunk)  # Add chunk to the queue
		return len(chunk)

	def _perform_deprecated(self):
		'''
			The initial basic way to call perform (using thread spawns), this is now done in a more complex but better way by using `curl_multi`s.
		'''
		t0 = time.time()
		
		try:
			self.curl.setopt(CurlOpt.WRITEFUNCTION, self._write_callback)
			self.curl.perform()
		
		except Exception as e: #(CurlError, pycurl.error)
			self.error = e
		finally:

			self.chunk_queue.put(None)  # End of stream

			try:
				if self.callback_after_perform and callable(self.callback_after_perform):
					self.callback_after_perform()
			except Exception as e:
				if self.debug:
					traceback.print_exc()
				pass
			
			self.curl.clean_after_perform()

			self.perform_finished.set()

			# Set to avoid blocking 
			if not self.initialized.is_set():
				self.initialized.set()

			if self.debug:
				print(f"[DEBUG] Curl Perform Elapsed Time: {time.time() - t0:.3f}s")

	def _perform_curl_cffi(self):
		'''
			CurlCffi multi perform
		'''

		#  This functions reads & saves data to our queue, one chunk at a time. Non‐blocking pass over sockets.
		err_code = lib.curl_multi_perform(self.curl_multi, self.curl_multi_running_pointer)

		transfer_complete = False

		# Get any messages for curl handle
		if (not err_code or err_code == -1):
			try:
				msgq = ffi.new("int *", 0)
				while True:
					msg = lib.curl_multi_info_read(self.curl_multi, msgq)
					if msg == ffi.NULL:
						break	
					# CURLMSG_DONE == transfer complete
					if msg.msg == CURLMSG_DONE:
						# translate the numeric code into a CurlError
						transfer_complete = True
						if msg.data.result != 0:
							self.error = self.curl._get_error(msg.data.result, "perform")
			except:
				if self.debug:
					traceback.print_exc()
		else:
			self.error = self.curl._get_error(err_code, "perform")
						
		if (
			transfer_complete
			or self.error
			or not self.curl_multi_running_pointer[0] 
			or self.quit_event.is_set()
		):

			if self.debug:
				print(f"[DEBUG] Closing curl. transfer_complete: {transfer_complete}, error: {bool(self.error)}, running: {bool(self.curl_multi_running_pointer[0])}, quit_event: {self.quit_event.is_set()}")
			
			try:
				lib.curl_multi_remove_handle(self.curl_multi, self.curl._curl)
				lib.curl_multi_cleanup(self.curl_multi)
			except Exception:
				if self.debug:
					traceback.print_exc()
			finally:
				self.curl_multi_running_pointer = None
				self.curl_multi = None
			
			# signal end of stream
			self.chunk_queue.put(None)

			if callable(self.callback_after_perform):
				try:
					self.callback_after_perform()
				except Exception:
					if self.debug:
						traceback.print_exc()
			
			self.perform_finished.set()
			self.initialized.set()
			
	def _perform_pycurl(self):
		'''
			Pycurl multi perform
		'''
		err_code, running = self.curl_multi.perform()

		transfer_complete = False
		
		if running and (not err_code or err_code == pycurl.E_CALL_MULTI_PERFORM):
			
			# Blocking poll sleep before next call
			r, w, x = self.curl_multi.fdset()
			ms = self.curl_multi.timeout()
			timeout = max(ms, 0) / 1000.0
			select.select(r, w, x, timeout)

			# I don't know why we need to run perform again straight away after socket select, but it works for reading the error messages, so let's leave it like that
			err_code, running = self.curl_multi.perform()

		
		if (not err_code or err_code == pycurl.E_CALL_MULTI_PERFORM):
			try:
				# Check curl handle messages
				while True:
					num_q, ok_list, err_list = self.curl_multi.info_read()
					
					if len(ok_list):
						transfer_complete = True

					if len(err_list):
						# err_list is a list of (handle, errno, errmsg)
						_, errno, errmsg = err_list[0]
						self.error = pycurl.error(errno, errmsg)
					
					if num_q == 0:
						break
				
			except:
				if self.debug:
					traceback.print_exc()
		else:
			self.error = pycurl.error(err_code, "perform")
			

		if (
			transfer_complete
			or not running 
			or self.error
			or self.quit_event.is_set()
		):

			if self.debug:
				print(f"[DEBUG] Closing curl. transfer_complete: {transfer_complete}, error: {bool(self.error)}, running: {bool(running)}, quit_event: {self.quit_event.is_set()}")
			
			try:
				self.curl_multi.remove_handle(self.curl)
				self.curl_multi.close()
			except Exception:
				if self.debug:
					traceback.print_exc()
			finally:
				self.curl_multi = None

			# signal end of stream
			self.chunk_queue.put(None)

			if callable(self.callback_after_perform):
				try:
					self.callback_after_perform()
				except Exception:
					if self.debug:
						traceback.print_exc()
			
			self.perform_finished.set()
			self.initialized.set()
	
	def _perform_read(self):
		if not self.curl_multi:
			raise Exception("Curl perform is not running.")

		if isinstance(self.curl, curl_cffi.Curl):
			return self._perform_curl_cffi()
			
		if isinstance(self.curl, pycurl.Curl):
			return self._perform_pycurl()

		raise TypeError("Cannot perform on invalid Curl object.")
	
	def _wait_for_headers(self):
		while not self.initialized.is_set():
			self._perform_read()
			_THREAD_SLEEP(0)
			pass

	def _wait_for_body(self):
		ts = time.time()
		warned = False
		
		while not self.perform_finished.is_set():

			if time.time() - ts > 5 and not warned:
				warnings.warn("""
					This method is a blocking call, it's only returned after the whole response body has been parsed. 
					If you are streaming a HTTP response and reading it into chunks (e.g. downloading a file), it's better to call this method *after* reading the body.
				""")
				warned = True
			
			self._perform_read()
			_THREAD_SLEEP(0)
			pass
	
	def _perform(self):
		
		t0 = time.time()

		# Initialize curl multi
		if not self.curl_multi:
			if isinstance(self.curl, curl_cffi.Curl):

				self.curl.setopt(CurlOpt.WRITEFUNCTION, self._write_callback)
				self.curl._ensure_cacert()

				# Init Multi
				self.curl_multi = lib.curl_multi_init()
				lib.curl_multi_add_handle(self.curl_multi, self.curl._curl)

				# running flag
				self.curl_multi_running_pointer = ffi.new("int *", 0)

			elif isinstance(self.curl, pycurl.Curl):
				self.curl.setopt(pycurl.WRITEFUNCTION, self._write_callback)

				# Init Multi
				self.curl_multi = pycurl.CurlMulti()
				self.curl_multi.add_handle(self.curl)
			else:
				raise TypeError("Cannot perform on invalid Curl object.")

		# Wait for headers
		self._wait_for_headers()

		if self.debug:
			print(f"[DEBUG] Curl perform elapsed: {time.time() - t0:.3f}s | Stream finished?: {self.perform_finished.is_set()}")

	def start(self):
		
		self._perform()

		return self

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
			
			if not self.perform_finished.is_set():
				self._perform_read()

			if self.error:
				raise self.error

			try:
				chunk = self.chunk_queue.get_nowait()
			except queue.Empty:
				# No data currently available
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
			
			if not self.perform_finished.is_set():
				self._perform_read()

			if self.error:
				raise self.error

			try:
				chunk = self.chunk_queue.get_nowait()
			except queue.Empty:
				# Temporarily no data
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
		#self._leftover.clear()
		pass
	
	def close(self):
		'''
			Signal to stop the streaming and wait for the task to complete.
		'''
		if self.closed:
			return
		
		self.quit_event.set()

		if not self.perform_finished.is_set():
			self.perform_finished.wait()

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