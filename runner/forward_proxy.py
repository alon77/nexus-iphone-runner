"""forward_proxy — one hop of an iPhone run's path, HTTP and websockets both.

Two uses of the same hop (`guide iphone:tunnel`):
1. The gate, on Alon's machine, in front of each dev origin: refuses any request without the run token (403) and
   forwards the rest to the origin under its real Host, TLS verify off (the dev cert is self-signed).
2. The runner proxy, on the GitHub Mac, on 443/6001 behind the simulator's /etc/hosts: terminates Safari's TLS with
   the run's throwaway cert, adds the run token and forwards to the cloudflared tunnel URL.

The dev origin sends no-store, so Safari caches nothing and every asset would cross the tunnel on every load. The
runner proxy keeps every GET 200 that carries an ETag and no Set-Cookie (never a Range) for the session; before each
spec the runner POSTs REVALIDATE_PATH, the proxy sends every cached path + ETag to the gate's VALIDATE_PATH in one
trip, the gate asks its origin locally, and every entry the origin no longer confirms is dropped. A static asset
(STATIC_ASSET_SUFFIXES) is kept under its path alone: nginx serves the file whatever its query, and alpha's local
stylesheets carry a ?v<time()> that changes on every page load.

Standalone on purpose: the runner repo carries this file as-is, so it imports nothing from lib/.
"""

import argparse
import asyncio
import hmac
import os
import ssl
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

TOKEN_HEADER = "X-Nexus-Run-Token"
RUNNER_HEADER = "X-Nexus-Runner"
OK = 200
NOT_MODIFIED = 304
TUNNEL_EDGE_SENDS_UNCOMPRESSED = {"Accept-Encoding": "identity"}
FORBIDDEN = 403
BAD_GATEWAY = 502
VALIDATE_PATH = "/__nexus/validate"
REVALIDATE_PATH = "/__nexus/revalidate"
CACHE_ENTRY_LIMIT_BYTES = 16 * 1024 * 1024
UNCACHED_RESPONSE_HEADERS = {"content-length"}
STATIC_ASSET_SUFFIXES = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff", ".woff2",
                         ".ttf", ".mp4", ".webm")
CHUNK_BYTES = 65536
HOP_BY_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
                      "transfer-encoding", "upgrade", "host"}
WEBSOCKET_HANDSHAKE_HEADERS = {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
                               "sec-websocket-protocol"}
ROUTE_KEY = web.AppKey("route", object)
SESSION_KEY = web.AppKey("session", aiohttp.ClientSession)
CACHE_KEY = web.AppKey("cache", dict)


@dataclass
class Route:
    upstream: str
    upstream_host: str
    required_token: Optional[str] = None
    added_token: Optional[str] = None
    verify_tls: bool = False
    cache_static: bool = False


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


def _print_timing(request: web.Request, timing: dict):
    finished, started = time.monotonic(), timing["started"]
    print(f"{time.strftime('%H:%M:%S')} {request.method} {request.path} {timing['status']} "
          f"headers {(timing['headers_at'] - started) * 1000:.0f}ms total {(finished - started) * 1000:.0f}ms "
          f"{timing['sent']}B", flush=True)


def _cache_candidate(request: web.Request) -> bool:
    return request.app[ROUTE_KEY].cache_static and request.method == "GET" and "Range" not in request.headers


def _cacheable(upstream) -> bool:
    return upstream.status == OK and "ETag" in upstream.headers and "Set-Cookie" not in upstream.headers


def _cache_key(request: web.Request) -> str:
    return request.path if request.path.endswith(STATIC_ASSET_SUFFIXES) else request.path_qs


def _keep(request: web.Request, answered: dict):
    upstream, body = answered["upstream"], answered["body"]
    headers = [(name, value) for name, value in _downstream_headers(upstream.headers).items()
               if name.lower() not in UNCACHED_RESPONSE_HEADERS]
    request.app[CACHE_KEY][_cache_key(request)] = {"headers": headers, "body": body, "etag": upstream.headers["ETag"],
                                               "accept_encoding": request.headers.get("Accept-Encoding", "")}


def _from_cache(request: web.Request) -> Optional[web.Response]:
    entry = request.app[CACHE_KEY].get(_cache_key(request)) if _cache_candidate(request) else None
    if entry is None:
        return None
    print(f"{time.strftime('%H:%M:%S')} {request.method} {request.path} {OK} cache {len(entry['body'])}B", flush=True)
    return web.Response(status=OK, headers=CIMultiDict(entry["headers"]), body=entry["body"])


async def _relay(request: web.Request, upstream) -> tuple:
    response = web.StreamResponse(status=upstream.status, reason=upstream.reason,
                                  headers=_downstream_headers(upstream.headers))
    await response.prepare(request)
    keeping = _cache_candidate(request) and _cacheable(upstream)
    chunks, sent = [], 0
    async for chunk in upstream.content.iter_chunked(CHUNK_BYTES):
        await response.write(chunk)
        sent += len(chunk)
        keeping = keeping and sent <= CACHE_ENTRY_LIMIT_BYTES
        chunks = chunks + [chunk] if keeping else []
    await response.write_eof()
    if keeping:
        _keep(request, {"upstream": upstream, "body": b"".join(chunks)})
    return response, sent


async def _forward_http(request: web.Request, headers) -> web.StreamResponse:
    route = request.app[ROUTE_KEY]
    started = time.monotonic()
    body = await request.read() if request.body_exists else None
    async with request.app[SESSION_KEY].request(request.method, route.upstream + request.path_qs, headers=headers,
                                                data=body, allow_redirects=False, ssl=_ssl_for(route)) as upstream:
        headers_at = time.monotonic()
        response, sent = await _relay(request, upstream)
        _print_timing(request, {"status": upstream.status, "started": started, "headers_at": headers_at,
                                "sent": sent})
        return response


async def _forward(request: web.Request):
    route = request.app[ROUTE_KEY]
    if not _token_accepted(route, request):
        return web.Response(status=FORBIDDEN, text="forbidden")
    headers = _upstream_headers(route, request.headers)
    try:
        if _is_websocket(request):
            return await _forward_websocket(request, headers)
        return _from_cache(request) or await _forward_http(request, headers)
    except aiohttp.ClientError as error:
        return web.Response(status=BAD_GATEWAY, text=f"upstream unreachable: {type(error).__name__}")


async def _still_fresh(app: web.Application, cached: tuple) -> bool:
    path, entry = cached
    route = app[ROUTE_KEY]
    if not path.startswith("/"):
        return False
    headers = {"Host": route.upstream_host, "If-None-Match": entry["etag"],
               "Accept-Encoding": entry.get("accept_encoding", "")}
    try:
        async with app[SESSION_KEY].get(route.upstream + path, headers=headers, allow_redirects=False,
                                        ssl=_ssl_for(route)) as answer:
            return answer.status == NOT_MODIFIED or answer.headers.get("ETag") == entry["etag"]
    except aiohttp.ClientError:
        return False


async def _validate(request: web.Request) -> web.Response:
    if not _token_accepted(request.app[ROUTE_KEY], request):
        return web.Response(status=FORBIDDEN, text="forbidden")
    entries = await request.json()
    fresh = await asyncio.gather(*(_still_fresh(request.app, cached) for cached in entries.items()))
    return web.json_response(dict(zip(entries, fresh)))


async def _confirmed_paths(app: web.Application, entries: dict) -> dict:
    route = app[ROUTE_KEY]
    headers = {"Host": route.upstream_host, TOKEN_HEADER: route.added_token or "", **TUNNEL_EDGE_SENDS_UNCOMPRESSED}
    try:
        async with app[SESSION_KEY].post(route.upstream + VALIDATE_PATH, json=entries, headers=headers,
                                         ssl=_ssl_for(route)) as answer:
            return await answer.json() if answer.status == OK else {}
    except (aiohttp.ClientError, ValueError):
        return {}


async def _revalidate(request: web.Request) -> web.Response:
    cache = request.app[CACHE_KEY]
    entries = {path: {"etag": entry["etag"], "accept_encoding": entry["accept_encoding"]}
               for path, entry in cache.items()}
    confirmed = await _confirmed_paths(request.app, entries) if entries else {}
    stale = [path for path in entries if confirmed.get(path) is not True]
    for path in stale:
        cache.pop(path, None)
    return web.json_response({"checked": len(entries), "dropped": len(stale)})


async def _open_session(app: web.Application):
    app[SESSION_KEY] = aiohttp.ClientSession(auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar())
    yield
    await app[SESSION_KEY].close()


def make_app(route: Route) -> web.Application:
    app = web.Application(client_max_size=0)
    app[ROUTE_KEY] = route
    app[CACHE_KEY] = {}
    app.cleanup_ctx.append(_open_session)
    if route.required_token is not None:
        app.router.add_post(VALIDATE_PATH, _validate)
    if route.cache_static:
        app.router.add_post(REVALIDATE_PATH, _revalidate)
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
    parser.add_argument("--cache-static", action="store_true", help="keep ETag'd GETs for the session (runner side)")
    return parser.parse_args()


def _route_from(arguments) -> Route:
    return Route(upstream=arguments.upstream, upstream_host=arguments.upstream_host,
                 required_token=os.environ[arguments.required_token_env] if arguments.required_token_env else None,
                 added_token=os.environ[arguments.added_token_env] if arguments.added_token_env else None,
                 verify_tls=arguments.verify_tls, cache_static=arguments.cache_static)


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
