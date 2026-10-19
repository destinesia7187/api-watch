"""Lossless request decoding before FastAPI/Pydantic validate monitoring settings."""

from fastapi import Request
from fastapi.routing import APIRoute

from api_watch.checker import strict_json_loads
from api_watch.errors import APIError


class ExactJSONRequest(Request):
    async def json(self):
        if not hasattr(self, "_json"):
            try:
                self._json = strict_json_loads(await self.body(), exact_integers=False)
            except (ValueError, UnicodeError, RecursionError):
                raise APIError(400, "INVALID_JSON", "The request body must be unambiguous bounded UTF-8 JSON.") from None
        return self._json


class ExactJSONRoute(APIRoute):
    """Preserve exact numbers while retaining typed request schemas in OpenAPI."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            return await original(ExactJSONRequest(request.scope, request.receive))
        return handler
