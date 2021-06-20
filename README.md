# Multi Destination Application Layer Protocol Client
Send data to a MDALP server. 

Contacts an *orchestrator* server to receive a list of *receiving* servers. Distributes sending of data to receiving servers according to latency.

## Requirements
```
Python >= 3.6
```

# Command line usage

## Sample usage
```bash
python mdalp.py -a 18.139.29.142 -p 4650 -f large_payload -v
```

## Help
```
usage: python mdalp.py -a ADDR -p PORT -f FILE [-h] [-v] [-m {1,2}] [-s SERVER]

Send text files using multidestination application-layer protocol (MDALP)

required arguments:
  -a ADDR, --addr ADDR  IPv4 address of the server
  -p PORT, --port PORT  UDP port of the server
  -f FILE, --file FILE  filename of the payload

optional arguments:
  -h, --help            show this help message and exit
  -v, --verbose
  -m {1,2}, --mode {1,2}
                        mode of the load balancing {1=load balance, 2=no load balancing}
  -s SERVER, --server SERVER
                        index of the server to use when no load balancing mode is used
```

# API
## Creating an MDALPClient object
```python
mdalp.MDALPClient(addr, sock: socket.socket = None)
'''
Args:
    addr: IP address of the server
    sock (socket.socket, optional): Socket to use, creates a new socket object if None. Defaults to None.
Returns:
    MDALPClient: an MDALPClient object    
'''
```

## MDALPClient object methods
```python
MDALPClient.send(data: bytes, load_balance: bool = True, nth_server: int = 1) -> int
'''Send data.

Args:
    data (bytes): Data to send
    load_balance (bool, optional): Flag if load balancing should be used. Defaults to True.
    nth_server (int, optional): The 1-indexed server number to use. Defaults to 1.

Returns:
    int: Number of data in bytes sent
'''
```
```python
MDALPClient.close()
'''Call when done'''
```

## Using with the Python context manager
To simplify usage and avoid the need of calling the `close()` method, one can use Python's context manager.
```python
with mdalp.MDALPClient((host, port)) as sock:
    sock.send(data)
```