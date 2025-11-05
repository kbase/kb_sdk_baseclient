"""
The base client for all SDK clients.
"""

import json as _json
import random as _random
import requests as _requests
import os as _os
from urllib.parse import urlparse as _urlparse
import time as _time
import traceback as _traceback
from typing import Any
from urllib3.exceptions import ProtocolError as _ProtocolError


# The first version is a pretty basic port from the old baseclient, removing some no longer
# relevant cruft.


__version__ = "0.1.0"


_EXP_BACKOFF_SEC = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 60, 120, 300]
_CT = "content-type"
_AJ = "application/json"
_URL_SCHEME = frozenset(["http", "https"])
_CHECK_JOB_RETRIES = 3


# tested this manually by shortening _EXP_BACKOFF_MS and adding printouts below
def _get_next_backoff(backoff_index: int = 1) -> tuple[int, float]:
    if backoff_index < len(_EXP_BACKOFF_SEC) - 1:
        backoff_index += 1
    return backoff_index, _EXP_BACKOFF_SEC[backoff_index]


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
    timeout - http requests will fail if they take longer than this value in seconds.
        Default 1800.
    token - a KBase authentication token.
    trust_all_ssl_certificates - set to True to trust self-signed certificates.
        If you don't understand the implications, leave as the default, False.
    lookup_url - set to true when contacting KBase dynamic services.
    """
    def __init__(
            self,
            url: str,
            *,
            timeout: int = 30 * 60,
            token: str = None,
            trust_all_ssl_certificates: bool = False,  # Too much of a pain to test
            lookup_url: bool = False,
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

    def _call(
        self, url: str, method: str, params: list[Any], context: dict[str, Any] | None = None
    ):
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

    def _get_service_url(self, service_method: str, service_version: str | None):
        if not self.lookup_url:
            return self.url
        service = service_method.split(".")[0]
        service_status_ret = self._call(
            self.url, "ServiceWizard.get_service_status",
            [{"module_name": service, "version": service_version}]
        )
        return service_status_ret["url"]

    def _set_up_context(self, service_ver: str = None):
        if service_ver:
            return {"service_ver": service_ver}
        return None

    def _check_job(self, service: str, job_id: str):
        return self._call(self.url, service + "._check_job", [job_id])

    def _submit_job(self, service_method: str, args: list[Any], service_ver: str = None):
        context = self._set_up_context(service_ver)
        mod, meth = service_method.split(".")
        return self._call(self.url, mod + "._" + meth + "_submit", args, context)

    def run_job(self, service_method: str, args: list[Any], service_ver: str = None):
        """
        Run a SDK method asynchronously.
        Required arguments:
        service_method - the service and method to run, e.g. myserv.mymeth.
        args - a list of arguments to the method.
        Optional arguments:
        service_ver - the version of the service to run, e.g. a git hash
            or dev/beta/release.
        """
        mod = service_method.split(".")[0]
        job_id = self._submit_job(service_method, args, service_ver)
        backoff_index = -1
        check_job_failures = 0
        while check_job_failures < _CHECK_JOB_RETRIES:
            backoff_index, backoff = _get_next_backoff(backoff_index)
            _time.sleep(backoff)
            try:
                job_state = self._check_job(mod, job_id)
            except (ConnectionError, _ProtocolError):
                _traceback.print_exc()
                check_job_failures += 1
            else:
                if job_state["finished"]:
                    if not job_state["result"]:
                        return None
                    if len(job_state["result"]) == 1:
                        return job_state["result"][0]
                    return job_state["result"]
        raise RuntimeError(f"_check_job failed {check_job_failures} times and exceeded limit")

    def call_method(
        self, service_method: str, args: list[Any], *, service_ver: str | None = None
    ):
        """
        Call a standard or dynamic service synchronously.
        Required arguments:
        service_method - the service and method to run, e.g. myserv.mymeth.
        args - a list of arguments to the method.
        Optional arguments:
        service_ver - the version of the service to run, e.g. a git hash
            or dev/beta/release.
        """
        url = self._get_service_url(service_method, service_ver)
        context = self._set_up_context(service_ver)
        return self._call(url, service_method, args, context)
