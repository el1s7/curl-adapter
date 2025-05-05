import collections
from contextlib import contextmanager
import io

from urllib3.util import parse_url
from urllib3.response import HTTPResponse
from urllib3._collections import HTTPHeaderDict

import requests

from http.client import HTTPMessage

import email.parser
import typing
from .handler import CurlStreamHandler, CurlStreamHandlerBase


class BytesQueueBuffer:
    """
	this class is sourced from urllib3 HTTPResponse. It's needed to support newer versions of urllib3
	------------------------------------------
	Memory-efficient bytes buffer

    To return decoded data in read() and still follow the BufferedIOBase API, we need a
    buffer to always return the correct amount of bytes.

    This buffer should be filled using calls to put()

    Our maximum memory usage is determined by the sum of the size of:

     * self.buffer, which contains the full data
     * the largest chunk that we will copy in get()

    The worst case scenario is a single chunk, in which case we'll make a full copy of
    the data inside get().
    """

    def __init__(self) -> None:
        self.buffer: typing.Deque[bytes] = collections.deque()
        self._size: int = 0

    def __len__(self) -> int:
        return self._size

    def put(self, data: bytes) -> None:
        self.buffer.append(data)
        self._size += len(data)

    def get(self, n: int) -> bytes:
        if n == 0:
            return b""
        elif not self.buffer:
            raise RuntimeError("buffer is empty")
        elif n < 0:
            raise ValueError("n should be > 0")

        fetched = 0
        ret = io.BytesIO()
        while fetched < n:
            remaining = n - fetched
            chunk = self.buffer.popleft()
            chunk_length = len(chunk)
            if remaining < chunk_length:
                left_chunk, right_chunk = chunk[:remaining], chunk[remaining:]
                ret.write(left_chunk)
                self.buffer.appendleft(right_chunk)
                self._size -= remaining
                break
            else:
                ret.write(chunk)
                self._size -= chunk_length
            fetched += chunk_length

            if not self.buffer:
                break

        return ret.getvalue()

    def get_all(self) -> bytes:
        buffer = self.buffer
        if not buffer:
            assert self._size == 0
            return b""
        if len(buffer) == 1:
            result = buffer.pop()
        else:
            ret = io.BytesIO()
            ret.writelines(buffer.popleft() for _ in range(len(buffer)))
            result = ret.getvalue()
        self._size = 0
        return result
		
class MockOriginalResponse():
	'''
		Mock the http.client -> urllib3 'original response' object class
	'''

	def __init__(self, url:str, method: str, header_list: typing.List[bytes]):
		self.url = url
		self._method = method

		header_list_str = b'\n'.join(header_list).decode('iso-8859-1')
		self.msg = self.headers = email.parser.Parser(_class=HTTPMessage).parsestr(
				header_list_str
			)

	def info(self):
		return self.headers

	def close(self):
		pass

class CurlStreamResponse(HTTPResponse):
	'''
		Modified urllib3 HTTPResponse 
	'''
	def __init__(
		self,

		curl_stream_handler: CurlStreamHandlerBase,
		request: requests.PreparedRequest=None,
		url=None, #URL
		method=None, #Method
		use_curl_content_decoding=None,

		headers: HTTPHeaderDict=None, #Headers
		header_list: typing.List[bytes]=None,
		status=0, #HTTP Status Code
		reason=None, #HTTP Reason
		version=None, #HTTP Version header

		preload_content=False,
		enforce_content_length=True,
		auto_close=True,
	):

		if curl_stream_handler.error:
			raise curl_stream_handler.error
		

		self.headers = headers
		self.status = status
		self.reason = reason
		self.version = version
		
		self._prepared_request = request
		self._request_url = url

		# curl automatically decodes depending on accept header, unless this behaviour is specifically disabled

		self._handle_content_decoding = bool(
			not use_curl_content_decoding
			or self._prepared_request.headers.get("Accept", None) is None 
		)

		self.decode_content = self._handle_content_decoding
		self.enforce_content_length = enforce_content_length

		if not self._handle_content_decoding:
			# In cases when curl is handling content decoding, disable content length checks otherwise we might get unexcepted errors
			self.enforce_content_length = False
		
		self.auto_close = auto_close

		self._decoder = None

		self._fp = curl_stream_handler
		self._fp_bytes_read = 0

		self._connection = None
		self._pool = None
		self.retries = None
		self.strict = 0

		self._original_response = MockOriginalResponse(url, method, header_list)

		# Are we using the chunked-style of transfer encoding?
		self.chunked = False
		self.chunk_left = None
		tr_enc = self.headers.get("transfer-encoding", "").lower()
		# Don't incur the penalty of creating a list and then discarding it
		encodings = (enc.strip() for enc in tr_enc.split(","))
		if "chunked" in encodings:
			self.chunked = True
		
		# Determine length of response
		self.length_remaining = self._init_length(method)

		self._decoded_buffer = BytesQueueBuffer()

		# If requested, preload the body.
		self._body = None #Raw data bytes
		if preload_content and not self._body:
			self._body = self.read(decode_content=self.decode_content)

	
	@contextmanager
	def _error_catcher(self):
		try:
			yield
		finally:
			# Shouldn't close here, throws error
			#self.close()
			pass

	def _decode(self, data, decode_content, flush_decoder):
		"""
			Curl automatically decodes content if the "Accept" header is present.
		"""
		if not self._handle_content_decoding:
			return data

		return super()._decode(data, decode_content, flush_decoder)


	def shutdown(self, *args, **kwargs):
		self._fp.close()
		pass

	def release_conn(self, *args, **kwargs):
		#nothing to release with curl...
		self._fp.close()
		pass

	def drain_conn(self, *args, **kwargs):
		"""
		Read and discard any remaining HTTP response data in the response connection.
		
		Unread data in the HTTPResponse connection blocks the connection from being released back to the pool.
		try:
			self.read()
		except (HTTPError, SocketError, BaseSSLError, HTTPException):
			pass
		"""
		
		raise NotImplementedError()

	@property
	def connection(self):
		raise NotImplementedError()

	@classmethod
	def from_httplib(self, *args, **kwargs):
		raise NotImplementedError()