"""
Curl-Adapter
A curl HTTP adapter for requests library

:copyright: (c) 2025 by Elis K.
:license: MIT.
"""

__all__ = [
    "CurlCffiAdapter",
    "PyCurlAdapter",
    "CurlInfo"
]


from .base_adapter import CurlInfo
from .curl_cffi import CurlCffiAdapter
from .pycurl import PyCurlAdapter
