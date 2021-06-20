'''
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
'''

import argparse
import asyncio
import itertools
import logging
import socket
import selectors
import re
import math
import platform
from subprocess import SubprocessError
from time import perf_counter
from typing import Any, Dict, Iterable, Iterator, List, Sequence, Tuple, Union

logging.basicConfig()
logger = logging.getLogger(__name__)


def argsort(seq: Sequence, *args, **kargs) -> List:
    '''Returns the indices that would sort an array.

    Args:
        seq (Sequence): Sequence to sort.

    Returns:
        List: List of indices that sort `seq`.
    '''
    return sorted(range(len(seq)), key=seq.__getitem__, *args, **kargs)


def normalize_to_sum(iter: Iterable[Union[int, float]]) -> List[float]:
    '''Returns the scaled `iter` to have a sum of 1.

    Args:
        iter (Iterable[Union[int, float]]): The iterable to normalize.

    Returns:
        List[float]: Scaled `iter`
    '''
    it1, it2 = itertools.tee(iter)
    total = sum(it1)
    return [float(i) / total for i in it2]


def split_by_ratio(seq: Sequence,
                   weights: Iterable,
                   min_length: int = 0) -> Iterator[Sequence]:
    '''Split `seq` according to `weights`. Tries to satisfy the optional
    `min_length` argument.

    Args:
        seq (Sequence): Sequence to split
        weights (Iterable): Ratio
        min_length (int, optional): Minimum splice length. Defaults to 0.

    Yields:
        Iterator[Sequence]: a splice of `seq`
    '''
    ratio_norm = normalize_to_sum(weights)
    ratio_norm_argsort = argsort(ratio_norm)

    seq_len = len(seq)
    sub_seq_lens = [0] * len(ratio_norm)
    remaining = seq_len
    for idx, ratio in zip(ratio_norm_argsort, sorted(ratio_norm)):
        sub_len = min(max(round(ratio * seq_len), min_length), remaining)
        remaining -= sub_len
        sub_seq_lens[idx] = sub_len

    splits = [0]
    for sub_len in sub_seq_lens:
        splits.append(splits[-1] + sub_len)

    for start, end in zip(splits, splits[1:]):
        yield seq[start:end]


async def get_average_ping(host: str, n: int = 3) -> float:
    '''Async function to get average ping of `n` echo requests to `host`

    Args:
        host (str): host
        n (int, optional): Number of echo requests. Defaults to 3.

    Raises:
        SubprocessError: OS ping command error
        RuntimeError: Cannot parse ping command

    Returns:
        float: Average ping
    '''
    number = r'\d+(?:\.\d+)?'
    if platform.system().lower() == 'windows':
        count_flag = '-n'
        pattern = f'Average = ({number})ms'
    else:
        count_flag = '-c'
        pattern = f'min/avg/max/mdev = {number}/({number})'

    proc = await asyncio.create_subprocess_exec('ping',
                                                count_flag,
                                                str(n),
                                                host,
                                                stdout=asyncio.subprocess.PIPE)

    stdout, stderr = await proc.communicate()
    if proc.returncode < 0:
        raise SubprocessError('ping command error')
    match = re.search(pattern, stdout.decode())
    if match is None:
        raise RuntimeError(
            f'no match found for pattern {pattern} in output\n{stdout}')
    average = float(match.group(1))
    return average


def get_latencies(hosts: Iterable[str]) -> List:
    '''Returns a list of the round trip times of `hosts`

    Args:
        hosts (Iterable[str]): Hosts

    Returns:
        List: List of round trip times
    '''
    async_pings = [get_average_ping(host) for host in hosts]
    pings_future = asyncio.gather(*async_pings)

    loop = asyncio.get_event_loop()
    returns = loop.run_until_complete(pings_future)
    if not loop.is_running(): loop.close()

    return returns


def batch_seq(seq: Sequence, size: int) -> Iterator[Sequence]:
    '''Batch a sequence into `size` lenghts

    Args:
        seq (Sequence): The sequence
        size (int): Length size

    Returns:
        Iterator[Sequence]: Batched `seq`
    '''
    return (seq[i:i + size] for i in range(0, len(seq), size))


class MDALP:
    '''Abstract base class of MDALP'''
    def __init__(self, addr, sock: socket.socket = None):
        '''
        Args:
            addr: IP address of the server
            sock (socket.socket, optional): Socket to use, creates new socket object if None. Defaults to None.
        '''
        self.sock: socket.socket = sock if sock is not None else socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.sock, selectors.EVENT_READ)
        self.addr = addr

    def __enter__(self):
        return self

    def close(self):
        '''Call when done'''
        self.sel.unregister(self.sock)
        self.sock.close()
        logger.info(f'MDALP: {self.addr} closed.')

    def __exit__(self, type, value, traceback):
        self.close()

    @staticmethod
    def parse_message(message: str) -> Dict[str, Any]:
        '''Parses a message into a dictionary

        Args:
            message (str): Message

        Returns:
            Dict[str, Any]: Parsed info
        '''
        if len(message) > 0 and message[-1] != ';': message += ';'
        fields = [
            ('Type', r'\d+'),
            ('TID', r'\d+'),
            ('SEQ', r'\d+'),
            ('DATA', r'.*')  # greedy
        ]
        regex = ''.join('({0}:(?P<{0}>{1});)?'.format(*field)
                        for field in fields)
        match = re.match(regex, message)
        if match is None: return None

        parsed = match.groupdict()
        for field in fields[0:3]:
            name = field[0]
            if parsed.get(name) is not None: parsed[name] = int(parsed[name])

        if parsed.get('Type') == 1 and parsed.get('DATA') is not None:
            parsed['DATA'] = eval(parsed['DATA'])

        logger.debug(f'Parsed data: {parsed}')
        return parsed

    def send_packet(self,
                    type: int,
                    tid: int = None,
                    seq: int = None,
                    data: bytes = None) -> int:
        '''Send a packet

        Args:
            type (int): Packet type
            tid (int, optional): Transaction ID. Defaults to None.
            seq (int, optional): Sequence number. Defaults to None.
            data (bytes, optional): Payload. Defaults to None.

        Returns:
            int: Number of bytes of `data` sent not including header.
        '''
        header = f'Type:{type};'
        if tid is not None: header += f'TID:{tid};'
        if seq is not None: header += f'SEQ:{seq};'
        header = header.encode()

        payload = b''
        if data is not None:
            header += b'DATA:'
            payload = data

        message = header + payload

        ret = max(0, self.sock.sendto(message, self.addr) - len(header))
        logger.debug(f'client -> {self.addr} (return {ret}): {message}')
        return ret

    def recv_packet(self,
                    buf_size: int = 1024,
                    timeout: float = None) -> Dict[str, Any]:
        '''Returns parsed message received

        Args:
            buf_size (int, optional): Buffer size. Defaults to 1024.
            timeout (float, optional): Timeout in seconds. Defaults to None.

        Returns:
            Dict[str, Any]: Parsed message.
        '''
        events = self.sel.select(timeout)
        if len(events) == 0:
            logger.info(f'Receive timeout!')
            return None

        data, addr = self.sock.recvfrom(buf_size)

        if addr != self.addr:
            logger.info(
                f'Data received from {addr}, expected address is {self.addr}.')
            return None

        if not data:
            logger.info(f'Data received from {addr} is empty.')
            return None

        data = data.decode()
        parsed = self.parse_message(data)
        if parsed is None: logger.warn(f'Parsed failed with data {data}')
        return parsed

    def recv_packet_from(self,
                         buf_size: int = 1024,
                         timeout: float = None) -> Tuple[Dict[str, Any], Any]:
        '''Returns tuple of parsed message and address of sender

        Args:
            buf_size (int, optional): Buffer size. Defaults to 1024.
            timeout (float, optional): Timeout in seconds. Defaults to None.

        Returns:
            Tuple[Dict[str, Any], Any]: Parsed message and address
        '''
        events = self.sel.select(timeout)
        if len(events) == 0:
            logger.info(f'Receive timeout!')
            return None, None

        data, addr = self.sock.recvfrom(buf_size)

        if not data:
            logger.info(f'Data received from {addr} is empty.')
            return None, None

        data = data.decode()
        parsed = self.parse_message(data)
        if parsed is None: logger.warn(f'Parsed failed with data {data}')
        return parsed, addr


class MDALPRecvClient(MDALP):
    '''Client class for the MDALP receiving server.'''
    def __init__(self,
                 addr,
                 sock: socket.socket,
                 tid: int,
                 data_seq: Sequence[bytes],
                 seq_start: int = 0):
        '''
        Args:
            addr ([type]): IP address of the server
            sock (socket.socket): Socket to use
                                  Usually the socket of the MDALP client
            tid (int): Transaction ID
            data_seq (Sequence[bytes]): Data to be sent to the receiving server
            seq_start (int, optional): Starting sequence number. Defaults to 0.
        '''
        super().__init__(addr, sock=sock)
        self.tid = tid
        self._last_send = None
        self._seq = tuple(data_seq)
        self._base = seq_start
        self._curr_idx = 0

    @property
    def data_len(self) -> int:
        return len(self._seq)

    @property
    def seq_min(self) -> int:
        return self._base

    @property
    def seq_len(self) -> int:
        return len(self._seq) + self._base

    @property
    def seq_curr(self) -> int:
        return self._curr_idx + self._base

    @seq_curr.setter
    def seq_curr(self, i: int):
        self._curr_idx = i - self._base

    @property
    def data_exhausted(self) -> bool:
        return self._curr_idx >= len(self._seq)

    @property
    def time_since_last_send(self) -> float:
        return perf_counter() - self._last_send

    def reset(self):
        '''Reset data iteration'''
        self._curr_idx = 0

    def get_curr_data(self) -> bytes:
        '''Returns the current data in iteration

        Returns:
            bytes: Bytes of data, None if iteration is finished
        '''
        return self._seq[self._curr_idx] if not self.data_exhausted else None

    def get_next_data(self) -> bytes:
        '''Returns the next data in iteration.
        Current index/sequence is incremented

        Returns:
            bytes: Bytes of data, None if iteration is finished
        '''
        self._curr_idx = min(self._curr_idx + 1, len(self._seq))
        if self.data_exhausted: return None
        return self._seq[self._curr_idx]

    def send_curr(self):
        '''Sends the current data in iteration'''
        data = self.get_curr_data()
        if data is None: return
        self.send_packet(type=2, tid=self.tid, seq=self.seq_curr, data=data)
        self._last_send = perf_counter()

    def send_next(self):
        '''Sends the next data in iteration
        Current index/sequence is incremented
        '''
        data = self.get_next_data()
        if data is None: return
        self.send_packet(type=2, tid=self.tid, seq=self.seq_curr, data=data)
        self._last_send = perf_counter()


class MDALPClient(MDALP):
    '''Client class for the MDALP orchestrator server.'''
    MAX_PAYLOAD = 100
    RECV_PORT = 4650
    TIMEOUT = 3
    MIN_RATIO = 0.1

    def send_intent(self) -> Dict[str, Any]:
        '''Returns the parsed type 1 message after sending the type 0.

        Returns:
            Dict[str, Any]: Parsed message
        '''
        self.send_packet(0)
        logger.info(f'Intent message sent to {self.addr}.')
        response = None
        while response is None:
            response = self.recv_packet(timeout=5)

        if response.get('Type') != 1: return None
        logger.info(f'Response: {response}')
        return response

    def _send_single_server(self, host: str, tid: int, data: bytes) -> int:
        '''Send `data` to `host` with transactio ID `tid`.

        Args:
            host (str): Host
            tid (int): Transaction ID
            data (bytes): Data

        Returns:
            int: Number of data in bytes sent
        '''
        addr = (host, self.RECV_PORT)
        server = MDALPRecvClient(addr, self.sock.dup(), tid,
                                 batch_seq(data, self.MAX_PAYLOAD))

        ret = 0
        server.send_curr()
        while not server.data_exhausted:
            response = server.recv_packet(timeout=self.TIMEOUT)
            if response is None:
                server.send_curr()
                continue

            if not all((response.get('Type') == 3, response.get('TID')
                        == tid, response.get('SEQ') == server.seq_curr)):
                server.send_curr()
                continue

            ret += len(server.get_curr_data())
            server.send_next()

        return ret

    def _send_load_balance(self, hosts: Iterable[str], tid: int,
                           data: bytes) -> int:
        '''Send `data` to `hosts` with transactio ID `tid`.
        Load balances using the inverse of round trip times

        Args:
            hosts (Iterable[str]): Hosts
            tid (int): Transaction ID
            data (bytes): Data

        Returns:
            int: Number of data in bytes sent
        '''
        hosts = list(hosts)

        # get round trip times
        latencies = get_latencies(hosts)
        # split by inverse of RTTs
        split_data = split_by_ratio(data, (1 / l for l in latencies),
                                    math.ceil(self.MIN_RATIO * len(data)))

        recv_servers: List[MDALPRecvClient] = []
        seq_base = 0
        for host, d in zip(hosts, split_data):
            addr = (host, self.RECV_PORT)
            recv_servers.append(
                MDALPRecvClient(addr, self.sock.dup(), tid,
                                batch_seq(d, self.MAX_PAYLOAD), seq_base))
            seq_base += recv_servers[-1].data_len

        # summary
        for server, latency in zip(recv_servers, latencies):
            logger.info(
                f'addr: {server.addr}, latency: {latency}, data_len: {server.data_len}'
            )

        ret = 0
        # initial send
        for server in recv_servers:
            server.send_curr()

        # send the rest
        while any(map(lambda s: not s.data_exhausted, recv_servers)):
            # check server timeouts
            for server in recv_servers:
                if server.data_exhausted: continue
                if server.time_since_last_send >= self.TIMEOUT:
                    logger.info(
                        f'Timeout! {server.addr} | seq: {server.seq_curr}')
                    server.send_curr()

            response, addr_from = self.recv_packet_from(timeout=self.TIMEOUT)
            if response is None: continue

            if not (response.get('Type') == 3 and response.get('TID') == tid):
                continue

            server = next((s for s in recv_servers if s.addr == addr_from),
                          None)
            if server is None: continue

            if response.get('SEQ') != server.seq_curr:
                # not expected acknowledgement sequence number
                new_seq = response.get('SEQ') + 1
                if server.seq_min <= new_seq < server.seq_len:
                    logger.info(
                        f'Server {server.addr}: Seq number mismatch. Updating seq_curr to {new_seq} from {server.seq_curr}.'
                    )
                    server.seq_curr = new_seq

            # server acknowledged as expected
            ret += len(server.get_curr_data())
            server.send_next()

        for s in recv_servers:
            s.close()

        return ret

    def send(self,
             data: bytes,
             load_balance: bool = True,
             nth_server: int = 1) -> int:
        '''Send data.

        Args:
            data (bytes): Data to send
            load_balance (bool, optional): Flag if load balancing should be used. Defaults to True.
            nth_server (int, optional): The 1-indexed server number to use. Defaults to 1.

        Returns:
            int: Number of data in bytes sent
        '''
        response = self.send_intent()
        if response is None: return 0

        tid = response.get('TID')
        if tid is None: return 0

        hosts = [server.get('ip_address') for server in response.get('DATA')]
        if len(hosts) == 0: return 0

        ret = 0
        if load_balance:
            ret = self._send_load_balance(hosts, tid, data)
        else:
            ret = self._send_single_server(hosts[nth_server - 1], tid, data)

        logger.info(f'Send completed.')
        return ret


def main(args):
    if args.verbose == 1:
        logger.setLevel(logging.INFO)
    elif args.verbose >= 2:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.WARNING)

    logger.info(f'Parsed args: {args}')

    with MDALPClient((args.addr, args.port)) as sock:
        data = args.file.read().encode()
        start = perf_counter()
        if args.mode == 1:
            ret = sock.send(data)
        else:
            ret = sock.send(data, load_balance=False, nth_server=args.server)
        end = perf_counter()
        logger.debug(f'return: {ret} | data length: {len(data)}')
        logger.info(f'Send took {end - start}s, with arguments {args}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=
        'Send text files using  multidestination application-layer protocol (MDALP)',
        add_help=False)

    required = parser.add_argument_group(title='required arguments')
    optional = parser.add_argument_group(title='optional arguments')

    required.add_argument('-a',
                          '--addr',
                          required=True,
                          help='IPv4 address of the server')
    required.add_argument('-p',
                          '--port',
                          required=True,
                          type=int,
                          help='UDP port of the server')
    required.add_argument('-f',
                          '--file',
                          type=argparse.FileType(mode='r', encoding='UTF-8'),
                          required=True,
                          help='filename of the payload')

    optional.add_argument('-h',
                          '--help',
                          action='help',
                          default=argparse.SUPPRESS,
                          help='show this help message and exit')
    optional.add_argument('-v', '--verbose', action="count", default=0)
    optional.add_argument(
        '-m',
        '--mode',
        default=1,
        type=int,
        choices=[1, 2],
        help='mode of the load balancing {1=load balance, 2=no load balancing}'
    )
    optional.add_argument(
        '-s',
        '--server',
        default=1,
        type=int,
        help='index of the server to use when no load balancing mode is used')

    args = parser.parse_args()

    main(args)