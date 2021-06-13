import argparse
import asyncio
import dataclasses
import itertools
import logging
import socket
import selectors
import re
import math
import platform
from subprocess import SubprocessError
from time import perf_counter
from typing import Any, Dict, Iterable, Iterator, List, Sequence, Union

logging.basicConfig()
logger = logging.getLogger(__name__)


def normalize_to_sum(iter: Iterable[Union[int, float]]) -> List[float]:
    it1, it2 = itertools.tee(iter)
    total = sum(it1)
    return [float(i) / total for i in it2]


def split_by_ratio(seq: Sequence, ratio: Iterable) -> Iterator[Sequence]:
    ratio = normalize_to_sum(ratio)

    length = len(seq)
    splits = [0]
    for p in ratio:
        sub_length = math.ceil(p * length)
        splits.append(sub_length + splits[-1])

    splits[-1] = length

    for start, end in zip(splits, splits[1:]):
        yield seq[start:end]


def get_latencies(hosts: Iterable[str]) -> List:
    async_pings = [get_average_ping(host) for host in hosts]
    pings_future = asyncio.gather(*async_pings)

    loop = asyncio.get_event_loop()
    returns = loop.run_until_complete(pings_future)
    if not loop.is_running(): loop.close()

    return returns


def batch_seq(seq: Sequence, size: int) -> Iterator[Sequence]:
    return (seq[i:i + size] for i in range(0, len(seq), size))


async def get_average_ping(host: str, n: int = 3) -> float:
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


class MDALPClient:
    MAX_PAYLOAD = 100
    MAX_RETRIES = 10
    PORT = 4650
    TIMEOUT = 3
    MIN_RATIO = 0.1

    def __init__(self, addr):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.sock, selectors.EVENT_READ)
        self.addr_main = addr

    def __enter__(self):
        return self

    def close(self):
        self.sel.unregister(self.sock)
        self.sock.close()
        logger.info('MDALPClient closed.')

    def __exit__(self, type, value, traceback):
        self.close()

    @staticmethod
    def parse_message(message: str) -> Dict[str, Any]:
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

        parsed = match.groupdict()

        for field in fields[0:3]:
            name = field[0]
            if parsed[name] is not None: parsed[name] = int(parsed[name])

        if parsed.get('Type') == 1 and parsed.get('DATA') is not None:
            parsed['DATA'] = eval(parsed['DATA'])

        logger.debug(f'Parsed data: {parsed}')
        return parsed

    def _send_to(self,
                 addr,
                 type: int,
                 tid: int = None,
                 seq: int = None,
                 data: bytes = None) -> int:
        header = f'Type:{type};'
        if tid is not None: header += f'TID:{tid};'
        if seq is not None: header += f'SEQ:{seq};'
        header = header.encode()

        payload = b''
        if data is not None:
            header += b'DATA:'
            payload = data

        message = header + payload

        ret = max(0, self.sock.sendto(message, addr) - len(payload))
        logger.debug(f'client -> {addr} (return {ret}): {message}')
        return ret

    def _send_type0(self) -> int:
        return self._send_to(self.addr_main, 0)

    def _send_type2(self,
                    addr,
                    tid: int = None,
                    seq: int = None,
                    data: bytes = None) -> int:
        ret = 0
        for attempt in range(MDALPClient.MAX_RETRIES):
            ret = self._send_to(addr, 2, tid, seq, data)
            events = self.sel.select(MDALPClient.TIMEOUT)
            if len(events) == 0:
                logger.info(f'Timeout! seq: {seq}')
                continue

            data_recv, addr_from = self.sock.recvfrom(1024)
            if not data_recv: continue

            if addr_from != addr:
                logger.info(
                    f'Data received from {addr_from}, expected address is {addr}.'
                )
                continue

            data_recv = data_recv.decode()
            response = MDALPClient.parse_message(data_recv)

            if all((response.get('Type') == 3, response.get('TID') == tid,
                    response.get('SEQ') == seq)):
                break
        else:
            # MAX_RETRIES reached
            logger.warn(f'MAX_RETRIES of {MDALPClient.MAX_RETRIES} reached!\n\
                    addr: {addr}\n\
                    tid: {tid}\n\
                    seq: {seq}')
            return 0

        return ret

    def send_intent(self) -> Dict[str, Any]:
        response = None
        for attempt in range(1):
            self._send_type0()
            logger.info(f'Intent message sent to {self.addr_main}.')

            events = self.sel.select()
            if len(events) == 0:
                logger.info('Timeout! No reply from orchestrator server.')
                continue

            data, addr = self.sock.recvfrom(1024)

            if addr != self.addr_main:
                logger.info(
                    f'Data received from {addr}, expected address is {self.addr_main}.'
                )
                continue

            if not data:
                logger.info(f'Data received from {addr} is empty.')
                continue

            data = data.decode()
            response = MDALPClient.parse_message(data)

            # break on success
            if response.get('Type') == 1: break
        else:
            # MAX_RETRIES reached
            logger.warning(
                f'MAX_RETRIES of {MDALPClient.MAX_RETRIES} reached!')
            response = None

        logger.info(f'Response: {response}')
        return response

    def _send_single_server(self, host, tid: int, data: bytes) -> int:
        ret = 0
        for seq, data_splice in enumerate(
                batch_seq(data, MDALPClient.MAX_PAYLOAD)):
            ret += self._send_type2((host, MDALPClient.PORT), tid, seq,
                                    data_splice)
        return ret

    def _send_load_balance(self, hosts, tid: int, data: bytes) -> int:
        @dataclasses.dataclass
        class ServerData:
            host: str
            latency: float = float('inf')
            data_seq: Sequence[bytes] = dataclasses.field(repr=False,
                                                          default_factory=list)
            seq_min: int = 0
            seq_curr: int = 0
            _last_access: float = 0

            @property
            def seq_len(self):
                return self.seq_min + len(self.data_seq)

            @property
            def time_since_last_access(self):
                return perf_counter() - self._last_access

            @property
            def _data_idx(self):
                return self.seq_curr - self.seq_min

            @property
            def data_exhausted(self):
                return self._data_idx >= len(self.data_seq)

            def get_curr_data(self):
                self._last_access = perf_counter()
                return self.data_seq[
                    self._data_idx] if not self.data_exhausted else None

            def get_next_data(self):
                if self.data_exhausted: return None
                self.seq_curr += 1
                return self.get_curr_data()

        servers = [ServerData(host) for host in hosts]
        # get round trip times
        latencies = get_latencies(server.host for server in servers)
        for server, latency in zip(servers, latencies):
            server.latency = latency

        # sort servers by decreasing latency
        servers.sort(key=lambda s: s.latency, reverse=True)

        split_data = split_by_ratio(data,
                                    (1 / server.latency for server in servers))

        for server, sub_data in zip(servers, split_data):
            server.data_seq = list(batch_seq(sub_data,
                                             MDALPClient.MAX_PAYLOAD))

        acc = 0
        for server in servers:
            server.seq_min = acc
            server.seq_curr = acc
            acc += len(server.data_seq)

        # summary
        for server in servers:
            logger.info(server)

        ret = 0
        # initial send
        for server in servers:
            self._send_to((server.host, MDALPClient.PORT),
                          type=2,
                          tid=tid,
                          seq=server.seq_curr,
                          data=server.get_curr_data())

        # send the rest
        while not all(map(lambda s: s.data_exhausted, servers)):
            # check server timeouts
            for server in servers:
                if server.data_exhausted: continue
                if server.time_since_last_access >= MDALPClient.TIMEOUT:
                    logger.info(
                        f'Timeout! {server.host} | seq: {server.seq_curr}')
                    self._send_to((server.host, MDALPClient.PORT),
                                  type=2,
                                  tid=tid,
                                  seq=server.seq_curr,
                                  data=server.get_curr_data())

            events = self.sel.select(MDALPClient.TIMEOUT)
            if len(events) == 0: continue

            data_recv, addr_from = self.sock.recvfrom(1024)
            if not data_recv: continue

            data_recv = data_recv.decode()
            response = MDALPClient.parse_message(data_recv)

            if not (response.get('Type') == 3 and response.get('TID') == tid):
                continue

            server = next(
                (s
                 for s in servers if (s.host, MDALPClient.PORT) == addr_from),
                None)
            if server is None: continue

            if response.get('SEQ') != server.seq_curr:
                # not expected acknowledgement sequence number
                new_seq = response.get('SEQ') + 1
                if server.seq_min <= new_seq < server.seq_len:
                    logger.info(
                        f'Server {server.host}: Seq number mismatch. Updating seq_curr to {new_seq} from {server.seq_curr}.'
                    )
                    server.seq_curr = new_seq

            # server acknowledged as expected
            ret += len(server.get_curr_data())
            next_data = server.get_next_data()
            if next_data is None:
                # no more data to send for this server
                logger.info(f'Server: {server.host} done!')
                continue

            self._send_to((server.host, MDALPClient.PORT),
                          type=2,
                          tid=tid,
                          seq=server.seq_curr,
                          data=next_data)

        return ret

    def send(self,
             data: bytes,
             load_balance: bool = True,
             nth_server: int = 1) -> int:
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
        if args.mode == 1:
            ret = sock.send(data)
        else:
            ret = sock.send(data, load_balance=False, nth_server=args.server)
        print(ret, len(data))


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