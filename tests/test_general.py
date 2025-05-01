'''
	Just run: pytest
'''

import pytest
import requests
import requests.adapters
from curl_adapter import CurlCffiAdapter, PyCurlAdapter, CurlInfo


test_server = "https://httpbingo.org" #httpbin.org, httpbingo.org, postman-echo.com

@pytest.mark.parametrize("curl_adapter", [CurlCffiAdapter, PyCurlAdapter])
class TestFunctions:

	def test_gzip(self, curl_adapter):

		with requests.Session() as s:
			s.mount("http://", curl_adapter(use_curl_content_decoding=True))
			s.mount("https://", curl_adapter(use_curl_content_decoding=True))
			r = s.get(f"{test_server}/gzip")
			assert r.status_code == 200
			assert r.json()["gzipped"]

	def test_gzip_python(self, curl_adapter):

		with requests.Session() as s:
			s.mount("http://", curl_adapter(use_curl_content_decoding=False))
			s.mount("https://", curl_adapter(use_curl_content_decoding=False))
			r = s.get(f"{test_server}/gzip")
			assert r.status_code == 200
			assert r.json()["gzipped"]

	def test_brotli(self, curl_adapter):
		# This fails for PyCurl because PyCurl doesn't support brotli decoding by itself.
		
		if curl_adapter is PyCurlAdapter:
			pytest.skip("PyCurl because PyCurl doesn't support brotli decoding by itself.")
		
		with requests.Session() as s:
			s.mount("http://", curl_adapter(use_curl_content_decoding=True))
			s.mount("https://", curl_adapter(use_curl_content_decoding=True))
			r = s.get(f"{test_server}/get", headers={
				"Accept-Encoding": "br"
			})
			assert r.status_code == 200
			assert "headers" in r.json()

	def test_brotli_python(self, curl_adapter):

		with requests.Session() as s:
			s.mount("http://", curl_adapter(use_curl_content_decoding=False))
			s.mount("https://", curl_adapter(use_curl_content_decoding=False))
			r = s.get(f"{test_server}/get", headers={
				"Accept-Encoding": "br"
			})
			assert r.status_code == 200
			assert "headers" in r.json()

	def test_raw_response(self, curl_adapter):
		with requests.Session() as s:
			s.mount("http://", curl_adapter(use_curl_content_decoding=True))
			s.mount("https://", curl_adapter(use_curl_content_decoding=True))

			r  = s.get(f"{test_server}/gzip")

			size1 = r.raw.tell()

			s.mount("http://", curl_adapter(use_curl_content_decoding=False))
			s.mount("https://", curl_adapter(use_curl_content_decoding=False))

			r  = s.get(f"{test_server}/gzip")

			size2 = r.raw.tell()

			assert size1 > size2

	def test_redirects(self, curl_adapter):
		with requests.Session() as s:
			s.mount("http://", curl_adapter())
			s.mount("https://", curl_adapter())
			r = s.get(f"{test_server}/absolute-redirect/3")
			assert r.status_code == 200

	def test_relative_redirects(self, curl_adapter):
		with requests.Session() as s:
			s.mount("http://", curl_adapter())
			s.mount("https://", curl_adapter())
			r = s.get(f"{test_server}/relative-redirect/3")
			assert r.status_code == 200

	def test_fingerprints(self, curl_adapter):
		with requests.Session() as s:
			s.mount("http://", curl_adapter())
			s.mount("https://", curl_adapter())
			curl_request = s.get("https://tools.scrapfly.io/api/fp/ja3")

			s.mount("http://", requests.adapters.HTTPAdapter())
			s.mount("https://", requests.adapters.HTTPAdapter())

			normal_request = s.get("https://tools.scrapfly.io/api/fp/ja3")

			assert curl_request.json()["ja3n_digest"] != normal_request.json()["ja3n_digest"]

	def test_cookies(self, curl_adapter):
		with requests.Session() as s:
			s.mount("http://", curl_adapter())
			s.mount("https://", curl_adapter())

			s.get(f"{test_server}/cookies/set?cookie1=one", allow_redirects=False)
			s.get(f"{test_server}/cookies/set?cookie2=two", allow_redirects=False)
			res = s.get(f"{test_server}/cookies/set?cookie3=three")

			res_cookies = res.json()["cookies"] if "cookies" in res.json() else res.json()

			assert res_cookies["cookie1"] == "one"
			assert res_cookies["cookie2"] == "two"
			assert res_cookies["cookie3"] == "three"

			assert s.cookies.get("cookie1") == "one"
			assert s.cookies.get("cookie2") == "two"
			assert s.cookies.get("cookie3") == "three"

	def test_curl_info(self, curl_adapter):
		with requests.Session() as s:
			s.mount("http://", curl_adapter())
			s.mount("https://", curl_adapter())
			r = s.get(f"{test_server}/get")
			curl_info: CurlInfo = r.get_curl_info()
			assert "local_ip" in curl_info

	def test_timeout(self, curl_adapter):
		with pytest.raises(requests.exceptions.ConnectTimeout) as err:
			with requests.Session() as s:
				s.mount("http://", curl_adapter())
				s.mount("https://", curl_adapter())
				s.get(f"{test_server}/delay/10", timeout=2)
		assert "Operation too slow." in str(err.value)

	def test_ssl_verify(self, curl_adapter):
		with pytest.raises(requests.exceptions.SSLError) as err:
			with requests.Session() as s:
				s.mount("http://", curl_adapter())
				s.mount("https://", curl_adapter())
				s.get("https://expired.badssl.com/")
		assert "certificate has expired" in str(err.value)

	def test_ssl_no_verify(self, curl_adapter):
		with requests.Session() as s:
			s.mount("http://", curl_adapter())
			s.mount("https://", curl_adapter())
			s.verify = False
			r = s.get("https://expired.badssl.com/")
			assert r.status_code == 200


