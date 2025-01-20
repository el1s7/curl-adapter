For a simple benchmark test you can run `py -m tests.benchmark`. 
Using 10 thread workers, and sending 100 requests.

Example benchmark output:
```
-------[custom Curl Adapter]-------
  [*] Average request speed custom Curl Adapter :  0.3508183431625366
  [*] Average requests/sec custom Curl Adapter :  27.448449703641707

-------[default urllib3 Adapter]-------
  [*] Average request speed default urllib3 Adapter :  0.45488128185272214
  [*] Average requests/sec default urllib3 Adapter :  20.894119822529422
```

It shows the average request elapsed time in ms, and average amount of requests per sec. Results can vary.
