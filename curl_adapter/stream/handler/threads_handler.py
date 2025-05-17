from .base import (
	CurlStreamHandlerBase, 
	QueueContinueRead, 
	QueueBreakRead,
)

from ._thread_env import (
	_THREAD_ENV, _THREAD_CLASS, _THREAD_SLEEP, _THREAD_EVENT, _THREAD_QUEUE_MODULE
)


class CurlStreamHandlerThreads(CurlStreamHandlerBase):
	'''
		Curl Stream Handler (c) 2025 by Elis K.

		Uses threads (either native, or gevent/eventlet) to spawn curl perform in a non-blocking way.
	'''

	def _wait_for_headers(self):
		if self.debug:
			print("[DEBUG] Waiting for headers")
		_done = self.initialized.wait(timeout=self.event_timeout)

		if not _done:
			raise self.read_timeout_error

	def _wait_for_body(self):
		if self.debug:
			print("[DEBUG] Waiting for body")
		self.perform_finished.wait()

	def _perform(self):
		if self.debug:
			print("[DEBUG] Using Curl Threads Stream Handler.")

		from concurrent.futures import ThreadPoolExecutor

		basic_perform = super()._perform

		if _THREAD_ENV == "gevent":
			_THREAD_CLASS.get_hub().threadpool.spawn(basic_perform).get()

		elif _THREAD_ENV == "eventlet":
			_THREAD_CLASS.tpool.execute(basic_perform)
		
		else:
			self.executor = ThreadPoolExecutor()
			self._future = self.executor.submit(basic_perform)

	def close(self):
		if self.closed:
			return
		
		if self._future:
			self._future.result() 

		return super().close()
