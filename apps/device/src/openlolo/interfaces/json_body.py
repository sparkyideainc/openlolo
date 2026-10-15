"""Bounded JSON request bodies for the box page and its owner routes."""

import json

from openlolo.domain.errors import OpenLoloError


async def body(request, limit=16384):
    """The request's JSON object, read in chunks and refused past ``limit`` bytes."""
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise OpenLoloError("REQUEST_TOO_LARGE", status=413)
    try:
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise OpenLoloError("INVALID_JSON", status=400) from None
