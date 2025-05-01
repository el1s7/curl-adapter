from concurrent.futures import ThreadPoolExecutor
import time
import requests
from curl_adapter import CurlCffiAdapter, PyCurlAdapter


test_url = "https://httpbingo.org/stream/100"

def curl_cffi_adapter_requests(n):
    try:
        s = requests.Session()

        s.mount("http://", CurlCffiAdapter())
        s.mount("https://", CurlCffiAdapter())
        a1 = time.time()
        s.get(test_url)
        a2 = time.time() - a1
        return a2
    except:
        return None

def pycurl_adapter_requests(n):
    try:
        s = requests.Session()

        s.mount("http://", PyCurlAdapter())
        s.mount("https://", PyCurlAdapter())
        a1 = time.time()
        s.get(test_url)
        a2 = time.time() - a1
        return a2
    except:
        return None


def normal_adapter_requests(n):
    try:
        s = requests.Session()
        a1 = time.time()
        s.get(test_url)
        a2 = time.time() - a1
        return a2
    except:
        return None


def benchmark(func, description):
    pool = ThreadPoolExecutor(10)

    n_requests = 1000

    results = pool.map(func, list(range(n_requests)))

    start = time.time()
    sum = 0
    i = 0
    for result in results:
        print("[~] Running benchmark for " + description + " : ", i, end="\r")
        if result is None:
            continue
        i +=1
        sum +=result
    end = time.time() - start


    print("\n\n-------[" + description + "]-------")
    print("  [*] Average request speed " +  description + " : ", str(sum / i))
    print("  [*] Average requests/sec " +  description + " : ", n_requests/end)
    print("\n")


if __name__ == "__main__":
    benchmark(curl_cffi_adapter_requests, "custom Curl CFFI Adapter")
    benchmark(pycurl_adapter_requests, "custom PyCurl Adapter")
    benchmark(normal_adapter_requests, "default urllib3 Adapter")