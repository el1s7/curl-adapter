# Curl Adapter
[![PyPI Downloads](https://static.pepy.tech/badge/curl-adapter/month)](https://pepy.tech/projects/curl-adapter)

A module that plugs straight-in to the python *[requests](https://github.com/psf/requests)* library and replaces the default *urllib3* HTTP adapter with **cURL**,  equipped with TLS fingerprint changing capabilities.

## Why?

Specifically, this module is meant to be used with the "curl impersonate" python bindings ([lexiforest/curl_cffi](https://github.com/lexiforest/curl_cffi)), in order to send HTTP requests with custom, browser-like TLS & HTTP/2 fingerprints for bypassing sites that detect and block normal python requests (such as [Cloudflare](https://www.nstbrowser.io/en/blog/how-does-cloudflare-detect-bots) for example).

<details>
  <summary>Note</summary>
Even though <i><a href="https://github.com/lexiforest/curl_cffi">curl_cffi</a></i> already has an API that *mimicks* the <i>requests</i>  library, it comes with some compatibility issues (e.g. response.raw not available, response.history, differences in headers, cookies, json, etc.).
<br><br>
    With curl adapter, instead of copying and mimicking the <i>requests</i> library API, the low level HTTP adapter is changed with a custom crafted one, and everything else is exactly the same (even the exceptions are mapped). 
<br><br>
With a single switch you can enable/disable curl for your requests, without needing to worry about changing the way you normally work with requests.
<br><br>
Though, if you're looking for async support or websockets, you should definitely checkout the <i>curl_cffi</i> instead, since by default, the requests library is only sync.
</details>
<br>

Additionally, you can even use curl adapter with [pycurl](https://github.com/pycurl/pycurl). 

## Installation
```console
pip install curl-adapter --upgrade --ignore-installed
```

## Usage
Basic example:
```python
import requests
from curl_adapter import CurlCffiAdapter

session = requests.Session()
session.mount("http://", CurlCffiAdapter())
session.mount("https://", CurlCffiAdapter())

# just use requests session like you normally would
session.get("https://example.com")
```

Configuring curl impersonate options:

```python
import requests
from curl_adapter import CurlCffiAdapter

# you can use 'with ...' for just making a single request
with requests.Session() as s:
    s.mount("http://", CurlCffiAdapter(impersonate_browser_type="chrome"))
    s.mount("https://", CurlCffiAdapter(impersonate_browser_type="chrome"))

    s.get("https://example.com")
```

Using it with [pycurl](https://github.com/pycurl/pycurl):

```python
import requests
from curl_adapter import PyCurlAdapter

with requests.Session() as s:
    s.mount("http://", PyCurlAdapter())
    s.mount("https://", PyCurlAdapter())

    s.get("https://example.com")
```

## More
You can get extra information from the curl response info:
```python
import requests
from curl_adapter import PyCurlAdapter, CurlInfo

with requests.Session() as s:
    s.mount("http://", PyCurlAdapter())
    s.mount("https://", PyCurlAdapter())

    response = s.get("https://example.com")
    curl_info: CurlInfo = response.get_curl_info()
    print(
        curl_info
    )
```

Returns:
```python
{
    'local_ip': '192.168.1.1', 
    'local_port': 40164,
    'primary_ip': '3.210.94.60', 
    'primary_port': 443, 
    'total_time': 429186, 
    'speed_download': 472, 
    'speed_upload': 0, 
    'size_upload': 0, 
    'request_size': 0, 
    'response_body_size': 203, 
    'response_header_size': 224
}
```