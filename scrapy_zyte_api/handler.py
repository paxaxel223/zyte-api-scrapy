import json
import logging

from scrapy import Spider
from scrapy.core.downloader.handlers.http import HTTPDownloadHandler
from scrapy.crawler import Crawler
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Request, Response
from scrapy.settings import Settings
from scrapy.utils.defer import deferred_from_coro
from twisted.internet.defer import Deferred, inlineCallbacks
from zyte_api.aio.client import AsyncClient, create_session
from zyte_api.aio.errors import RequestError

logger = logging.getLogger("scrapy-zyte-api")


class ScrapyZyteAPIDownloadHandler(HTTPDownloadHandler):
    def __init__(
        self, settings: Settings, crawler: Crawler, client: AsyncClient = None
    ):
        super().__init__(settings=settings, crawler=crawler)
        self._client: AsyncClient = client if client else AsyncClient()
        # TODO Check do I need to verify reactor
        # verify_installed_reactor(
        #     "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
        # )
        self._stats = crawler.stats
        self._session = create_session()

    def download_request(self, request: Request, spider: Spider) -> Deferred:
        if request.meta.get("zyte_api"):
            return deferred_from_coro(self._download_request(request, spider))
        else:
            return super().download_request(request, spider)

    async def _download_request(self, request: Request, spider: Spider):
        api_data = {"url": request.url, "browserHtml": True}
        allowed_keys = {"javascript", "geolocation", "echoData"}
        api_params = request.meta["zyte_api"]
        if not isinstance(api_params, dict):
            logger.error(
                "zyte_api parameters in the request meta should be "
                f"provided as dictionary (got {type(api_params)} instead)."
            )
            raise IgnoreRequest()
        for key, value in api_params.items():
            if key not in allowed_keys:
                logger.warning(
                    f"Key `{key}` isn't allowed in Zyte API parameters, skipping."
                )
                continue
            # Protect default settings (request url and browserHtml)
            if key in api_data:
                logger.warning(
                    "Key `{key}` is already in Zyte API parameters "
                    f"({api_data[key]}) and can't be overwritten, skipping."
                )
                continue
            # TODO Decide how to validate echoData or do I need to validate it at all?
            api_data[key] = value
        # TODO Check where to pick jobId
        try:
            api_response = await self._client.request_raw(
                api_data, session=self._session
            )
        except RequestError as er:
            error_message = self.get_request_error_message(er)
            logger.error(
                f"Got Zyte API error ({er.status}) while processing URL ({request.url}): {error_message}"
            )
            # Skipping requests if API error
            raise IgnoreRequest()
        # TODO Handle other errors
        self._stats.inc_value("scrapy-zyte-api/request_count")
        body = api_response["browserHtml"].encode("utf-8")
        # TODO Add retrying support?
        return Response(
            url=request.url,
            # TODO Add status code data to the API
            status=200,
            body=body,
            request=request,
            flags=["zyte-api"],
            # API provides no page-request-related headers, so returning no headers
        )

    @inlineCallbacks
    def close(self) -> Deferred:
        yield super().close()
        yield deferred_from_coro(self._close())

    async def _close(self) -> None:  # NOQA
        await self._session.close()

    @staticmethod
    def get_request_error_message(error: RequestError) -> str:
        base_message = ""
        if hasattr(error, "message"):
            base_message = error.message
        if not hasattr(error, "response_content"):
            return base_message
        try:
            error_data = json.loads(error.response_content.decode("utf-8"))
        except (AttributeError, TypeError, ValueError):
            return base_message
        if error_data.get("detail"):
            return error_data["detail"]
        return base_message
