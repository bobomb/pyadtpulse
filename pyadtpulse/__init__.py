"""Base Python Class for pyadtpulse."""

import logging
import asyncio
import re
from threading import Thread, RLock
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from aiohttp import (
    ClientConnectionError,
    ClientResponse,
    ClientResponseError,
    ClientSession,
)
from bs4 import BeautifulSoup

from pyadtpulse.const import (
    ADT_DEFAULT_HTTP_HEADERS,
    ADT_DEFAULT_VERSION,
    ADT_DEVICE_URI,
    ADT_HTTP_REFERER_URIS,
    ADT_LOGIN_URI,
    ADT_LOGOUT_URI,
    ADT_ORB_URI,
    ADT_SYNC_CHECK_URI,
    ADT_SYSTEM_URI,
    ADT_TIMEOUT_INTERVAL,
    ADT_TIMEOUT_URI,
    API_PREFIX,
    DEFAULT_API_HOST,
    ADT_DEFAULT_POLL_INTERVAL,
)
from pyadtpulse.util import handle_response, make_soup

import uvloop

# FIXME -- circular reference
# from pyadtpulse.site import ADTPulseSite

if TYPE_CHECKING:
    from pyadtpulse.site import ADTPulseSite

LOG = logging.getLogger(__name__)

RECOVERABLE_ERRORS = [429, 500, 502, 503, 504]


class PyADTPulse:
    """Base object for ADT Pulse service."""

    def __init__(
        self,
        username: str,
        password: str,
        fingerprint: str,
        service_host: str = DEFAULT_API_HOST,
        user_agent=ADT_DEFAULT_HTTP_HEADERS["User-Agent"],
        websession: Optional[ClientSession] = None,
        do_login: bool = True,
        poll_interval: float = ADT_DEFAULT_POLL_INTERVAL,
    ):
        """Create a PyADTPulse object.

        Args:
            username (str): Username.
            password (str): Password.
            fingerprint (str): 2FA fingerprint.
            service_host (str, optional): host prefix to use
                         i.e. https://portal.adtpulse.com or 
                              https://portal-ca.adtpulse.com
            user_agent (str, optional): User Agent.
                         Defaults to ADT_DEFAULT_HTTP_HEADERS["User-Agent"].
            websession (ClientSession, optional): an initialized
                        aiohttp.ClientSession to use, defaults to None
            do_login (bool, optional): login synchronously when creating object
                            Should be set to False for asynchronous usage
                            and async_login() should be called instead
                            Setting websession will override this
                            and not login
                        Defaults to True
            poll_interval (float, optional): number of seconds between update checks
        """
        self._session = websession
        if self._session is not None:
            self._session.headers.update(ADT_DEFAULT_HTTP_HEADERS)
        self._init_login_info(username, password, fingerprint)
        self._user_agent = user_agent
        self._api_version: str = ADT_DEFAULT_VERSION

        self._sync_task: Optional[asyncio.Task] = None
        self._timeout_task: Optional[asyncio.Task] = None
        # FIXME use thread event/condition, regular condition?
        # defer initialization to make sure we have an event loop
        self._authenticated: Optional[asyncio.locks.Event] = None
        self._updates_exist: Optional[asyncio.locks.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session_thread: Optional[Thread] = None
        self._attribute_lock = RLock()
        self._last_timeout_reset = time.time()
        self._sync_timestamp = 0.0
        # fixme circular import, should be an ADTPulseSite
        if TYPE_CHECKING:
            self._sites: List[ADTPulseSite]
        else:
            self._sites: List[Any] = []

        self._api_host = service_host
        self._poll_interval = poll_interval

        # authenticate the user
        if do_login and self._session is None:
            self.login()

    def _init_login_info(self, username: str, password: str, fingerprint: str) -> None:
        if username is None or username == "":
            raise ValueError("Username is madatory")
        pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
        if not re.match(pattern, username):
            raise ValueError("Username must be an email address")
        self._username = username
        if password is None or password == "":
            raise ValueError("Password is mandatory")
        self._password = password
        if fingerprint is None or fingerprint == "":
            raise ValueError("Fingerprint is required")
        self._fingerprint = fingerprint

    def __repr__(self) -> str:
        """Object representation."""
        return "<{}: {}>".format(self.__class__.__name__, self._username)

    # ADTPulse API endpoint is configurable (besides default US ADT Pulse endpoint) to
    # support testing as well as alternative ADT Pulse endpoints such as
    # portal-ca.adtpulse.com
    def set_service_host(self, host: str) -> None:
        """Override the Pulse host (i.e. to use portal-ca.adpulse.com).

        Args:
            host (str): name of Pulse endpoint host
        """
        if self.is_threaded:
            self._attribute_lock.acquire()
        self._api_host = f"https://{host}"
        if self._session is not None:
            self._session.headers.update({"Host": host})
            self._session.headers.update(ADT_DEFAULT_HTTP_HEADERS)
        if self.is_threaded:
            self._attribute_lock.release()

    def make_url(self, uri: str) -> str:
        """Create a URL to service host from a URI.

        Args:
            uri (str): the URI to convert

        Returns:
            str: the converted string
        """
        if self.is_threaded:
            with self._attribute_lock:
                return f"{self._api_host}{API_PREFIX}{self.version}{uri}"
        return f"{self._api_host}{API_PREFIX}{self.version}{uri}"

    @property
    def poll_interval(self) -> float:
        """Get polling interval.

        Returns:
            float: interval in seconds to poll for updates
        """
        if self.is_threaded:
            with self._attribute_lock:
                return self._poll_interval
        return self._poll_interval

    @poll_interval.setter
    def poll_interval(self, interval: float) -> None:
        """Set polling interval.

        Args:
            interval (float): interval in seconds to poll for updates
        """
        if self.is_threaded:
            self._attribute_lock.acquire()
            self._poll_interval = interval
            self._attribute_lock.release()
        else:
            self._poll_interval = interval

    @property
    def username(self) -> str:
        """Get username.

        Returns:
            str: the username
        """
        if self.is_threaded:
            with self._attribute_lock:
                return self._username
        return self._username

    @property
    def version(self) -> str:
        """Get the ADT Pulse site version.

        Returns:
            str: a string containing the version
        """
        if self.is_threaded:
            with self._attribute_lock:
                return self._api_version
        return self._api_version

    async def _async_fetch_version(self) -> None:
        result = None
        if self._session:
            try:
                async with self._session.get(self._api_host) as response:
                    result = await response.text()
                    response.raise_for_status()
            except (ClientResponseError, ClientConnectionError):
                LOG.warning(
                    "Error occurred during API version fetch, defaulting to"
                    f"{ADT_DEFAULT_VERSION}"
                )
                self._api_version = ADT_DEFAULT_VERSION
                return
        if result is None:
            LOG.warning(
                "Error occurred during API version fetch, defaulting to"
                f"{ADT_DEFAULT_VERSION}"
            )
            self._api_version = ADT_DEFAULT_VERSION
            return

        m = re.search("/myhome/(.+)/[a-z]*/", result)
        if m is not None:
            self._api_version = m.group(1)
            LOG.debug(
                "Discovered ADT Pulse version"
                f" {self._api_version} at {self._api_host}"
            )
            return

        self._api_version = ADT_DEFAULT_VERSION
        LOG.warning(
            "Couldn't auto-detect ADT Pulse version, "
            f"defaulting to {self._api_version}"
        )

    async def _update_sites(self, soup: BeautifulSoup) -> None:
        if len(self._sites) == 0:
            await self._initialize_sites(soup)
        else:
            # FIXME: this will have to be fixed once multiple ADT sites
            # are supported, since the summary_html only represents the
            # alarm status of the current site!!
            if len(self._sites) > 1:
                LOG.error(
                    (
                        "pyadtpulse DOES NOT support an ADT account ",
                        "with multiple sites yet!!!",
                    )
                )

        for site in self._sites:
            site._update_alarm_from_soup(soup)
            site._update_zone_from_soup(soup)

    async def _initialize_sites(self, soup: BeautifulSoup) -> None:
        # typically, ADT Pulse accounts have only a single site (premise/location)
        singlePremise = soup.find("span", {"id": "p_singlePremise"})
        if singlePremise:
            site_name = singlePremise.text

            # FIXME: this code works, but it doesn't pass the linter
            signout_link = str(
                soup.find("a", {"class": "p_signoutlink"}).get("href")  # type: ignore
            )
            if signout_link:
                m = re.search("networkid=(.+)&", signout_link)
                if m and m.group(1) and m.group(1):
                    from pyadtpulse.site import ADTPulseSite

                    site_id = m.group(1)
                    LOG.debug(f"Discovered site id {site_id}: {site_name}")
                    # FIXME ADTPulseSite circular reference
                    new_site = ADTPulseSite(self, site_id, site_name)
                    # fetch zones first, so that we can have the status
                    # updated with _update_alarm_status
                    await new_site._fetch_zones(None)
                    new_site._update_alarm_from_soup(soup)
                    new_site._update_zone_from_soup(soup)
                    self._sites.append(new_site)
                    return
            else:
                LOG.warning(
                    f"Couldn't find site id for '{site_name}' in '{signout_link}'"
                )
        else:
            LOG.error(("ADT Pulse accounts with MULTIPLE sites not supported!!!"))

    # ...and current network id from:
    # <a id="p_signout1" class="p_signoutlink"
    # href="/myhome/16.0.0-131/access/signout.jsp?networkid=150616za043597&partner=adt"
    # onclick="return flagSignOutInProcess();">
    #
    # ... or perhaps better, just extract all from /system/settings.jsp

    def _close_response(self, response: Optional[ClientResponse]) -> None:
        if response is not None and not response.closed:
            response.close()

    async def _keepalive_task(self) -> None:
        LOG.debug("creating Pulse keepalive task")
        response = None
        if self._authenticated is None:
            raise RuntimeError(
                "Keepalive task is runnng without an authenticated event"
            )
        while self._authenticated.is_set():
            try:
                await asyncio.sleep(ADT_TIMEOUT_INTERVAL)
                LOG.debug("Resetting timeout")
                response = await self._async_query(ADT_TIMEOUT_URI, "POST")
                if handle_response(
                    response, logging.INFO, "Failed resetting ADT Pulse cloud timeout"
                ):
                    self._close_response(response)
                    continue
                self._close_response(response)
            except asyncio.CancelledError:
                LOG.debug("ADT Pulse timeout task cancelled")
                self._close_response(response)
                return

    def _pulse_session_thread(self) -> None:
        LOG.debug("creating Pulse background thread")
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._sync_loop())
        self._loop.close()
        self._loop = None
        self._session_thread = None

    async def _sync_loop(self) -> None:
        await self.async_login()
        if self._sync_task is not None and self._timeout_task is not None:
            await asyncio.wait((self._sync_task, self._timeout_task))

    def login(self) -> None:
        """Login to ADT Pulse and generate access token."""
        self._attribute_lock.acquire()
        if self._session_thread is None:
            self._session_thread = Thread(
                target=self._pulse_session_thread,
                name="PyADTPulse Session",
                daemon=True,
            )
            self._attribute_lock.release()
            self._session_thread.run()
        else:
            assert self._loop is not None
            coro = self.async_login()
            asyncio.run_coroutine_threadsafe(coro, self._loop)
            self._attribute_lock.release()

    @property
    def attribute_lock(self) -> RLock:
        """Get attribute lock for PyADTPulse object.

        Returns:
            RLock: thread Rlock
        """
        return self._attribute_lock

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """Get event loop.

        Returns:
            Optional[asyncio.AbstractEventLoop]: the event loop object or
                                                 None if no thread is running
        """
        if self.is_threaded:
            with self._attribute_lock:
                return self._loop
        return self._loop

    @property
    def is_threaded(self) -> bool:
        """Query if ADT Pulse Session is threaded/sync.

        Returns:
            bool: True if the session is threaded/sync
        """
        with self._attribute_lock:
            return self._loop is not None

    async def async_login(self) -> None:
        """Login asynchronously to ADT."""
        if self._session is None:
            self._session = ClientSession()
            self._session.headers.update(ADT_DEFAULT_HTTP_HEADERS)
        self._authenticated = asyncio.locks.Event()
        LOG.debug(f"Authenticating to ADT Pulse cloud service as {self._username}")
        await self._async_fetch_version()

        response = await self._async_query(
            ADT_LOGIN_URI,
            method="POST",
            extra_params={
                "partner": "adt",
                "usernameForm": self._username,
                "passwordForm": self._password,
                "fingerprint": self._fingerprint,
                "sun": "yes",
            },
            force_login=False,
            timeout=10,
        )

        soup = await make_soup(
            response, logging.ERROR, "Could not log into ADT Pulse site"
        )
        if soup is None:
            self._authenticated.clear()
            return

        error = soup.find("div", {"id": "warnMsgContents"})
        if error:
            LOG.error(f"Invalid ADT Pulse response: ): {error}")
            self._authenticated.clear()
            return

        self._authenticated.set()
        self._last_timeout_reset = time.time()

        # since we received fresh data on the status of the alarm, go ahead
        # and update the sites with the alarm status.

        await self._update_sites(soup)
        self._sync_timestamp = time.time()
        if self._sync_task is None:
            self._sync_task = asyncio.create_task(
                self._sync_check_task(), name="PyADTPulse sync check"
            )
        if self._timeout_task is None:
            self._timeout_task = asyncio.create_task(
                self._keepalive_task(), name="PyADTPulse timeout"
            )
        if self._updates_exist is None:
            self._updates_exist = asyncio.locks.Event()
        await asyncio.sleep(0)

    async def async_logout(self) -> None:
        """Logout of ADT Pulse async."""
        LOG.info(f"Logging {self._username} out of ADT Pulse")
        if self._timeout_task is not None:
            try:
                self._timeout_task.cancel()
            except asyncio.CancelledError:
                LOG.debug("Pulse timeout task successfully cancelled")
                await self._timeout_task
        if self._sync_task is not None:
            try:
                self._sync_task.cancel()
            except asyncio.CancelledError:
                LOG.debug("Pulse sync check task successfully cancelled")
                await self._sync_task
        self._timeout_task = self._sync_task = None
        await self._async_query(ADT_LOGOUT_URI, timeout=10)
        if self._session is not None:
            if not self._session.closed:
                await self._session.close()
        self._last_timeout_reset = time.time()
        if self._authenticated is not None:
            self._authenticated.clear()

    def logout(self) -> None:
        """Log out of ADT Pulse."""
        if self._loop is None:
            raise RuntimeError("Attempting to call sync logout without sync login")
        coro = self.async_logout()
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _sync_check_task(self) -> None:
        LOG.debug("creating Pulse sync check task")
        response = None
        if self._updates_exist is None:
            raise RuntimeError(
                "Sync check task started without update event initialized"
            )
        while True:
            try:
                # call property to lock value if necessary
                pi = self.poll_interval
                await asyncio.sleep(pi)
                response = await self._async_query(
                    ADT_SYNC_CHECK_URI,
                    extra_params={"ts": int(self._sync_timestamp * 1000)},
                )
                if response is None:
                    continue
                text = await response.text()
                if not handle_response(
                    response, logging.ERROR, "Error querying ADT sync"
                ):
                    self._close_response(response)
                    continue

                pattern = r"\d+[-]\d+[-]\d+"
                if not re.match(pattern, text):
                    LOG.warn(
                        f"Unexpected sync check format ({pattern}), forcing re-auth"
                    )
                    LOG.debug(f"Received {text} from ADT Pulse site")
                    self._close_response(response)
                    await self.async_login()
                    continue

                # we can have 0-0-0 followed by 1-0-0 followed by 2-0-0, etc
                # wait until these settle
                if text.endswith("-0-0"):
                    LOG.debug(
                        f"Sync token {text} indicates updates may exist, requerying"
                    )
                    self._close_response(response)
                    self._sync_timestamp = time.time()
                    self._updates_exist.set()
                    if await self.async_update() is False:
                        LOG.debug("Pulse data update from sync task failed")
                    continue
                LOG.debug(f"Sync token {text} indicates no remote updates to process")
                self._close_response(response)
                self._sync_timestamp = time.time()
            except asyncio.CancelledError:
                LOG.debug("ADT Pulse sync check task cancelled")
                self._close_response(response)
                return

    @property
    def updates_exist(self) -> bool:
        """Check if updated data exists.

        Returns:
            bool: True if updated data exists
        """
        if self.is_threaded:
            self._attribute_lock.acquire()
        if self._updates_exist is None:
            if self.is_threaded:
                self._attribute_lock.release()
            return False
        if self._updates_exist.is_set():
            self._updates_exist.clear()
            if self.is_threaded:
                self._attribute_lock.release()
            return True
        if self.is_threaded:
            self._attribute_lock.release()
        return False

    async def wait_for_update(self) -> None:
        """Wait for update.

        Blocks current async task until Pulse system
        signals an update
        """
        if self._updates_exist is None:
            raise RuntimeError("Update event does not exist")
        await self._updates_exist.wait()
        self._updates_exist.clear()

    @property
    def is_connected(self) -> bool:
        """Check if connected to ADT Pulse.

        Returns:
            bool: True if connected
        """
        # FIXME: timeout automatically based on ADT default expiry?
        # self._authenticated_timestamp
        if self.is_threaded:
            self._attribute_lock.acquire()
        if self._authenticated is None:
            if self.is_threaded:
                self._attribute_lock.release()
            return False
        if self.is_threaded:
            self._attribute_lock.release()
        return self._authenticated.is_set()

    async def _async_query(
        self,
        uri: str,
        method: str = "GET",
        extra_params: Optional[Dict] = None,
        extra_headers: Optional[Dict] = None,
        force_login: Optional[bool] = True,
        timeout=1,
    ) -> Optional[ClientResponse]:
        """Query ADT Pulse async.

        Args:
            uri (str): URI to query
            method (str, optional): method to use. Defaults to "GET".
            extra_params (Optional[Dict], optional): query parameters. Defaults to None.
            extra_headers (Optional[Dict], optional): extra HTTP headers.
                        Defaults to None.
            force_login (Optional[bool], optional): login if not connected.
                        Defaults to True.
            timeout (int, optional): timeout in seconds. Defaults to 1.

        Returns:
            Optional[ClientResponse]: aiohttp.ClientResponse object
                                      None on failure
                                      ClientResponse will already be closed.
        """
        response = None

        # automatically attempt to login, if not connected
        if force_login and not self.is_connected:
            await self.async_login()

        if self._session is None:
            raise RuntimeError("ClientSession not initialized")
        url = self.make_url(uri)
        if uri in ADT_HTTP_REFERER_URIS:
            new_headers = {"Accept": ADT_DEFAULT_HTTP_HEADERS["Accept"]}
        else:
            new_headers = {"Accept": "*/*"}

        LOG.debug(f"Updating HTTP headers: {new_headers}")
        self._session.headers.update(new_headers)

        LOG.debug(f"Attempting {method} {url}")

        # FIXME: reauthenticate if received:
        # "You have not yet signed in or you
        #  have been signed out due to inactivity."

        # define connection method
        try:
            if method == "GET":
                async with self._session.get(
                    url, headers=extra_headers, params=extra_params, timeout=timeout
                ) as response:
                    await response.text()
            elif method == "POST":
                async with self._session.post(
                    url, headers=extra_headers, data=extra_params, timeout=timeout
                ) as response:
                    await response.text()
            else:
                LOG.error(f"Invalid request method {method}")
                return None
            response.raise_for_status()

        except ClientResponseError as err:
            code = err.code
            LOG.exception(f"Received HTTP error code {code} in request to ADT Pulse")
            return None
        except ClientConnectionError:
            LOG.exception("An exception occurred in request to ADT Pulse")
            return None

        # success!
        # FIXME? login uses redirects so final url is wrong
        if uri in ADT_HTTP_REFERER_URIS:
            if uri == ADT_DEVICE_URI:
                referer = self.make_url(ADT_SYSTEM_URI)
            else:
                referer = str(response.url)
                LOG.debug(f"Setting Referer to: {referer}")
            self._session.headers.update({"Referer": referer})

        return response

    def query(
        self,
        uri: str,
        method: str = "GET",
        extra_params: Optional[Dict] = None,
        extra_headers: Optional[Dict] = None,
        force_login: Optional[bool] = True,
        timeout=1,
    ) -> Optional[ClientResponse]:
        """Query ADT Pulse async.

        Args:
            uri (str): URI to query
            method (str, optional): method to use. Defaults to "GET".
            extra_params (Optional[Dict], optional): query parameters. Defaults to None.
            extra_headers (Optional[Dict], optional): extra HTTP headers.
                                                    Defaults to None.
            force_login (Optional[bool], optional): login if not connected.
                                                    Defaults to True.
            timeout (int, optional): timeout in seconds. Defaults to 1.
        Returns:
            Optional[ClientResponse]: aiohttp.ClientResponse object
                                      None on failure
                                      ClientResponse will already be closed.
        """
        if self._loop is None:
            raise RuntimeError("Attempting to run sync query from async login")
        coro = self._async_query(
            uri, method, extra_params, extra_headers, force_login, timeout
        )
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    # FIXME? might have to move this to site for multiple sites
    async def _query_orb(
        self, level: int, error_message: str
    ) -> Optional[BeautifulSoup]:
        response = await self._async_query(ADT_ORB_URI)

        return await make_soup(response, level, error_message)

    async def async_update(self) -> bool:
        """Update ADT Pulse data.

        Returns:
            bool: True if update succeeded.
        """
        LOG.debug("Checking ADT Pulse cloud service for updates")

        # FIXME will have to query other URIs for camera/zwave/etc
        soup = await self._query_orb(
            logging.INFO, "Error returned from ADT Pulse service check"
        )
        if soup is not None:
            await self._update_sites(soup)
            return True

        return False

    def update(self) -> bool:
        """Update ADT Pulse data.

        Returns:
            bool: True on success
        """
        if self._loop is None:
            raise RuntimeError("Attempting to run sync update from async login")
        coro = self.async_update()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    # FIXME circular reference, should be ADTPulseSite

    @property
    def sites(self) -> List[Any]:
        """Return all sites for this ADT Pulse account."""
        if self.is_threaded:
            with self._attribute_lock:
                return self._sites
        return self._sites
