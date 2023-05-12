import random
import socket
import string
import struct
import asyncio
import logging
import time
import ipaddress
from collections import namedtuple
from functools import reduce
from typing import Optional

from lbry.dht.node import get_kademlia_peers_from_hosts
from lbry.utils import resolve_host, async_timed_cache, cache_concurrent
from lbry.wallet.stream import StreamController
from lbry import version

log = logging.getLogger(__name__)
CONNECTION_EXPIRES_AFTER_SECONDS = 50
PREFIX = 'LB'  # todo: PR BEP20 to add ourselves
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_CONCURRENCY_LIMIT = 100
# see: http://bittorrent.org/beps/bep_0015.html and http://xbtt.sourceforge.net/udp_tracker_protocol.html
ConnectRequest = namedtuple("ConnectRequest", ["connection_id", "action", "transaction_id"])
ConnectResponse = namedtuple("ConnectResponse", ["action", "transaction_id", "connection_id"])
AnnounceRequest = namedtuple("AnnounceRequest",
                             ["connection_id", "action", "transaction_id", "info_hash", "peer_id", "downloaded", "left",
                              "uploaded", "event", "ip_addr", "key", "num_want", "port"])
AnnounceResponse = namedtuple("AnnounceResponse",
                              ["action", "transaction_id", "interval", "leechers", "seeders", "peers"])
CompactIPv4Peer = namedtuple("CompactPeer", ["address", "port"])
ScrapeRequest = namedtuple("ScrapeRequest", ["connection_id", "action", "transaction_id", "infohashes"])
ScrapeResponse = namedtuple("ScrapeResponse", ["action", "transaction_id", "items"])
ScrapeResponseItem = namedtuple("ScrapeResponseItem", ["seeders", "completed", "leechers"])
ErrorResponse = namedtuple("ErrorResponse", ["action", "transaction_id", "message"])
structs = {
    ConnectRequest: struct.Struct(">QII"),
    ConnectResponse: struct.Struct(">IIQ"),
    AnnounceRequest: struct.Struct(">QII20s20sQQQIIIiH"),
    AnnounceResponse: struct.Struct(">IIIII"),
    CompactIPv4Peer: struct.Struct(">IH"),
    ScrapeRequest: struct.Struct(">QII"),
    ScrapeResponse: struct.Struct(">II"),
    ScrapeResponseItem: struct.Struct(">III"),
    ErrorResponse: struct.Struct(">II")
}


def decode(cls, data, offset=0):
    decoder = structs[cls]
    if cls is AnnounceResponse:
        return AnnounceResponse(*decoder.unpack_from(data, offset),
                                peers=[decode(CompactIPv4Peer, data, index) for index in range(20, len(data), 6)])
    elif cls is ScrapeResponse:
        return ScrapeResponse(*decoder.unpack_from(data, offset),
                              items=[decode(ScrapeResponseItem, data, index) for index in range(8, len(data), 12)])
    elif cls is ErrorResponse:
        return ErrorResponse(*decoder.unpack_from(data, offset), data[decoder.size:])
    return cls(*decoder.unpack_from(data, offset))


def encode(obj):
    if isinstance(obj, ScrapeRequest):
        return structs[ScrapeRequest].pack(*obj[:-1]) + b''.join(obj.infohashes)
    elif isinstance(obj, ErrorResponse):
        return structs[ErrorResponse].pack(*obj[:-1]) + obj.message
    elif isinstance(obj, AnnounceResponse):
        return structs[AnnounceResponse].pack(*obj[:-1]) + b''.join([encode(peer) for peer in obj.peers])
    return structs[type(obj)].pack(*obj)


def make_peer_id(random_part: Optional[str] = None) -> bytes:
    # see https://wiki.theory.org/BitTorrentSpecification#peer_id and https://www.bittorrent.org/beps/bep_0020.html
    # not to confuse with node id; peer id identifies uniquely the software, version and instance
    random_part = random_part or ''.join(random.choice(string.ascii_letters) for _ in range(20))
    return f"{PREFIX}-{'-'.join(map(str, version))}-{random_part}"[:20].encode()


class UDPTrackerClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.transport = None
        self.data_queue = {}
        self.timeout = timeout
        self.semaphore = asyncio.Semaphore(DEFAULT_CONCURRENCY_LIMIT)

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    async def request(self, obj, tracker_ip, tracker_port):
        self.data_queue[obj.transaction_id] = asyncio.get_running_loop().create_future()
        try:
            async with self.semaphore:
                self.transport.sendto(encode(obj), (tracker_ip, tracker_port))
                return await asyncio.wait_for(self.data_queue[obj.transaction_id], self.timeout)
        finally:
            self.data_queue.pop(obj.transaction_id, None)

    async def connect(self, tracker_ip, tracker_port):
        transaction_id = random.getrandbits(32)
        return decode(ConnectResponse,
                      await self.request(ConnectRequest(0x41727101980, 0, transaction_id), tracker_ip, tracker_port))

    @cache_concurrent
    @async_timed_cache(CONNECTION_EXPIRES_AFTER_SECONDS)
    async def ensure_connection_id(self, peer_id, tracker_ip, tracker_port):
        # peer_id is just to ensure cache coherency
        return (await self.connect(tracker_ip, tracker_port)).connection_id

    async def announce(self, info_hash, peer_id, port, tracker_ip, tracker_port, stopped=False):
        connection_id = await self.ensure_connection_id(peer_id, tracker_ip, tracker_port)
        # this should make the key deterministic but unique per info hash + peer id
        key = int.from_bytes(info_hash[:4], "big") ^ int.from_bytes(peer_id[:4], "big") ^ port
        transaction_id = random.getrandbits(32)
        req = AnnounceRequest(
            connection_id, 1, transaction_id, info_hash, peer_id, 0, 0, 0, 3 if stopped else 1, 0, key, -1, port)
        return decode(AnnounceResponse, await self.request(req, tracker_ip, tracker_port))

    async def scrape(self, infohashes, tracker_ip, tracker_port, connection_id=None):
        connection_id = await self.ensure_connection_id(None, tracker_ip, tracker_port)
        transaction_id = random.getrandbits(32)
        reply = await self.request(
            ScrapeRequest(connection_id, 2, transaction_id, infohashes), tracker_ip, tracker_port)
        return decode(ScrapeResponse, reply), connection_id

    def datagram_received(self, data: bytes, addr: (str, int)) -> None:
        if len(data) < 8:
            return
        transaction_id = int.from_bytes(data[4:8], byteorder="big", signed=False)
        if (
            transaction_id in self.data_queue
            and not self.data_queue[transaction_id].done()
        ):
            if data[3] == 3:
                return self.data_queue[transaction_id].set_exception(Exception(decode(ErrorResponse, data).message))
            return self.data_queue[transaction_id].set_result(data)
        log.debug("unexpected packet (can be a response for a previously timed out request): %s", data.hex())

    def connection_lost(self, exc: Exception = None) -> None:
        self.transport = None


class TrackerClient:
    event_controller = StreamController()

    def __init__(self, node_id, announce_port, get_servers, timeout=10.0):
        self.client = UDPTrackerClientProtocol(timeout=timeout)
        self.transport = None
        self.peer_id = make_peer_id(node_id.hex() if node_id else None)
        self.announce_port = announce_port
        self._get_servers = get_servers
        self.results = {}  # we can't probe the server before the interval, so we keep the result here until it expires
        self.tasks = {}

    async def start(self):
        self.transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: self.client, local_addr=("0.0.0.0", 0))
        self.event_controller.stream.listen(
            lambda request: self.on_hash(request[1], request[2]) if request[0] == 'search' else None)

    def stop(self):
        while self.tasks:
            self.tasks.popitem()[1].cancel()
        if self.transport is not None:
            self.transport.close()
        self.client = None
        self.transport = None
        self.event_controller.close()

    def on_hash(self, info_hash, on_announcement=None):
        if info_hash not in self.tasks:
            task = asyncio.create_task(self.get_peer_list(info_hash, on_announcement=on_announcement))
            task.add_done_callback(lambda *_: self.tasks.pop(info_hash, None))
            self.tasks[info_hash] = task

    async def announce_many(self, *info_hashes, stopped=False):
        await asyncio.gather(
            *[self._announce_many(server, info_hashes, stopped=stopped) for server in self._get_servers()],
            return_exceptions=True)

    async def _announce_many(self, server, info_hashes, stopped=False):
        tracker_ip = await resolve_host(*server, 'udp')
        still_good_info_hashes = {
            info_hash for (info_hash, (next_announcement, _)) in self.results.get(tracker_ip, {}).items()
            if time.time() < next_announcement
        }
        results = await asyncio.gather(
            *[self._probe_server(info_hash, tracker_ip, server[1], stopped=stopped)
              for info_hash in info_hashes if info_hash not in still_good_info_hashes],
            return_exceptions=True)
        if results:
            errors = sum(
                1
                for result in results
                if result is None or isinstance(result, Exception)
            )
            log.info("Tracker: finished announcing %d files to %s:%d, %d errors", len(results), *server, errors)

    async def get_peer_list(self, info_hash, stopped=False, on_announcement=None, no_port=False):
        found = []
        probes = [self._probe_server(info_hash, *server, stopped, no_port) for server in self._get_servers()]
        for done in asyncio.as_completed(probes):
            result = await done
            if result is not None:
                await asyncio.gather(*filter(asyncio.iscoroutine, [on_announcement(result)] if on_announcement else []))
                found.append(result)
        return found

    async def get_kademlia_peer_list(self, info_hash):
        responses = await self.get_peer_list(info_hash, no_port=True)
        return await announcement_to_kademlia_peers(*responses)

    async def _probe_server(self, info_hash, tracker_host, tracker_port, stopped=False, no_port=False):
        result = None
        try:
            tracker_host = await resolve_host(tracker_host, tracker_port, 'udp')
        except socket.error:
            log.warning("DNS failure while resolving tracker host: %s, skipping.", tracker_host)
            return
        self.results.setdefault(tracker_host, {})
        if info_hash in self.results[tracker_host]:
            next_announcement, result = self.results[tracker_host][info_hash]
            if time.time() < next_announcement:
                return result
        try:
            result = await self.client.announce(
                info_hash, self.peer_id, 0 if no_port else self.announce_port, tracker_host, tracker_port, stopped)
            self.results[tracker_host][info_hash] = (time.time() + result.interval, result)
        except asyncio.TimeoutError:  # todo: this is UDP, timeout is common, we need a better metric for failures
            self.results[tracker_host][info_hash] = (time.time() + 60.0, result)
            log.debug("Tracker timed out: %s:%d", tracker_host, tracker_port)
            return None
        log.debug("Announced: %s found %d peers for %s", tracker_host, len(result.peers), info_hash.hex()[:8])
        return result


def enqueue_tracker_search(info_hash: bytes, peer_q: asyncio.Queue):
    async def on_announcement(announcement: AnnounceResponse):
        peers = await announcement_to_kademlia_peers(announcement)
        log.info("Found %d peers from tracker for %s", len(peers), info_hash.hex()[:8])
        peer_q.put_nowait(peers)
    TrackerClient.event_controller.add(('search', info_hash, on_announcement))


def announcement_to_kademlia_peers(*announcements: AnnounceResponse):
    peers = [
        (str(ipaddress.ip_address(peer.address)), peer.port)
        for announcement in announcements for peer in announcement.peers if peer.port > 1024  # no privileged or 0
    ]
    return get_kademlia_peers_from_hosts(peers)


class UDPTrackerServerProtocol(asyncio.DatagramProtocol):  # for testing. Not suitable for production
    def __init__(self):
        self.transport = None
        self.known_conns = set()
        self.peers = {}

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def add_peer(self, info_hash, ip_address: str, port: int):
        self.peers.setdefault(info_hash, [])
        self.peers[info_hash].append(encode_peer(ip_address, port))

    def datagram_received(self, data: bytes, addr: (str, int)) -> None:
        if len(data) < 16:
            return
        action = int.from_bytes(data[8:12], "big", signed=False)
        if action == 0:
            req = decode(ConnectRequest, data)
            connection_id = random.getrandbits(32)
            self.known_conns.add(connection_id)
            return self.transport.sendto(encode(ConnectResponse(0, req.transaction_id, connection_id)), addr)
        elif action == 1:
            req = decode(AnnounceRequest, data)
            if req.connection_id not in self.known_conns:
                resp = encode(ErrorResponse(3, req.transaction_id, b'Connection ID missmatch.\x00'))
            else:
                compact_address = encode_peer(addr[0], req.port)
                if req.event != 3:
                    self.add_peer(req.info_hash, addr[0], req.port)
                elif compact_address in self.peers.get(req.info_hash, []):
                    self.peers[req.info_hash].remove(compact_address)
                peers = [decode(CompactIPv4Peer, peer) for peer in self.peers[req.info_hash]]
                resp = encode(AnnounceResponse(1, req.transaction_id, 1700, 0, len(peers), peers))
            return self.transport.sendto(resp, addr)


def encode_peer(ip_address: str, port: int):
    compact_ip = reduce(lambda buff, x: buff + bytearray([int(x)]), ip_address.split('.'), bytearray())
    return compact_ip + port.to_bytes(2, "big", signed=False)
