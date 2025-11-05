from configparser import ConfigParser
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import pytest
import re
import requests
from requests.exceptions import HTTPError, ReadTimeout
import semver
import shutil
import subprocess
import tempfile
import threading
import time
from urllib3.exceptions import ProtocolError

from kbase import sdk_baseclient


_VERSION = "0.1.0"
# should be fine, find an empty ports otherwise
_MOCKSERVER_PORT = 31590
_CALLBACK_SERVER_PORT = 31591
_CALLBACK_SERVER_IMAGE = "ghcr.io/kbase/jobrunner:pr-116"


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


def _wait_for_callback(callback_url: str):
    interval = 1
    limit = 120
    start = time.monotonic()
    err = None
    print("waiting for callback server to start")
    while time.monotonic() - start < limit:
        try:
            res = requests.get(callback_url)
            restext = res.text
            if res.status_code == 200 and res.text == "[{}]":
                print(f"Callback server is up at {callback_url}")
                return
        except Exception as e:
            err = e
        print("waiting for CBS")
        time.sleep(interval)
    raise IOError(f"Callback server did not start, last response: {restext}") from err


@pytest.fixture(scope="module")
def callback(url_and_token):
    # Tried using the temp path pytest fixture but kept getting lots of warnings
    tmpdir = tempfile.mkdtemp(prefix="callback_server_data_")
    container_name = f"sdk_baseclient_test_{str(time.time()).replace('.', '_')}"
    dockercmd = [
        "docker", "run",
        "--platform=linux/amd64",  # until we have multiarch images
        "--name", container_name,
        "--rm",
        # TODO SECURITY when CBS allows, use a file instead
        #               https://github.com/kbase/JobRunner/issues/90
        "-e", f"KB_AUTH_TOKEN={url_and_token[1]}",
        "-e", f"KB_BASE_URL={url_and_token[0]}/services/",
        "-e", f"JOB_DIR={tmpdir}",
        "-e", "CALLBACK_IP=localhost",
        "-e", f"CALLBACK_PORT={_CALLBACK_SERVER_PORT}",
        "-e", "DEBUG_RUNNER=true", # prints logs from containers
        "-v", "/var/run/docker.sock:/run/docker.sock",
        "-v", f"{tmpdir}:{tmpdir}",
        "-p", f"{_CALLBACK_SERVER_PORT}:{_CALLBACK_SERVER_PORT}",
        _CALLBACK_SERVER_IMAGE
    ]
    proc = subprocess.Popen(dockercmd)
    callback_url = f"http://localhost:{_CALLBACK_SERVER_PORT}"
    _wait_for_callback(callback_url)

    try:
        yield callback_url
    finally:
        subprocess.check_call(["docker", "stop", container_name])
        proc.wait(timeout=10)
        dockercmd = [
            "docker", "run",
            "--platform=linux/amd64",  # until we have multiarch images
            "--name", container_name,
            "--rm",
            "-v", f"{tmpdir}:{tmpdir}",
            "--entrypoint", "bash",
            _CALLBACK_SERVER_IMAGE,
            "-c", f"rm -rf {tmpdir}/*",  # need to use bash for the globbing
        ]
        subprocess.check_call(dockercmd)
        shutil.rmtree(tmpdir)


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
    # Tests returning a single value
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


# TODO add test for service that returns > 1 value. Not sure if any services do this


def test_serialize_sets(url_and_token):
    # Tests serializing set and frozenset
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


###
# Dynamic service tests
# 
# All of the 3 ways of calling services use the same underlying _call method, so we don't
# reiterate those tests every time.
###

def test_dynamic_service(url_and_token):
    bc = sdk_baseclient.SDKBaseClient(
        url_and_token[0] + "/services/service_wizard", lookup_url=True
    )
    res = bc.call_method("HTMLFileSetServ.status", [])
    del res["git_commit_hash"]
    ver = res["version"]
    del res["version"]
    assert res == {
        "git_url": "https://github.com/kbaseapps/HTMLFileSetServ",
        "message": "",
        "state": "OK",
    }
    assert semver.Version.parse(ver) > semver.Version.parse("0.0.8")


def test_dynamic_service_with_service_version(url_and_token):
    # Current version of HFS is 0.0.9 everywhere
    bc = sdk_baseclient.SDKBaseClient(
        url_and_token[0] + "/services/service_wizard", lookup_url=True
    )
    res = bc.call_method("HTMLFileSetServ.status", [], service_ver="0.0.8")
    del res["git_commit_hash"]
    assert res == {
        "git_url": "https://github.com/kbaseapps/HTMLFileSetServ",
        "message": "",
        "state": "OK",
        "version": "0.0.8"
    }


###
# Async job tests
# 
# All of the 3 ways of calling services use the same underlying _call method, so we don't
# reiterate those tests every time.
###


def test_run_job_with_service_ver(url_and_token, callback):
    bc = sdk_baseclient.SDKBaseClient(callback, token=url_and_token[1], timeout=10)
    res = bc.run_job(
        "njs_sdk_test_2.run",
        # force backoff with a wait
        [{"id": "simplejob2", "wait": 1}],
        # it seems semvers don't work for unreleased modules
        service_ver="9d6b868bc0bfdb61c79cf2569ff7b9abffd4c67f"
    )
    assert res == {
        "id": "simplejob2",
        "name": "njs_sdk_test_2",
        "hash": "9d6b868bc0bfdb61c79cf2569ff7b9abffd4c67f",
        "wait": 1,
    }


def test_run_job_no_return(url_and_token, callback):
    bc = sdk_baseclient.SDKBaseClient(callback, token=url_and_token[1], timeout=10)
    res = bc.run_job("HelloServiceDeluxe.how_rude", ["Georgette"])
    assert res is None


def test_run_job_list_return(url_and_token, callback):
    bc = sdk_baseclient.SDKBaseClient(callback, token=url_and_token[1], timeout=10)
    res = bc.run_job("HelloServiceDeluxe.say_hellos", ["JimBob", "Gengulphus"])
    assert res == [
        'Hi JimBob, you santimonious lickspittle', # the dork that wrote this module can't spell
        'Hi Gengulphus, what a lovely and scintillating person you are',
    ]


def test_run_job_failure(url_and_token, callback, requests_mock):
    requests_mock.post(callback, [
        {"json": {"result": ["job_id"]}},
        {"exc": ConnectionError("oopsie")},
        {"exc": ProtocolError("oh dang")},
        {"exc": ConnectionError("so unreliable omg")},
    ])
    bc = sdk_baseclient.SDKBaseClient(callback, token=url_and_token[1], timeout=10)
    with pytest.raises(RuntimeError, match="_check_job failed 3 times and exceeded limit"):
        bc.run_job("HelloServiceDeluxe.say_hellos", ["JimBob"])


def test_run_job_failure_recovery(url_and_token, callback, requests_mock):
    requests_mock.post(callback, [
        {"json": {"result": ["job_id"]}},
        {"exc": ConnectionError("oopsie")},
        {"exc": ProtocolError("oh dang")},
        {"json": {"result": [{"finished": 1, "result": ["meh"]}]}},
    ])
    bc = sdk_baseclient.SDKBaseClient(callback, token=url_and_token[1], timeout=10)
    res = bc.run_job("HelloServiceDeluxe.say_hellos", ["JimBob"])
    assert res == "meh"
