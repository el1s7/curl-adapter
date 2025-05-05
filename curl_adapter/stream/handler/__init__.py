import typing

from ._thread_env import _THREAD_ENV
from .base import CurlStreamHandlerBase


if _THREAD_ENV == "gevent":
	from .gevent_handler import CurlStreamHandlerGevent
	_Impl = CurlStreamHandlerGevent
else:
	# Alternatively, CurlStreamHandlerThreads
	from .multi_handler import CurlStreamHandlerMulti
	_Impl = CurlStreamHandlerMulti

CurlStreamHandler: typing.Type[CurlStreamHandlerBase] = _Impl