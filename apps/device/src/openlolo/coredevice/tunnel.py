"""One root-free, in-process RSD tunnel and its RemoteServiceDiscovery handshake.

Both transports hand over a provider with ``start_tcp_tunnel()``; the tunnel terminates in
pymobiledevice3's userspace TCP/IP stack, so no kernel utun and no root are needed on the Pi.
The stack is a process-wide singleton, so this worker keeps exactly one tunnel at a time.
"""

import asyncio
from contextlib import AsyncExitStack

from openlolo.domain.errors import OpenLoloError

_lock = asyncio.Lock()


class Tunnel:
    def __init__(self, provider, closers, connect_deadline: float = 30, mode: str = "userspace"):
        self.provider = provider
        self.closers = list(closers)
        self.connect_deadline = connect_deadline
        self.mode = mode
        self.stack: AsyncExitStack | None = None
        self.rsd = None
        self.tun = None
        self.result = None

    async def open(self):
        from pymobiledevice3.remote import tunnel_service, userspace_tunnel
        from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService

        userspace = self.mode == "userspace"
        async with _lock:
            if userspace_tunnel.USERSPACE_ACTIVE:
                raise OpenLoloError("TUNNEL_BUSY", "A previous tunnel is still shutting down")
            tunnel_service.USE_USERSPACE_TUNNEL = userspace
            stack = AsyncExitStack()
            try:
                for closer in self.closers:
                    stack.push_async_callback(closer)
                result = await asyncio.wait_for(
                    stack.enter_async_context(self.provider.start_tcp_tunnel()), self.connect_deadline
                )
                tun = result.client.tun
                if userspace:
                    tun.set_peer(result.address)
                    dial_plane = await stack.enter_async_context(
                        userspace_tunnel.UserspaceDialPlane(tun, result.address)
                    )
                    rsd = RemoteServiceDiscoveryService(
                        (result.address, result.port),
                        open_connection=dial_plane.dial,
                        auxiliary_metadata=result.auxiliary_metadata,
                    )
                else:
                    rsd = RemoteServiceDiscoveryService(
                        (result.address, result.port), auxiliary_metadata=result.auxiliary_metadata
                    )
                stack.push_async_callback(rsd.close)
                await asyncio.wait_for(rsd.connect(), self.connect_deadline)
            except BaseException:
                await stack.aclose()
                tunnel_service.USE_USERSPACE_TUNNEL = False
                raise
            self.stack, self.rsd, self.tun, self.result = stack, rsd, tun, result
            if userspace:
                # Device-initiated media (the HID authentication stream) must terminate on the
                # userspace stack; the library finds that stack through these module globals.
                userspace_tunnel._active_tunnel = self
                userspace_tunnel.USERSPACE_ACTIVE = True
            return rsd

    async def wait_closed(self):
        if self.result is not None:
            await self.result.client.wait_closed()

    async def aclose(self):
        from pymobiledevice3.remote import tunnel_service, userspace_tunnel

        async with _lock:
            stack, self.stack = self.stack, None
            if stack is None:
                return
            self.rsd = self.tun = self.result = None
            if userspace_tunnel._active_tunnel is self:
                userspace_tunnel._active_tunnel = None
                userspace_tunnel.USERSPACE_ACTIVE = False
                tunnel_service.USE_USERSPACE_TUNNEL = False
            await stack.aclose()
