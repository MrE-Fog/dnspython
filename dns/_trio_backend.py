# Copyright (C) Dnspython Contributors, see LICENSE for text of ISC license

"""trio async I/O library query support"""

import socket
import trio
import trio.socket  # type: ignore

import dns._asyncbackend
import dns.exception
import dns.inet


def _maybe_timeout(timeout):
    if timeout:
        return trio.move_on_after(timeout)
    else:
        return dns._asyncbackend.NullContext()


# for brevity
_lltuple = dns.inet.low_level_address_tuple

# pylint: disable=redefined-outer-name


class DatagramSocket(dns._asyncbackend.DatagramSocket):
    def __init__(self, socket):
        super().__init__(socket.family)
        self.socket = socket

    async def sendto(self, what, destination, timeout):
        with _maybe_timeout(timeout):
            return await self.socket.sendto(what, destination)
        raise dns.exception.Timeout(
            timeout=timeout
        )  # pragma: no cover  lgtm[py/unreachable-statement]

    async def recvfrom(self, size, timeout):
        with _maybe_timeout(timeout):
            return await self.socket.recvfrom(size)
        raise dns.exception.Timeout(timeout=timeout)  # lgtm[py/unreachable-statement]

    async def close(self):
        self.socket.close()

    async def getpeername(self):
        return self.socket.getpeername()

    async def getsockname(self):
        return self.socket.getsockname()


class StreamSocket(dns._asyncbackend.StreamSocket):
    def __init__(self, family, stream, tls=False):
        self.family = family
        self.stream = stream
        self.tls = tls

    async def sendall(self, what, timeout):
        with _maybe_timeout(timeout):
            return await self.stream.send_all(what)
        raise dns.exception.Timeout(timeout=timeout)  # lgtm[py/unreachable-statement]

    async def recv(self, size, timeout):
        with _maybe_timeout(timeout):
            return await self.stream.receive_some(size)
        raise dns.exception.Timeout(timeout=timeout)  # lgtm[py/unreachable-statement]

    async def close(self):
        await self.stream.aclose()

    async def getpeername(self):
        if self.tls:
            return self.stream.transport_stream.socket.getpeername()
        else:
            return self.stream.socket.getpeername()

    async def getsockname(self):
        if self.tls:
            return self.stream.transport_stream.socket.getsockname()
        else:
            return self.stream.socket.getsockname()


try:
    import httpx

    import httpcore
    import httpcore.backends.base
    import httpcore.backends.trio

    from dns.query import _compute_times, _remaining, _expiration_for_this_attempt

    class _NetworkBackend(httpcore.backends.base.AsyncNetworkBackend):
        def __init__(self, resolver, local_port, bootstrap_address, family):
            super().__init__()
            self._local_port = local_port
            self._resolver = resolver
            self._bootstrap_address = bootstrap_address
            self._family = family

        async def connect_tcp(
            self, host, port, timeout, local_address
        ):  # pylint: disable=signature-differs
            addresses = []
            _, expiration = _compute_times(timeout)
            if dns.inet.is_address(host):
                addresses.append(host)
            elif self._bootstrap_address is not None:
                addresses.append(self._bootstrap_address)
            else:
                timeout = _remaining(expiration)
                family = self._family
                if local_address:
                    family = dns.inet.af_for_address(local_address)
                answers = await self._resolver.resolve_name(
                    host, family=family, lifetime=timeout
                )
                addresses = answers.addresses()
            for address in addresses:
                try:
                    af = dns.inet.af_for_address(address)
                    if local_address is not None or self._local_port != 0:
                        source = (local_address, self._local_port)
                    else:
                        source = None
                    destination = (address, port)
                    attempt_expiration = _expiration_for_this_attempt(2.0, expiration)
                    timeout = _remaining(attempt_expiration)
                    sock = await Backend().make_socket(
                        af, socket.SOCK_STREAM, 0, source, destination, timeout
                    )
                    return httpcore.backends.trio.TrioStream(sock.stream)
                except Exception:
                    continue
            raise httpcore.ConnectError

        async def connect_unix_socket(
            self, path, timeout
        ):  # pylint: disable=signature-differs
            raise NotImplementedError

        async def sleep(self, seconds):  # pylint: disable=signature-differs
            await trio.sleep(seconds)

    class _HTTPTransport(httpx.AsyncHTTPTransport):
        def __init__(
            self,
            *args,
            local_port=0,
            bootstrap_address=None,
            resolver=None,
            family=socket.AF_UNSPEC,
            **kwargs,
        ):
            if resolver is None:
                # pylint: disable=import-outside-toplevel,redefined-outer-name
                import dns.asyncresolver

                resolver = dns.asyncresolver.Resolver()
            super().__init__(*args, **kwargs)
            self._pool._network_backend = _NetworkBackend(
                resolver, local_port, bootstrap_address, family
            )

except ImportError:
    _HTTPTransport = dns._asyncbackend.NullTransport  # type: ignore


class Backend(dns._asyncbackend.Backend):
    def name(self):
        return "trio"

    async def make_socket(
        self,
        af,
        socktype,
        proto=0,
        source=None,
        destination=None,
        timeout=None,
        ssl_context=None,
        server_hostname=None,
    ):
        s = trio.socket.socket(af, socktype, proto)
        stream = None
        try:
            if source:
                await s.bind(_lltuple(source, af))
            if socktype == socket.SOCK_STREAM:
                connected = False
                with _maybe_timeout(timeout):
                    await s.connect(_lltuple(destination, af))
                    connected = True
                if not connected:
                    raise dns.exception.Timeout(
                        timeout=timeout
                    )  # lgtm[py/unreachable-statement]
        except Exception:  # pragma: no cover
            s.close()
            raise
        if socktype == socket.SOCK_DGRAM:
            return DatagramSocket(s)
        elif socktype == socket.SOCK_STREAM:
            stream = trio.SocketStream(s)
            tls = False
            if ssl_context:
                tls = True
                try:
                    stream = trio.SSLStream(
                        stream, ssl_context, server_hostname=server_hostname
                    )
                except Exception:  # pragma: no cover
                    await stream.aclose()
                    raise
            return StreamSocket(af, stream, tls)
        raise NotImplementedError(
            "unsupported socket " + f"type {socktype}"
        )  # pragma: no cover

    async def sleep(self, interval):
        await trio.sleep(interval)

    def get_transport_class(self):
        return _HTTPTransport
