import urllib.error
import urllib.request

import pytest

from tests.fixtures.web import FixtureSite


def test_serves_routes_on_loopback(fixture_site: FixtureSite) -> None:
    assert fixture_site.base_url.startswith("http://127.0.0.1:")
    with urllib.request.urlopen(fixture_site.url("/page"), timeout=5) as resp:  # noqa: S310
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        assert resp.read() == b"<html><body>page</body></html>"


def test_unknown_path_is_404(fixture_site: FixtureSite) -> None:
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(fixture_site.url("/missing"), timeout=5)  # noqa: S310
    assert err.value.code == 404
