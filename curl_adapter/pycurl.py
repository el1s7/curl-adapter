import pycurl
from .base_adapter import BaseCurlAdapter
from .stream.handler.base import CurlStreamHandlerBase

class PyCurlAdapter(BaseCurlAdapter):

	def __init__(self, 
			debug=False, 
			use_curl_content_decoding=False, # pyCurl automatic decoding is disabled by default. Because pycurl doesnt support modern decoding algorithms...
			use_thread_local_curl=True,
			stream_handler: CurlStreamHandlerBase=None
        ):

		super().__init__(
			pycurl.Curl, 
			debug,
			use_curl_content_decoding, 
			use_thread_local_curl,
			stream_handler
		)

	def parse_info(self, curl: pycurl.Curl, headers_only=False):
		'''
		 PyCurl currently doesn't support newer methods like TOTAL_TIME_T, SPEED_DOWNLOAD_T, SPEED_UPLOAD_T, SIZE_UPLOAD_T, SIZE_DOWNLOAD_T, etc.
		 So we use the deprecated ones instead.
		'''
		
		additional_info = {
			# IP/Ports
			"local_ip": self.get_curl_info(curl, pycurl.LOCAL_IP), 
			"local_port": self.get_curl_info(curl, pycurl.LOCAL_PORT), 
			"primary_ip": self.get_curl_info(curl, pycurl.PRIMARY_IP), 
			"primary_port": self.get_curl_info(curl, pycurl.PRIMARY_PORT), 
			
			# Sizes
			"request_size": self.get_curl_info(curl, pycurl.REQUEST_SIZE), 
			"request_body_size": self.get_curl_info(curl, pycurl.SIZE_UPLOAD), 
			"response_header_size": self.get_curl_info(curl, pycurl.HEADER_SIZE),

			# SSL
			"ssl_verify_result": self.get_curl_info(curl, pycurl.SSL_VERIFYRESULT),
			"proxy_ssl_verify_result": "unsupported",

			# Times
			"starttransfer_time": self.get_curl_info(curl, pycurl.STARTTRANSFER_TIME),
			"connect_time": self.get_curl_info(curl, pycurl.CONNECT_TIME),
			"appconnect_time": self.get_curl_info(curl, pycurl.APPCONNECT_TIME),
			"pretransfer_time": self.get_curl_info(curl, pycurl.PRETRANSFER_TIME),
			"namelookup_time": self.get_curl_info(curl, pycurl.NAMELOOKUP_TIME),

			# Other
			"has_used_proxy": "unsupported", 
		}

		if not headers_only:
			# Available after the body has been parsed
			additional_info.update({
				"speed_download": self.get_curl_info(curl, pycurl.SPEED_DOWNLOAD), 
				"speed_upload": self.get_curl_info(curl, pycurl.SPEED_UPLOAD), 
				"response_body_size": self.get_curl_info(curl, pycurl.SIZE_DOWNLOAD), 
				"total_time": self.get_curl_info(curl, pycurl.TOTAL_TIME)
			})
			
		filtered_keys = {k:v for k,v in additional_info.items() if v is not None}

		return filtered_keys