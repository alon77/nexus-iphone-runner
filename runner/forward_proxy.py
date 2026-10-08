"""forward_proxy — one hop of an iPhone run's path, HTTP and websockets both.

Two uses of the same hop (`guide iphone:tunnel`):
1. The gate, on Alon's machine, in front of each dev origin: refuses any request without the run token (403) and
   forwards the rest to the origin under its real Host, TLS verify off (the dev cert is self-signed).
2. The runner proxy, on the GitHub Mac, on 443/6001 behind the simulator's /etc/hosts: terminates Safari's TLS with
   the run's throwaway cert, adds the run token and forwards to the cloudflared tunnel URL.

Standalone on purpose: the runner repo carries this file as-is, so it imports nothing from lib/.
"""

import argparse
import asyncio
import hmac
import os
import ssl
from dataclasses import dataclass
from typing import Optional

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

TOKEN_HEADER = "X-Nexus-Run-Token"
FORBIDDEN = 403
BAD_GATEWAY = 502
CHUNK_BYTES = 65536
HOP_BY_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
                      "transfer-encoding", "upgrade", "host"}
WEBSOCKET_HANDSHAKE_HEADERS = {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
                               "sec-websocket-protocol"}
ROUTE_KEY = web.AppKey("route", object)
SESSION_KEY = web.AppKey("session", aiohttp.ClientSession)


@dataclass
class Route:
    upstream: str
    upstream_host: str
    required_token: Optional[str] = None
    added_token: Optional[str] = None
    verify_tls: bool = False


def _token_accepted(route: Route, request: web.Request) -> bool:
    if route.required_token is None:
        return True
    presented = request.headers.get(TOKEN_HEADER, "")
    return hmac.compare_digest(presented.encode(), route.required_token.encode())


def _upstream_headers(route: Route, incoming) -> dict:
    dropped = HOP_BY_HOP_HEADERS | {TOKEN_HEADER.lower()}
    headers = [(name, value) for name, value in incoming.items() if name.lower() not in dropped]
    headers.append(("Host", route.upstream_host))
    if route.added_token:
        headers.append((TOKEN_HEADER, route.added_token))
    return CIMultiDict(headers)


def _downstream_headers(upstream_headers):
    return CIMultiDict([(name, value) for name, value in upstream_headers.items()
                       if name.lower() not in HOP_BY_HOP_HEADERS])


def _is_websocket(request: web.Request) -> bool:
    return request.headers.get("Upgrade", "").lower() == "websocket"


def _ssl_for(route: Route):
    return None if route.verify_tls else False


async def _pump(source, sink):
    async for message in source:
        if message.type == aiohttp.WSMsgType.TEXT:
            await sink.send_str(message.data)
        elif message.type == aiohttp.WSMsgType.BINARY:
            await sink.send_bytes(message.data)
        else:
            break


async def _forward_websocket(request: web.Request, headers) -> web.WebSocketResponse:
    route = request.app[ROUTE_KEY]
    protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
    for name in WEBSOCKET_HANDSHAKE_HEADERS:
        headers.popall(name, None)
    socket_url = route.upstream.replace("http", "ws", 1) + request.path_qs
    async with request.app[SESSION_KEY].ws_connect(socket_url, headers=headers, protocols=protocols,
                                                   ssl=_ssl_for(route)) as upstream:
        downstream = web.WebSocketResponse(protocols=[upstream.protocol] if upstream.protocol else ())
        await downstream.prepare(request)
        pumps = [asyncio.ensure_future(_pump(downstream, upstream)), asyncio.ensure_future(_pump(upstream, downstream))]
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for pump in pumps:
            pump.cancel()
        await downstream.close()
        return downstream


async def _forward_http(request: web.Request, headers) -> web.StreamResponse:
    route = request.app[ROUTE_KEY]
    body = await request.read() if request.body_exists else None
    async with request.app[SESSION_KEY].request(request.method, route.upstream + request.path_qs, headers=headers,
                                                data=body, allow_redirects=False, ssl=_ssl_for(route)) as upstream:
        response = web.StreamResponse(status=upstream.status, reason=upstream.reason,
                                      headers=_downstream_headers(upstream.headers))
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(CHUNK_BYTES):
            await response.write(chunk)
        await response.write_eof()
        return response


async def _forward(request: web.Request):
    route = request.app[ROUTE_KEY]
    if not _token_accepted(route, request):
        return web.Response(status=FORBIDDEN, text="forbidden")
    headers = _upstream_headers(route, request.headers)
    try:
        if _is_websocket(request):
            return await _forward_websocket(request, headers)
        return await _forward_http(request, headers)
    except aiohttp.ClientError as error:
        return web.Response(status=BAD_GATEWAY, text=f"upstream unreachable: {type(error).__name__}")


async def _open_session(app: web.Application):
    app[SESSION_KEY] = aiohttp.ClientSession(auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar())
    yield
    await app[SESSION_KEY].close()


def make_app(route: Route) -> web.Application:
    app = web.Application(client_max_size=0)
    app[ROUTE_KEY] = route
    app.cleanup_ctx.append(_open_session)
    app.router.add_route("*", "/{tail:.*}", _forward)
    return app


def _arguments():
    parser = argparse.ArgumentParser(description="one hop of an iPhone run: gate or runner proxy")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--upstream-host", required=True)
    parser.add_argument("--required-token-env", help="env var holding the token every request must carry")
    parser.add_argument("--added-token-env", help="env var holding the token this hop adds")
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--verify-tls", action="store_true")
    return parser.parse_args()


def _route_from(arguments) -> Route:
    return Route(upstream=arguments.upstream, upstream_host=arguments.upstream_host,
                 required_token=os.environ[arguments.required_token_env] if arguments.required_token_env else None,
                 added_token=os.environ[arguments.added_token_env] if arguments.added_token_env else None,
                 verify_tls=arguments.verify_tls)


def _listen_tls(arguments):
    if not arguments.cert:
        return None
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(arguments.cert, arguments.key)
    return context


def main():
    arguments = _arguments()
    web.run_app(make_app(_route_from(arguments)), host=arguments.listen_host, port=arguments.listen_port,
                ssl_context=_listen_tls(arguments), print=None)


if __name__ == "__main__":
    main()
