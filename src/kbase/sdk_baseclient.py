"""
The base client for all SDK clients.
"""

import json as _json
import random as _random
import requests as _requests
import os as _os
from urllib.parse import urlparse as _urlparse
from typing import Any


# The first version is a pretty basic port from the old baseclient, removing some no longer
# relevant cruft.


__version__ = "0.1.0"


_CT = "content-type"
_AJ = "application/json"
_URL_SCHEME = frozenset(["http", "https"])
_CHECK_JOB_RETRIES = 3


class ServerError(Exception):

    def __init__(self, name, code, message, data=None, error=None):
        super(Exception, self).__init__(message)
        self.name = name
        self.code = code
        # Ew. Leave it for backwards compatibility
        self.message = "" if message is None else message
        # Not really worth setting up a mock for the error case
        # data = JSON RPC 2.0, error = 1.1
        self.data = data or error or ""

    def __str__(self):
        return self.name + ": " + str(self.code) + ". " + self.message + \
            "\n" + self.data


class _JSONObjectEncoder(_json.JSONEncoder):

    def default(self, obj):
        if isinstance(obj, set):
            return list(obj)
        if isinstance(obj, frozenset):
            return list(obj)
        return _json.JSONEncoder.default(self, obj)


class SDKBaseClient:
    """
    The KBase base client.

    url - the url of the the service to contact:
        For SDK methods: the url of the callback service.
        For SDK dynamic services: the url of the Service Wizard.
        For other services: the url of the service.
    timeout - methods will fail if they take longer than this value in seconds.
        Default 1800.
    token - a KBase authentication token.
    trust_all_ssl_certificates - set to True to trust self-signed certificates.
        If you don't understand the implications, leave as the default, False.
    lookup_url - set to true when contacting KBase dynamic services.
    async_job_check_time_ms - the wait time between checking job state for
        asynchronous jobs run with the run_job method.
    async_job_check_time_scale_percent - the percentage increase in wait time between async job
        check attempts.
    async_job_check_max_time_ms - the maximum time to wait for a job check attempt before
        failing.
    """
    def __init__(
            self,
            url: str,
            *,
            timeout: int = 30 * 60,
            token: str = None,
            trust_all_ssl_certificates: bool = False,  # Too much of a pain to test
            lookup_url: bool = False,
            async_job_check_time_ms: int = 100,
            async_job_check_time_scale_percent: int = 150,
            async_job_check_max_time_ms: int = 300000
        ):
        if url is None:
            raise ValueError("A url is required")
        scheme, _, _, _, _, _ = _urlparse(url)
        if scheme not in _URL_SCHEME:
            raise ValueError(url + " isn't a valid http url")
        self.url = url
        self.timeout = int(timeout)
        self._headers = {}
        self.trust_all_ssl_certificates = trust_all_ssl_certificates
        self.lookup_url = lookup_url
        self.async_job_check_time = async_job_check_time_ms / 1000.0
        self.async_job_check_time_scale_percent = async_job_check_time_scale_percent
        self.async_job_check_max_time = async_job_check_max_time_ms / 1000.0
        self.token = None
        if token is not None:
            self.token = token
        # Not a fan of magic env vars but this is too baked in to remove
        elif "KB_AUTH_TOKEN" in _os.environ:
            self.token = _os.environ.get("KB_AUTH_TOKEN")
        if self.token:
            self._headers["AUTHORIZATION"] = self.token
        if self.timeout < 1:
            raise ValueError("Timeout value must be at least 1 second")

    def _call(self, url: str, method: str, params: list[Any], context: dict[str, Any] | None):
        arg_hash = {"method": method,
                    "params": params,
                    "version": "1.1",
                    "id": str(_random.random())[2:],
                    }
        if context:
            arg_hash["context"] = context

        body = _json.dumps(arg_hash, cls=_JSONObjectEncoder)
        ret = _requests.post(
            url,
            data=body,
            headers=self._headers,
            timeout=self.timeout,
            verify=not self.trust_all_ssl_certificates
        )
        ret.encoding = "utf-8"
        if ret.status_code == 500:
            if ret.headers.get(_CT) == _AJ:
                err = ret.json()
                if "error" in err:
                    raise ServerError(**err["error"])
                else:
                    raise ServerError(
                        "Unknown", 0, f"The server returned unexpected error JSON: {ret.text}"
                    )
            else:
                raise ServerError(
                    "Unknown", 0, f"The server returned a non-JSON response: {ret.text}"
                )
        if not ret.ok:
            ret.raise_for_status()
        resp = ret.json()
        if "result" not in resp:
            raise ServerError("Unknown", 0, "An unknown server error occurred")
        if not resp["result"]:
            return None
        if len(resp["result"]) == 1:
            return resp["result"][0]
        return resp["result"]

    def call_method(self, service_method: str, args: list[Any], *, service_ver: str | None = None):
        """
        Call a standard or dynamic service synchronously.
        Required arguments:
        service_method - the service and method to run, e.g. myserv.mymeth.
        args - a list of arguments to the method.
        Optional arguments:
        service_ver - the version of the service to run, e.g. a git hash
            or dev/beta/release.
        """
        # TDOO NEXT implement dynamic methods
        #url = self._get_service_url(service_method, service_ver)
        #context = self._set_up_context(service_ver)
        url = self.url
        return self._call(url, service_method, args, None)
