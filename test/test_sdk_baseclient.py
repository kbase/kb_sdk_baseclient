from configparser import ConfigParser
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import pytest
import re
from requests.exceptions import HTTPError, ReadTimeout
import semver
import threading
import time

from kbase import sdk_baseclient


_VERSION = "0.1.0"
_MOCKSERVER_PORT = 31590  # should be fine, find an empty port otherwise


@pytest.fixture(scope="module")
def url_and_token():
    config = ConfigParser()
    config.read("test.cfg")
    sec = config["kbase_sdk_baseclient_tests"]
    return sec["test_url"], sec["test_token"]


class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/not-json":
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Wrong server pal")
        elif self.path == "/missing-error":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"oops": "no error key"}).encode("utf-8"))
        else:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"Don't call this endpoint chum")


@pytest.fixture(scope="module")
def mockserver():
    server = HTTPServer(("localhost", _MOCKSERVER_PORT), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    
    yield f"http://localhost:{_MOCKSERVER_PORT}"
    
    server.shutdown()


def test_version():
    assert sdk_baseclient.__version__ == _VERSION


def test_construct_fail():
    _test_construct_fail(None, 1, "A url is required")
    _test_construct_fail("ftp://foo.com/bar", 1, "ftp://foo.com/bar isn't a valid http url")
    for t in [.999999, 0, -1, -1000]:
        _test_construct_fail("http://example.com", t, "Timeout value must be at least 1 second")


def _test_construct_fail(url: str, timeout: int, expected: str):
    with pytest.raises(ValueError, match=expected):
        sdk_baseclient.SDKBaseClient(url, timeout=timeout)


def test_tokenless(url_and_token):
    bc = sdk_baseclient.SDKBaseClient(url_and_token[0] + "/services/ws")
    res = bc.call_method("Workspace.ver", [])
    semver.Version.parse(res)


def test_call_method_basic_passed_token(url_and_token):
    # Tests returning a single value
    _test_call_method_basic(url_and_token[0] + "/services/ws", url_and_token[1])


def test_call_method_basic_env_token(url_and_token):
    os.environ["KB_AUTH_TOKEN"] = url_and_token[1]
    try:
        _test_call_method_basic(url_and_token[0] + "/services/ws", None)
    finally:
        del os.environ["KB_AUTH_TOKEN"]


def _test_call_method_basic(url: str, token: str |  None):
    # Also tests a null result with delete_workspace
    ws_name = f"sdk_baseclient_test_{time.time()}"
    bc = sdk_baseclient.SDKBaseClient(url, token=token)
    try:
        res = bc.call_method("Workspace.create_workspace", [{"workspace": ws_name}])
        assert len(res) == 9
        assert res[1] == ws_name
        assert res[4:] == [0, "a", "n", "unlocked", {}]
    finally:
        res = bc.call_method("Workspace.delete_workspace", [{"workspace": ws_name}])
        assert res is None


def test_serialize_sets_and_list_return(url_and_token):
    """
    Tests
    * Serializing set and frozenset
    * Methods that return a list vs. a single value (save_objects).
    """
    bc = sdk_baseclient.SDKBaseClient(url_and_token[0] + "/services/ws", token=url_and_token[1])
    ws_name = f"sdk_baseclient_test_{time.time()}"
    try:
        res = bc.call_method("Workspace.create_workspace", [{"workspace": ws_name}])
        wsid = res[0]
        res = bc.call_method("Workspace.save_objects", [{
            "id": wsid,
            "objects": [{
                "type": "Empty.AType",  # basically no restrictions
                "name": "foo",
                "data": {},
                "provenance": [{
                    "method_params": set(["a"]),
                    "intermediate_outgoing": frozenset(["b"])
                }]
            }]
        }])
        assert len(res) == 1
        res = res[0]
        assert res[0] == 1
        assert res[1] == "foo"
        assert res[2].startswith("Empty.AType")
        assert res[4] == 1
        assert res[7:] == [ws_name, "99914b932bd37a50b983c5e7c90ae93b", 2, {}]
        res = bc.call_method("Workspace.get_objects2", [{"objects": [{"ref": f"{wsid}/1/1"}]}])
        assert set(res.keys()) == {"data"}
        objs = res["data"]
        assert len(objs) == 1
        assert objs[0]["provenance"] == [{
            "method_params": ["a"],
            "input_ws_objects": [],
            "resolved_ws_objects": [],
            "intermediate_incoming": [],
            "intermediate_outgoing": ["b"],
            "external_data": [],
            "subactions": [],
            "custom": {}
        }]
    finally:
        res = bc.call_method("Workspace.delete_workspace", [{"workspace": ws_name}])


def test_call_method_error(url_and_token):
    bc = sdk_baseclient.SDKBaseClient(url_and_token[0] + "/services/ws", token=url_and_token[1])
    with pytest.raises(sdk_baseclient.ServerError) as got:
        bc.call_method("Workspace.get_workspace_info", [{"id": 100000000000000}])
    assert got.value.name == "JSONRPCError"
    assert got.value.message == "No workspace with id 100000000000000 exists"
    assert got.value.code == -32500
    assert got.value.data.startswith(
        "us.kbase.workspace.database.exceptions.NoSuchWorkspaceException: "
        + "No workspace with id 100000000000000 exists"
    )
    assert str(got.value).startswith(
        "JSONRPCError: -32500. No workspace with id 100000000000000 exists\n"
        + "us.kbase.workspace.database.exceptions.NoSuchWorkspaceException")


def test_error_non_500(url_and_token):
    bc = sdk_baseclient.SDKBaseClient(url_and_token[0] + "/services/wsfake")
    err = "404 Client Error: Not Found for url: https://ci.kbase.us//services/wsfake"
    with pytest.raises(HTTPError, match=err):
        bc.call_method("Workspace.ver", [])


def test_timeout():
    bc = sdk_baseclient.SDKBaseClient("https://httpbin.org/delay/10", timeout=1)
    err = re.escape(
        "HTTPSConnectionPool(host='httpbin.org', port=443): Read timed out. (read timeout=1)"
    )
    with pytest.raises(ReadTimeout, match=err):
        bc.call_method("Workspace.ver", [])


def test_missing_result_key():
    bc = sdk_baseclient.SDKBaseClient("https://httpbin.org/delay/0")
    with pytest.raises(sdk_baseclient.ServerError) as got:
        bc.call_method("Workspace.ver", [])
    assert got.value.name == "Unknown"
    assert got.value.message == "An unknown server error occurred"
    assert got.value.code == 0
    assert got.value.data == ""


def test_not_application_json(mockserver):
    bc = sdk_baseclient.SDKBaseClient(mockserver + "/not-json")
    with pytest.raises(sdk_baseclient.ServerError) as got:
        bc.call_method("Workspace.ver", [])
    assert got.value.name == "Unknown"
    assert got.value.message == "The server returned a non-JSON response: Wrong server pal"
    assert got.value.code == 0
    assert got.value.data == ""


def test_missing_error_key(mockserver):
    bc = sdk_baseclient.SDKBaseClient(mockserver + "/missing-error")
    with pytest.raises(sdk_baseclient.ServerError) as got:
        bc.call_method("Workspace.ver", [])
    assert got.value.name == "Unknown"
    assert got.value.message == (
        'The server returned unexpected error JSON: {"oops": "no error key"}'
    )
    assert got.value.code == 0
    assert got.value.data == ""
