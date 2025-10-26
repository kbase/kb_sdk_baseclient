from kbase import sdk_baseclient


_VERSION = "0.1.0"


def test_version():
    assert sdk_baseclient.__version__ == _VERSION
