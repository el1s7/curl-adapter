
import pycurl
from .base_adapter import BaseCurlAdapter



class PyCurlAdapter(BaseCurlAdapter):

	def __init__(self, 
			debug=False, 
			use_curl_content_decoding=False, # pyCurl automatic decoding is disabled by default. Because pycurl doesnt support modern decoding algorithms...
			use_thread_local_curl=True
        ):

		super().__init__(
			pycurl.Curl, 
			debug,
			use_curl_content_decoding, 
			use_thread_local_curl
		)

	def parse_info(self, curl: pycurl.Curl):

		additional_info = {
			"local_ip": self.get_curl_info(curl, pycurl.LOCAL_IP), 
			"local_port": self.get_curl_info(curl, pycurl.LOCAL_PORT), 
			"primary_ip": self.get_curl_info(curl, pycurl.PRIMARY_IP), 
			"primary_port": self.get_curl_info(curl, pycurl.PRIMARY_PORT), 
			"total_time": self.get_curl_info(curl, pycurl.TOTAL_TIME),  #Unsupported TOTAL_TIME_T
			"speed_download": self.get_curl_info(curl, pycurl.SPEED_DOWNLOAD), #Unsupported SPEED_DOWNLOAD_T
			"speed_upload": self.get_curl_info(curl, pycurl.SPEED_UPLOAD),  #Unsupported SPEED_UPLOAD_T
			"size_upload": self.get_curl_info(curl, pycurl.SIZE_UPLOAD), #Unsupported SIZE_UPLOAD_T
			"request_size": self.get_curl_info(curl, pycurl.REQUEST_SIZE),
			"response_body_size": self.get_curl_info(curl, pycurl.SIZE_DOWNLOAD), #Unsupported SIZE_DOWNLOAD_T
			"response_header_size": self.get_curl_info(curl, pycurl.HEADER_SIZE), 
		}

		return additional_info


	def set_curl_options(self, curl, request, url, timeout, proxies):
		super().set_curl_options(curl, request, url, timeout, proxies)
		
		if self.use_curl_content_decoding:
			# For some reason pycurl content decoding can only be enabled like this:
			curl.setopt(pycurl.HTTP_CONTENT_DECODING, 0)
			curl.setopt(pycurl.ENCODING, "gzip, deflate") #br, zstd not supported...
			# Seems it better to use the urllib3 content decoding instead of automatic