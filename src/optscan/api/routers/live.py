"""The live feed's two endpoints: a status poll and a server sent event stream.

The stream is a plain text/event-stream response. No websocket, and the reasoning is
in optscan.live.hub: there is no vendor push to forward on the tier this project runs
on, the traffic is one directional, and EventSource reconnects on its own where a
websocket would need that written by hand.

The body is an async generator even though every other endpoint here is sync, and the
reason is disconnect detection. A stream has no natural end: when there is nothing to
say it emits a keepalive rather than closing. A sync generator has no way to ask
whether the client is still there, so it would sit in the loop forever after the
browser navigated away, holding a subscription that keeps a symbol being polled. Only
the ASGI receive channel knows, and reaching it means awaiting.

The hub's queue is a plain thread queue, because the hub is threaded and knows nothing
about event loops. It is waited on in a worker thread in short slices: long enough that
the thread churn is irrelevant at one dashboard's worth of connections, short enough
that a closed tab stops being polled in about a second rather than at the next
heartbeat.
"""

from __future__ import annotations

import asyncio
import json
import queue
import time
from collections.abc import AsyncIterator
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from optscan.api.deps import SettingsDep, live_hub
from optscan.api.schemas import LiveStatusOut
from optscan.live import Subscription
from optscan.logging import get_logger

router = APIRouter(tags=["live"])

log = get_logger("optscan.api.live")

#: Seconds of silence before a comment frame is sent, to keep intermediaries from
#: closing an idle stream and to give the browser something to time out against.
HEARTBEAT_SECONDS = 15.0

#: How long each wait on the hub's queue blocks before the loop checks whether the
#: client is still connected. Not a heartbeat: nothing is written on these.
DISCONNECT_CHECK_SECONDS = 1.0

# Annotated aliases rather than calls in default arguments, the same convention deps.py
# uses. Same wiring, and it keeps the linter's general objection to calls in defaults
# intact for the places where it is right.
SymbolQuery = Annotated[str, Query(min_length=1, max_length=32)]
ExpiryQuery = Annotated[date | None, Query()]

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Nginx and friends buffer streaming responses by default, which turns a live feed
    # into a batch delivery at the end of the request.
    "X-Accel-Buffering": "no",
}


@router.get("/live/status", response_model=LiveStatusOut)
def live_status(settings: SettingsDep) -> LiveStatusOut:
    """What the feed is doing, without opening a stream.

    Always answers, including when the feed is disabled, because "off" is the fact the
    UI most needs in order to label the rest of the screen as stored.
    """
    return LiveStatusOut(**live_hub(settings).status().as_dict())


@router.get("/live/stream")
def live_stream(
    request: Request,
    settings: SettingsDep,
    symbol: SymbolQuery,
    expiry: ExpiryQuery = None,
) -> StreamingResponse:
    """Subscribe to one symbol and expiry.

    Refuses rather than opening a stream that can never carry anything: a disabled feed
    is a 409 with the setting to change, and passing the symbol ceiling is a 429 with
    the reason. A stream that connects and stays silent is the failure mode this is
    written to avoid, because it looks exactly like a market that has stopped moving.
    """
    hub = live_hub(settings)
    if not settings.live_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The live feed is off, so this dashboard is showing stored captures. "
                "Set OPTSCAN_LIVE_ENABLED=true and configure a provider to turn it on."
            ),
        )

    try:
        subscription = hub.subscribe(symbol, expiry)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
        ) from error

    return StreamingResponse(
        _events(request, hub, subscription),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _events(request: Request, hub, subscription: Subscription) -> AsyncIterator[str]:
    """Drain one subscriber's queue onto the wire until the client goes away.

    The release in the finally block is the important line in this file. Without it a
    page navigation leaves a subscription behind, the hub keeps polling a symbol nobody
    is watching, and the request budget drains into closed tabs.
    """
    last_beat = time.monotonic()
    try:
        yield _frame("hello", {"symbol": subscription.symbol, "id": subscription.id})
        while not await request.is_disconnected():
            if subscription.closed:
                yield _frame("bye", {"reason": "The server stopped the live feed."})
                return
            try:
                name, payload = await asyncio.to_thread(
                    subscription.events.get, True, DISCONNECT_CHECK_SECONDS
                )
            except queue.Empty:
                if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                    last_beat = time.monotonic()
                    # A comment frame. EventSource ignores it; intermediaries do not.
                    yield ": keepalive\n\n"
                continue
            last_beat = time.monotonic()
            yield _frame(name, payload)
    finally:
        hub.release(subscription)


def _frame(event: str, payload: dict) -> str:
    """One SSE frame.

    The payload is serialized as a single line. A raw newline inside a data field would
    be read as a field separator and silently truncate the event, which is the classic
    way an SSE stream starts delivering half messages.
    """
    body = json.dumps(payload, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n"
