import traceback

import pycurl
import curl_cffi.curl

from curl_cffi.curl import CurlOpt
from curl_cffi._wrapper import ffi, lib

from .base import (
	CurlStreamHandlerBase, 
	QueueContinueRead, 
	QueueBreakRead
)

from ._thread_env import (
	_THREAD_ENV, _THREAD_CLASS, _THREAD_SLEEP, _THREAD_EVENT, _THREAD_QUEUE_MODULE
)


class CurlStreamHandlerMulti(CurlStreamHandlerBase):
	'''
		Curl Stream Handler (c) 2025 by Elis K.

		Using curl's multi interface and Python's coroutines.
	'''
	CURLMSG_DONE = 1

	def _perform_multi_curl_cffi(self):
		'''
			CurlCffi multi perform
		'''

		#  This functions reads & saves data to our queue, one chunk at a time. Non‐blocking pass over sockets.
		err_code = lib.curl_multi_perform(self.curl_multi, self.curl_multi_running_pointer)

		transfer_complete = False

		# Get any messages for curl handle
		if (not err_code or err_code == -1):
			
			# ask for the next timeout
			if self.curl_multi_running_pointer[0]:
				try:
					tptr = ffi.new("long *")
					lib.curl_multi_timeout(self.curl_multi, tptr)
					ms = tptr[0]
					if ms < 0:
						ms = 1000   # or whatever “default” poll interval you like
   					# block here until either a socket event *or* the timeout elapses
					nfds = ffi.new("int *", 0)
					lib.curl_multi_wait(self.curl_multi, ffi.NULL, 0, int(ms), nfds)
				except Exception as e:
					if self.debug:
						traceback.print_exc()

			# drain messages
			try:
				msgq = ffi.new("int *", 0)
				while True:
					msg = lib.curl_multi_info_read(self.curl_multi, msgq)
					if msg == ffi.NULL:
						break	
					# CURLMSG_DONE == transfer complete
					if msg.msg == self.CURLMSG_DONE:
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
			
			self._cleanup_after_perform()
			
	def _perform_multi_pycurl(self):
		'''
			Pycurl multi perform
		'''
		err_code, running = self.curl_multi.perform()

		transfer_complete = False
		
		if running and (not err_code or err_code == pycurl.E_CALL_MULTI_PERFORM):
			
			# Blocking poll sleep before next call
			ms = self.curl_multi.timeout()
			if ms < 0:
				ms = 1000
			self.curl_multi.select(ms / 1000.0)

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
			
			self._cleanup_after_perform()
	
	def _cleanup_after_perform(self):
		try:

			if self.curl_type == "pycurl":
				self.curl_multi.remove_handle(self.curl)
			
			elif self.curl_type == "curl_cffi":
				lib.curl_multi_remove_handle(self.curl_multi, self.curl._curl)

		except Exception:
			if self.debug:
				traceback.print_exc()
		finally:

			if self.curl_type == "pycurl":
				self.curl_multi.close()
			elif self.curl_type == "curl_cffi":
				lib.curl_multi_cleanup(self.curl_multi)
			
			self.curl_multi_running_pointer = None
			self.curl_multi = None
		
		return super()._cleanup_after_perform()

	def _perform_multi_read(self):
		if not self.curl_multi:
			raise Exception("Curl perform is not running.")

		if isinstance(self.curl, curl_cffi.Curl):
			return self._perform_multi_curl_cffi()

		if isinstance(self.curl, pycurl.Curl):
			return self._perform_multi_pycurl()

		raise TypeError("Cannot perform on invalid Curl object.")
	
	def _dequeue_chunks(self):

		if not hasattr(self, '_body_gen'):
			# Body stream
			self._body_gen = self._read_body()
		
		try:
			next(self._body_gen)
		except StopIteration:
			pass

		if self.error:
			raise self.error

		try:
			chunk = self.chunk_queue.get_nowait()
			return chunk
		except _THREAD_QUEUE_MODULE.Empty:
			# No data available anymore, break
			raise QueueContinueRead()

	def _read_headers(self):
		while not self.initialized.is_set():
			self._perform_multi_read()
			yield
			
	def _read_body(self):
		while not self.perform_finished.is_set():
			self._perform_multi_read()
			yield

	def _wait_for_headers(self):
		try:
			for _ in self._read_headers():
				pass
		except StopIteration:
			pass
	
	def _wait_for_body(self):
		try:
			for _ in self._read_body():
				pass
		except StopIteration:
			pass
	
	def _perform(self):
		if self.debug:
			print("[DEBUG] Using Curl Multi Stream Handler.")
			
		# Initialize curl multi
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
