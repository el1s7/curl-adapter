from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import math
import os
import re
import threading
import time
import traceback
from typing import TypedDict
import typing
import warnings

from urllib3.util import parse_url
from urllib3.response import HTTPResponse
from urllib3._collections import HTTPHeaderDict

import requests

from requests.utils import (
	DEFAULT_CA_BUNDLE_PATH,
	extract_zipped_paths,
	get_auth_from_url,
	get_encoding_from_headers,
	prepend_scheme_if_needed,
	select_proxy,
	urldefragauth,
)

from requests.compat import urlparse
from requests.structures import CaseInsensitiveDict
from requests.cookies import extract_cookies_to_jar
from requests.adapters import BaseAdapter
from requests.models import Response
from requests.exceptions import (
	RequestException,
	ConnectionError,
	ConnectTimeout,
	InvalidHeader,
	InvalidProxyURL,
	InvalidSchema,
	InvalidURL,
	ProxyError,
	ReadTimeout,
	RetryError,
	SSLError,
	HTTPError,
	TooManyRedirects,
	ContentDecodingError,
	ChunkedEncodingError
)
import pycurl
import curl_cffi.curl
from curl_cffi.curl import CurlInfo as CurlInfoOpt, CurlOpt, CurlError 
from curl_cffi.const import CurlECode, CurlHttpVersion

from .stream.handler import CurlStreamHandler, CurlStreamHandlerBase
from .stream.response import CurlStreamResponse

class CurlInfo(TypedDict):
	local_ip: str
	local_port: int
	primary_ip: str
	primary_port: int

	speed_download: int
	speed_upload: int

	request_size: int
	request_body_size: int
	response_body_size: int
	response_header_size: int

	ssl_verify_result: int
	proxy_ssl_verify_result: int

	total_time: int
	starttransfer_time: int
	connect_time: int
	appconnect_time: int
	pretransfer_time: int
	namelookup_time: int
	has_used_proxy: int

class Response(requests.Response):
	curl_info: CurlInfo
	wait_for_body: typing.Callable[[], None]

class BaseCurlAdapter(BaseAdapter):
	
	_local = threading.local()

	def __init__(self, 
		curl_class: typing.Union[curl_cffi.Curl, pycurl.Curl],
		debug=False, 
		use_curl_content_decoding=False,
		use_thread_local_curl=True,
		stream_handler: CurlStreamHandlerBase=None
	):
		
		self.curl_class: typing.Union[curl_cffi.Curl, pycurl.Curl] = curl_class
		self.debug = debug
		self.use_curl_content_decoding = use_curl_content_decoding
		
		self.use_thread_local_curl = use_thread_local_curl

		self.stream_handler = (stream_handler or CurlStreamHandler)

		if self.use_thread_local_curl:
			self._local.curl = self.curl_class()
		else:
			self._curl = self.curl_class()

		if self.debug:
			self.enable_debug()

	@property
	def curl(self) -> typing.Union[curl_cffi.Curl, pycurl.Curl]:
		if self.use_thread_local_curl:
			if not getattr(self._local, "curl", None):
				self._local.curl = self.curl_class()
			return self._local.curl
		return self._curl
	
	def reset_curl(self):
		'''
			Close current handle and open a new curl handle
		'''
		self.curl.close()

		if self.use_thread_local_curl:
			self._local.curl = self.curl_class()
		else:
			self._curl = self.curl_class()

	def enable_debug(self):
		if self.debug:
			self.curl.setopt(CurlOpt.VERBOSE, 1)
		
	def cert_verify(self, curl, url: str, verify: bool, cert):
		"""
		Verify an SSL certificate for HTTPS requests.
		"""

		if url.lower().startswith("https"):
			if verify is True:
				curl.setopt(CurlOpt.SSL_VERIFYPEER, 1)
				curl.setopt(CurlOpt.SSL_VERIFYHOST, 1)
			elif isinstance(verify, str):
				if not os.path.exists(verify):
					raise OSError(
						f"Could not find a suitable TLS CA certificate bundle at: {verify}"
					)
				curl.setopt(CurlOpt.CAINFO, verify)
			else:
				curl.setopt(CurlOpt.SSL_VERIFYPEER, 0)
				curl.setopt(CurlOpt.SSL_VERIFYHOST, 0)

			if cert:
				if isinstance(cert, (list, tuple)) and len(cert) == 2:
					cert_file, key_file = cert
					if not os.path.exists(cert_file):
						raise OSError(
							f"Could not find the TLS certificate file at: {cert_file}"
						)
					if not os.path.exists(key_file):
						raise OSError(f"Could not find the TLS key file at: {key_file}")
					curl.setopt(CurlOpt.SSLCERT, cert_file)
					curl.setopt(CurlOpt.SSLKEY, key_file)
				elif isinstance(cert, str):
					if not os.path.exists(cert):
						raise OSError(
							f"Could not find the TLS certificate file at: {cert}"
						)
					curl.setopt(CurlOpt.SSLCERT, cert)
				else:
					raise ValueError("Invalid SSL certificate format.")
		else:
			curl.setopt(CurlOpt.SSL_VERIFYPEER, 0)
			curl.setopt(CurlOpt.SSL_VERIFYHOST, 0)

	CODE2ERROR = {
			0: RequestException,
			CurlECode.UNSUPPORTED_PROTOCOL: InvalidSchema,
			CurlECode.URL_MALFORMAT: InvalidURL,
			CurlECode.COULDNT_RESOLVE_PROXY: ProxyError,
			CurlECode.COULDNT_RESOLVE_HOST: ConnectionError, #DNSError
			CurlECode.COULDNT_CONNECT: ConnectionError,
			CurlECode.WEIRD_SERVER_REPLY: ConnectionError,
			CurlECode.REMOTE_ACCESS_DENIED: ConnectionError,
			CurlECode.HTTP2: HTTPError,
			CurlECode.HTTP_RETURNED_ERROR: HTTPError,
			CurlECode.WRITE_ERROR: RequestException,
			CurlECode.READ_ERROR: RequestException,
			CurlECode.OUT_OF_MEMORY: RequestException,
			CurlECode.OPERATION_TIMEDOUT: ConnectTimeout, #Timeout,
			CurlECode.SSL_CONNECT_ERROR: SSLError,
			CurlECode.INTERFACE_FAILED: RequestException, #InterfaceError,
			CurlECode.TOO_MANY_REDIRECTS: TooManyRedirects,
			CurlECode.UNKNOWN_OPTION: RequestException,
			CurlECode.SETOPT_OPTION_SYNTAX: RequestException,
			CurlECode.GOT_NOTHING: ConnectionError,
			CurlECode.SSL_ENGINE_NOTFOUND: SSLError,
			CurlECode.SSL_ENGINE_SETFAILED: SSLError,
			CurlECode.SEND_ERROR: ConnectionError,
			CurlECode.RECV_ERROR: ConnectionError,
			CurlECode.SSL_CERTPROBLEM: SSLError,
			CurlECode.SSL_CIPHER: SSLError,
			CurlECode.PEER_FAILED_VERIFICATION: SSLError, #CertificateVerifyError,
			CurlECode.BAD_CONTENT_ENCODING: ContentDecodingError,
			CurlECode.SSL_ENGINE_INITFAILED: SSLError,
			CurlECode.SSL_CACERT_BADFILE: SSLError,
			CurlECode.SSL_CRL_BADFILE: SSLError,
			CurlECode.SSL_ISSUER_ERROR: SSLError,
			CurlECode.SSL_PINNEDPUBKEYNOTMATCH: SSLError,
			CurlECode.SSL_INVALIDCERTSTATUS: SSLError,
			CurlECode.HTTP2_STREAM: HTTPError,
			CurlECode.HTTP3: HTTPError,
			CurlECode.QUIC_CONNECT_ERROR: ConnectionError,
			CurlECode.PROXY: ProxyError,
			CurlECode.SSL_CLIENTCERT: SSLError,
			CurlECode.ECH_REQUIRED: SSLError,
			CurlECode.PARTIAL_FILE: ChunkedEncodingError,
	}

	def curl_error_map(self, error: typing.Union[CurlError, pycurl.error], has_proxy=None):
		
		err_code = 0
		if hasattr(error, 'code'):
			#curl_cffi.CurlError
			err_code = error.code

		elif len(error.args) > 0 and isinstance(error.args[0], int):
			#pycurl.error
			err_code = error.args[0]

		err_message = str(error)

		if err_code == CurlECode.RECV_ERROR and "CONNECT" in err_message and has_proxy:
			return ProxyError

		# Read timeout
		if err_code == 28 and ("Read timeout." in err_message or "Operation too slow." in err_message):
			return requests.exceptions.ReadTimeout
		
		return self.CODE2ERROR.get(err_code, RequestException)

	def get_curl_info(self, curl: typing.Union[curl_cffi.Curl, pycurl.Curl], option_code: int):
		try:
			return curl.getinfo(option_code)
		except:
			if self.debug:
				traceback.print_exc()
			return None
	def parse_info(self, curl: typing.Union[curl_cffi.Curl, pycurl.Curl], headers_only=False):

		additional_info = {
			# IP/Ports
			"local_ip": self.get_curl_info(curl, CurlInfoOpt.LOCAL_IP), 
			"local_port": self.get_curl_info(curl, CurlInfoOpt.LOCAL_PORT), 
			"primary_ip": self.get_curl_info(curl, CurlInfoOpt.PRIMARY_IP), 
			"primary_port": self.get_curl_info(curl, CurlInfoOpt.PRIMARY_PORT), 
			
			# Sizes
			"request_size": self.get_curl_info(curl, CurlInfoOpt.REQUEST_SIZE), # This is always 0 in Curl_Cffi
			"request_body_size": self.get_curl_info(curl, CurlInfoOpt.SIZE_UPLOAD_T), 
			"response_header_size": self.get_curl_info(curl, CurlInfoOpt.HEADER_SIZE),

			# SSL
			"ssl_verify_result": self.get_curl_info(curl, CurlInfoOpt.SSL_VERIFYRESULT),
			"proxy_ssl_verify_result": self.get_curl_info(curl, CurlInfoOpt.PROXY_SSL_VERIFYRESULT),

			# Times
			"starttransfer_time": self.get_curl_info(curl, CurlInfoOpt.STARTTRANSFER_TIME_T),
			"connect_time": self.get_curl_info(curl, CurlInfoOpt.CONNECT_TIME_T),
			"appconnect_time": self.get_curl_info(curl, CurlInfoOpt.APPCONNECT_TIME_T),
			"pretransfer_time": self.get_curl_info(curl, CurlInfoOpt.PRETRANSFER_TIME_T),
			"namelookup_time": self.get_curl_info(curl, CurlInfoOpt.NAMELOOKUP_TIME_T),

			# Other
			"has_used_proxy": self.get_curl_info(curl, CurlInfoOpt.USED_PROXY)
		}

		if not headers_only:
			# Available after the body has been parsed

			additional_info.update({
				"speed_download": self.get_curl_info(curl, CurlInfoOpt.SPEED_DOWNLOAD_T), 
				"speed_upload": self.get_curl_info(curl, CurlInfoOpt.SPEED_UPLOAD_T), 
				"response_body_size": self.get_curl_info(curl, CurlInfoOpt.SIZE_DOWNLOAD_T), 
				"total_time": self.get_curl_info(curl, CurlInfoOpt.TOTAL_TIME_T), 
			})
		
		return additional_info

	def parse_headers(self, curl: typing.Union[curl_cffi.Curl, pycurl.Curl], header_buffer: BytesIO):
		
		def parse_status_line(status_line: str) -> typing.List[str]:

			match = re.match(r"^HTTP/(\d|\d\.\d)\s+([0-9]{3})(?:\s+(.*))?$", status_line)

			if not match:
				return CurlHttpVersion.V1_0, 0, ""

			map_http_versions = {
				"2": CurlHttpVersion.V2_0,
				"2.0": CurlHttpVersion.V2_0,
				"1.1": CurlHttpVersion.V1_1,
				"1.0": CurlHttpVersion.V1_0,
				"1": CurlHttpVersion.V1_0
			}

			_http_version, _status_code, _reason = match.groups()
			
			http_version = (
				map_http_versions[_http_version] if _http_version in map_http_versions else CurlHttpVersion.V1_0
			)

			status_code = int(_status_code)

			reason = _reason or ""
				
			return http_version, status_code, reason

		http_version = CurlHttpVersion.V1_0
		status_code = 0
		reason: str = ""

		header_lines = header_buffer.getvalue().splitlines()
		
		header_list: list[bytes] = []
		for header_line in header_lines:
			if not header_line.strip():
				continue
			if header_line.startswith(b"HTTP/"):

				# read header from last response
				http_version, status_code, reason = parse_status_line(header_line.decode())

				# empty header list for new redirected response
				header_list = []
				continue
			if header_line.startswith(b" ") or header_line.startswith(b"\t"):
				header_list[-1] += header_line
				continue
			header_list.append(header_line)
		
		header_dict = HTTPHeaderDict()

		for header_item in header_list:			
			header_key, header_value = header_item.decode("ascii").split(":", maxsplit=1)
			header_dict.add(
				header_key.strip().title(), header_value.strip()
			)

		return {
			"version": http_version, 
			"status": status_code, 
			"reason": reason,
			"headers": header_dict,
			"header_list": header_list,
		}

	def build_response(self, 
		curl: typing.Union[curl_cffi.Curl, pycurl.Curl], 
		res:CurlStreamResponse, 
		parsed_headers: dict, 
		req: requests.PreparedRequest, 
		wait_for_body: callable,
		curl_info_dict: dict
	) -> Response:
		
		response = Response()

		# Fallback to None if there's no status_code, for whatever reason.
		response.status_code = parsed_headers["status"]

		# Make headers case-insensitive.
		response.headers = CaseInsensitiveDict(parsed_headers["headers"])

		# Set encoding.
		response.encoding = get_encoding_from_headers(response.headers)
		response.raw = res
		
		response.reason = parsed_headers["reason"]

		response.wait_for_body = wait_for_body

		response.curl_info = curl_info_dict

		if isinstance(req.url, bytes):
			response.url = req.url.decode("utf-8")
		else:
			response.url = req.url
		
	
		# Add new cookies from the server.
		extract_cookies_to_jar(response.cookies, req, res)

		# Give the Response some context.
		response.request = req
		response.connection = self

		return response

	def request_url(self, request: requests.PreparedRequest, proxies):
		"""
		Obtain the URL to use when making the final request.
		"""
		'''
		proxy = select_proxy(request.url, proxies)
		scheme = urlparse(request.url).scheme

		is_proxied_http_request = proxy and scheme != "https"
		using_socks_proxy = False
		if proxy:
			proxy_scheme = urlparse(proxy).scheme.lower()
			using_socks_proxy = proxy_scheme.startswith("socks")

		url = request.path_url
		if url.startswith("//"):  # Don't confuse Curl
			url = f"/{url.lstrip('/')}"

		if is_proxied_http_request and not using_socks_proxy:
			url = 

		'''

		return urldefragauth(request.url)
	
	def set_curl_options(self, 
			curl: typing.Union[curl_cffi.Curl, pycurl.Curl], 
			request: requests.PreparedRequest,
			url:str, 
			timeout, 
			proxies
		):
		
		if self.debug:
			print("[DEBUG] Sending: ", url, request.headers, timeout, proxies)
		# url
		curl.setopt(CurlOpt.URL, url.encode())

		# method
		method = request.method.upper()
		if method == "POST":
			curl.setopt(CurlOpt.POST, 1)
		elif method != "GET":
			curl.setopt(CurlOpt.CUSTOMREQUEST, method.encode())
		if method == "HEAD":
			curl.setopt(CurlOpt.NOBODY, 1)

		# timeout 
		if timeout is None:
			timeout = 0  # indefinitely
		
		if isinstance(timeout, tuple):
			connect_timeout, read_timeout = timeout
			all_timeout = connect_timeout + read_timeout

			curl.setopt(CurlOpt.CONNECTTIMEOUT_MS, int(connect_timeout * 1000))
	
			#curl.setopt(CurlOpt.TIMEOUT_MS, int(all_timeout * 1000))
		
			# trick from: https://github.com/lexiforest/curl_cffi/issues/156
			curl.setopt(CurlOpt.LOW_SPEED_LIMIT, 1)
			curl.setopt(CurlOpt.LOW_SPEED_TIME, math.ceil(all_timeout))
	
		elif isinstance(timeout, (int, float)):

				#curl.setopt(CurlOpt.TIMEOUT_MS, int(timeout * 1000))
		
				curl.setopt(CurlOpt.CONNECTTIMEOUT_MS, int(timeout * 1000))
				curl.setopt(CurlOpt.LOW_SPEED_LIMIT, 1)
				curl.setopt(CurlOpt.LOW_SPEED_TIME, math.ceil(timeout))

		# body
		body = b"" if not request.body else (
			request.body.encode() if isinstance(request.body, str) else request.body
		)

		if body or method in ("POST", "PUT", "PATCH"):
			curl.setopt(CurlOpt.POSTFIELDS, body)
			# necessary if body contains '\0'
			curl.setopt(CurlOpt.POSTFIELDSIZE, len(body))
			if method == "GET":
				curl.setopt(CurlOpt.CUSTOMREQUEST, method)
	
		# headers
		host_header = request.headers.get("host")
		if host_header is not None:
			# remove Host header if it's unnecessary, otherwise curl may get confused.
			# Host header will be automatically added by curl if it's not present.
			# https://github.com/lexiforest/curl_cffi/issues/119
			parsed_url = urlparse(url)
			if host_header == parsed_url.netloc or host_header == parsed_url.hostname:
				request.headers.pop("Host", None)

		request.headers.pop("Expect", None) # Never send `Expect` header. ?
		
		header_lines = []
		for k, v in request.headers.items():
			# Make Curl Headers Array
			# Make curl always include empty headers.
			# See: https://stackoverflow.com/a/32911474/1061155

			if v is None:
				header_lines.append(f"{k}:".encode())  # Explictly disable this header
			elif v == "":
				header_lines.append(f"{k};".encode())  # Add an empty valued header
			else:
				header_lines.append(f"{k}: {v}".encode())
	
		curl.setopt(CurlOpt.HTTPHEADER, header_lines)
		
		# cookies
		#already handled
	
		# files
		#already handled

		# auth
		#already handled, it's just a header...
		
		# allow_redirects
		curl.setopt(CurlOpt.FOLLOWLOCATION, 0)  # Don't allow. Requests library handles them by itself.
	
		# proxies
		proxy = select_proxy(request.url, proxies)
		if proxy:
			proxy: str = prepend_scheme_if_needed(proxy, "http")
			proxy_url = parse_url(proxy)
			if not proxy_url.host:
				raise InvalidProxyURL(
					"Please check proxy URL. It is malformed "
					"and could be missing the host."
				)


			curl.setopt(CurlOpt.PROXY, proxy)

			# Authentication?
			username, password = get_auth_from_url(proxy)
			if username:
				curl.setopt(CurlOpt.PROXYUSERNAME, username.encode())
				curl.setopt(CurlOpt.PROXYPASSWORD, password.encode())

			if proxy.lower().startswith("socks"):
				pass
			else:
				if proxy_url.scheme == "https":
					# For https site with http tunnel proxy, tell curl to enable tunneling
					curl.setopt(CurlOpt.HTTPPROXYTUNNEL, 1)
	
		
		# content decoding
		if self.use_curl_content_decoding:
			curl.setopt(CurlOpt.HTTP_CONTENT_DECODING, 1)
			curl.setopt(CurlOpt.ACCEPT_ENCODING, "gzip, deflate, br, zstd")
		else:
			curl.setopt(CurlOpt.HTTP_CONTENT_DECODING, 0)
			curl.setopt(CurlOpt.HTTP_TRANSFER_DECODING, 1)

		# do not check max_recv_speed
		curl.setopt(CurlOpt.MAX_RECV_SPEED_LARGE, 0)

	def send(
		self, request: requests.PreparedRequest, stream=False, timeout=None, verify=True, cert=None, proxies=None
	):
		self.reset_curl()
		
		self.cert_verify(self.curl, request.url, verify, cert)

		url = self.request_url(request, proxies)

		self.set_curl_options(
			self.curl,
			request=request,
			url=url,
			timeout=timeout,
			proxies=proxies
		)
		a = time.time()
		try:
			# Save headers when received
			header_buffer = BytesIO()
			self.curl.setopt(CurlOpt.HEADERDATA, header_buffer)

			# Callbacks for retrieving & saving curl info object
			curl_info_dict: CurlInfo = {}

			# Perform curl request with threading, and return body in a 'read' like class type (by simply using Curl.WRITEFUNCTION callback)
			start_curl_stream = (
				self.stream_handler(
					curl_instance=self.curl,
					callback_after_perform=lambda curl: curl_info_dict.update(self.parse_info(curl)),
					timeout=timeout,
					debug=self.debug
				)
			).start()
			
			# Headers are already available
			curl_info_dict.update(self.parse_info(self.curl, headers_only=True))

			if self.debug:
				print("[DEBUG] Curl Start Elapsed Time: ", time.time() - a)

			# Headers are available after start, parse them
			parsed_headers = self.parse_headers(self.curl, header_buffer)

			curl_stream_res = CurlStreamResponse(
				url=url,
				method=request.method.upper(),
				request=request,
				curl_stream_handler=start_curl_stream,
				use_curl_content_decoding=self.use_curl_content_decoding,
				**parsed_headers
			)

			return self.build_response(self.curl, curl_stream_res, parsed_headers, request, wait_for_body=start_curl_stream._wait_for_body, curl_info_dict=curl_info_dict)
		except OSError as e:
			raise ConnectionError(e, request=request)
		
		except (CurlError, pycurl.error) as e:
			error_to_throw = self.curl_error_map(e, has_proxy=proxies)
			raise error_to_throw(e, request=request)

		except Exception as e:
			if self.debug:
				traceback.print_exc()
			raise e
		finally:
			if self.debug:
				print("[DEBUG] Curl Send Elapsed Time: ", time.time() - a)
			pass
		
	def close(self) -> None:
		"""Close the session."""
		self._closed = True
		self.curl.close()

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()

