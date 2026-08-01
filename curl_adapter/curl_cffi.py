from typing import TypedDict, List

import curl_cffi.curl
from curl_cffi._wrapper import ffi, lib
from curl_cffi.curl import CurlOpt
from curl_cffi.requests.impersonate import (
	ExtraFingerprints,
	BrowserTypeLiteral,
)
from curl_cffi.requests.utils import (
	HttpVersionLiteral,
	normalize_http_version,
	set_ja3_options as curl_cffi_set_ja3_options,
	set_akamai_options as curl_cffi_set_akamai_options,
	set_extra_fp as curl_cffi_set_extra_fp,
)

try:
	# curl_cffi >= 0.16.0
	from curl_cffi.requests.impersonate import resolve_latest_browser_type as normalize_browser_type
except ImportError:
	# curl_cffi < 0.16.0
	from curl_cffi.requests.impersonate import normalize_browser_type

from .stream.handler.base import CurlStreamHandlerBase

from .base_adapter import BaseCurlAdapter


class CurlAdapterConfigurationOptions(TypedDict):
	ja3_str: str
	permute: bool
	akamai_str: str
	extra_fp: ExtraFingerprints

class CurlCffiAdapter(BaseCurlAdapter):

	def __init__(self, 
			*,
			impersonate_browser_type: BrowserTypeLiteral="chrome", 
			tls_configuration_options: CurlAdapterConfigurationOptions=None,
			http_version: HttpVersionLiteral | None = None,
			debug=False, 
			use_curl_content_decoding=False, 
			use_thread_local_curl=True,
			stream_handler: CurlStreamHandlerBase=None
		):

		self.impersonate_browser_type = impersonate_browser_type
		self.configuration_options = tls_configuration_options
		self.http_version = http_version

		super().__init__(curl_cffi.Curl, debug, use_curl_content_decoding, use_thread_local_curl, stream_handler)

	def enable_debug(self):
		if self.debug:
			self.curl.debug()

	def get_curl_info(self, curl: curl_cffi.Curl, option_code: int):
		value = super().get_curl_info(curl, option_code)

		if isinstance(value, bytes):
			return value.decode()

		return value

	def set_ja3_options(self, curl: curl_cffi.Curl, ja3: str, permute: bool = False):
		"""
			Delegates to curl_cffi's own implementation, so that JA3 handling stays in
			sync with the installed curl_cffi version.

			Detailed explanation: https://engineering.salesforce.com/tls-fingerprinting-with-ja3-and-ja3s-247362855967/
		"""
		curl_cffi_set_ja3_options(curl, ja3, permute=permute)

	def set_akamai_options(self, curl: curl_cffi.Curl, akamai: str):
		"""
			Delegates to curl_cffi's own implementation.

			Detailed explanation: https://www.blackhat.com/docs/eu-17/materials/eu-17-Shuster-Passive-Fingerprinting-Of-HTTP2-Clients-wp.pdf
		"""
		curl_cffi_set_akamai_options(curl, akamai)

	def set_extra_fp(self, curl: curl_cffi.Curl, fp: ExtraFingerprints):
		"""
			Delegates to curl_cffi's own implementation.
		"""
		curl_cffi_set_extra_fp(curl, fp)

	def set_curl_options(self, curl, request, url, timeout, proxies, request_adapter_options=None):
		super().set_curl_options(curl, request, url, timeout, proxies, request_adapter_options=request_adapter_options)

		# impersonate
		curl.impersonate(
			normalize_browser_type(self.impersonate_browser_type), 
			default_headers=False
		)

		# additional TLS fingerprint configuration options
		if self.configuration_options:
			if self.configuration_options.get("ja3_str"):
				self.set_ja3_options(
					curl, 
					self.configuration_options.get("ja3_str"), 
					self.configuration_options.get("permute", False)
				)
			if self.configuration_options.get("akamai_str"):
				self.set_akamai_options(
					curl,
					self.configuration_options.get("akamai_str")
				)

			if self.configuration_options.get("extra_fp"):
				self.set_extra_fp(
					curl,
					self.configuration_options.get("extra_fp")
				)
		
		# HTTP Version
		if self.http_version:
			curl_http_version = normalize_http_version(self.http_version)
			curl.setopt(CurlOpt.HTTP_VERSION, curl_http_version)

	def reset_curl(self):
		curl = self.curl
		if hasattr(curl, 'clean_handles_and_buffers'):
			# curl_cffi >= 0.14.0: clean_after_perform() was renamed to clean_handles_and_buffers()
			curl.clean_handles_and_buffers()
		elif hasattr(curl, 'clean_after_perform'):
			# curl_cffi < 0.14.0
			curl.clean_after_perform()
		return super().reset_curl(curl=curl)
