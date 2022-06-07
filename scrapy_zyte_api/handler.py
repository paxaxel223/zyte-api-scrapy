import json
import logging
import os
from typing import Any, Dict, Generator, Optional, Union

from scrapy import Spider
from scrapy.core.downloader.handlers.http import HTTPDownloadHandler
from scrapy.crawler import Crawler
from scrapy.exceptions import IgnoreRequest, NotConfigured
from scrapy.http import Request
from scrapy.settings import Settings
from scrapy.utils.defer import deferred_from_coro
from scrapy.utils.reactor import verify_installed_reactor
from twisted.internet.defer import Deferred, inlineCallbacks
from zyte_api.aio.client import AsyncClient, create_session
from zyte_api.aio.errors import RequestError

from .responses import ZyteAPIResponse, ZyteAPITextResponse, _process_response

logger = logging.getLogger(__name__)


class ScrapyZyteAPIDownloadHandler(HTTPDownloadHandler):
    def __init__(
        self, settings: Settings, crawler: Crawler, client: AsyncClient = None
    ):
        super().__init__(settings=settings, crawler=crawler)
        self._client: AsyncClient = (
            client
            or AsyncClient(
                api_key=settings.get('ZYTE_API_KEY'),
                n_conn=settings.getint('CONCURRENT_REQUESTS'),
            )
        )
        verify_installed_reactor(
            "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
        )
        self._stats = crawler.stats
        self._job_id = crawler.settings.get("JOB")
        self._zyte_api_default_params = settings.getdict("ZYTE_API_DEFAULT_PARAMS")
        self._session = create_session(self._client.n_conn)

    @classmethod
    def from_crawler(cls, crawler):
        zyte_api_key = crawler.settings.get("ZYTE_API_KEY") or os.getenv("ZYTE_API_KEY")
        if not zyte_api_key:
            logger.warning(
                "'ZYTE_API_KEY' must be set in the spider settings or env var "
                "in order for ScrapyZyteAPIDownloadHandler to work."
            )
            raise NotConfigured

        logger.info(f"Using Zyte API Key: {zyte_api_key[:7]}")
        client = AsyncClient(api_key=zyte_api_key)
        return cls(crawler.settings, crawler, client)

    def download_request(self, request: Request, spider: Spider) -> Deferred:
        api_params = self._prepare_api_params(request)
        if api_params:
            return deferred_from_coro(
                self._download_request(api_params, request, spider)
            )
        return super().download_request(request, spider)

    def _prepare_api_params(self, request: Request) -> Optional[dict]:
        meta_params = request.meta.get("zyte_api")
        if not meta_params and meta_params != {}:
            return None

        if meta_params is True:
            meta_params = {}

        api_params: Dict[str, Any] = self._zyte_api_default_params or {}
        try:
            api_params.update(meta_params)
        except TypeError:
            logger.error(
                f"zyte_api parameters in the request meta should be "
                f"provided as dictionary, got {type(request.meta.get('zyte_api'))} "
                f"instead ({request.url})."
            )
            raise IgnoreRequest()
        return api_params

    async def _download_request(
        self, api_params: dict, request: Request, spider: Spider
    ) -> Optional[Union[ZyteAPITextResponse, ZyteAPIResponse]]:
        # Define url by default
        api_data = {**{"url": request.url}, **api_params}
        if self._job_id is not None:
            api_data["jobId"] = self._job_id
        try:
            api_response = await self._client.request_raw(
                api_data, session=self._session
            )
        except RequestError as er:
            error_message = self._get_request_error_message(er)
            logger.error(
                f"Got Zyte API error ({er.status}) while processing URL ({request.url}): {error_message}"
            )
            raise IgnoreRequest()
        except Exception as er:
            logger.error(
                f"Got an error when processing Zyte API request ({request.url}): {er}"
            )
            raise IgnoreRequest()

        self._stats.inc_value("scrapy-zyte-api/request_count")
        return _process_response(api_response, request)

    @inlineCallbacks
    def close(self) -> Generator:
        yield super().close()
        yield deferred_from_coro(self._close())

    async def _close(self) -> None:  # NOQA
        await self._session.close()

    @staticmethod
    def _get_request_error_message(error: RequestError) -> str:
        if hasattr(error, "message"):
            base_message = error.message
        else:
            base_message = str(error)
        if not hasattr(error, "response_content"):
            return base_message
        try:
            error_data = json.loads(error.response_content.decode("utf-8"))
        except (AttributeError, TypeError, ValueError):
            return base_message
        if error_data.get("detail"):
            return error_data["detail"]
        return base_message
