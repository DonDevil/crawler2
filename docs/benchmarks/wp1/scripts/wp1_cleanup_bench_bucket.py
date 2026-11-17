"""Delete the P2 latency benchmark's own bucket (hard-coded `crawler2-bench`) via S3 API.
Run inside an app container: docker exec -i -e PYTHONPATH=/app <ctr> python - < this_file
The crawler2 object store has no delete API, so this signs raw requests with the project's sign_v4."""

# ruff: noqa: E501, S101
# (measurement tooling: broad excepts keep sampling alive when a sensor is missing; fixed argv lists only;
#  asserts guard the bench directory/bucket names; PRNG is for I/O offsets, not security.)
import http.client
import os
import re
import urllib.parse
from datetime import UTC, datetime

from crawler2.storage.objectstore.s3 import Credentials, sign_v4

BUCKET = "crawler2-bench"
assert BUCKET == "crawler2-bench"
EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
host = os.environ["CRAWLER2_MINIO__ENDPOINT"]
cred = Credentials(
    os.environ["CRAWLER2_MINIO__ACCESS_KEY"],
    os.environ["CRAWLER2_MINIO__SECRET_KEY"],
    os.environ.get("CRAWLER2_MINIO__REGION", "us-east-1"),
)


def call(method, path, query=None):
    q = query or {}
    h = sign_v4(
        method=method,
        host=host,
        path=path,
        query=q,
        headers={},
        payload_sha256=EMPTY,
        credentials=cred,
        now=datetime.now(UTC),
    )
    c = http.client.HTTPConnection(host, timeout=30)
    qs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(q.items())
    )
    c.request(method, urllib.parse.quote(path, safe="/-_.~") + ("?" + qs if qs else ""), None, h)
    r = c.getresponse()
    b = r.read()
    c.close()
    return r.status, b


deleted = 0
token = None
while True:
    q = {"list-type": "2"}
    if token:
        q["continuation-token"] = token
    st, body = call("GET", f"/{BUCKET}", q)
    assert st == 200, (st, body[:200])
    x = body.decode()
    for k in re.findall(r"<Key>([^<]+)</Key>", x):
        s, _ = call("DELETE", f"/{BUCKET}/{k}")
        assert s in (200, 204), (s, k)
        deleted += 1
    m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", x)
    if not m:
        break
    token = m.group(1)
print("objects deleted:", deleted)
print("bucket delete status:", call("DELETE", f"/{BUCKET}")[0])
