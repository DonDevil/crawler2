from datetime import UTC, datetime

import pytest
from antipiracy_contracts.digests import ContentDigest

from crawler2.storage.objectstore.layout import digest_of_key, parse_uri, raw_key, uri_of
from crawler2.storage.objectstore.s3 import Credentials, sign_v4

EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sigv4_matches_the_published_aws_example() -> None:
    """'Example: GET Object' of the AWS SigV4 S3 documentation (header-based auth)."""
    headers = sign_v4(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        path="/test.txt",
        query={},
        headers={"Range": "bytes=0-9"},
        payload_sha256=EMPTY,
        credentials=Credentials(
            "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1"
        ),
        now=datetime(2013, 5, 24, tzinfo=UTC),
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_raw_key_is_a_pure_function_of_the_bytes() -> None:
    digest = ContentDigest.of_bytes(b"hello")
    key = raw_key(digest)
    assert key == f"raw/sha256/{digest.hex[:2]}/{digest.hex[2:4]}/{digest.hex}"
    assert raw_key(ContentDigest.of_bytes(b"hello")) == key
    assert raw_key(ContentDigest.of_bytes(b"hello!")) != key
    assert digest_of_key(key) == digest
    assert parse_uri(uri_of("crawler2-raw", key)) == ("crawler2-raw", key)


@pytest.mark.parametrize(
    "uri",
    [
        "http://crawler2-raw/raw/sha256/aa/bb/" + "a" * 64,
        "s3://crawler2-raw/raw/sha256/aa/bb/" + "b" * 64,  # fan-out does not match the hash
        "s3://crawler2-raw/raw/sha256/../../etc/passwd",
        "s3:///raw/sha256/aa/aa/" + "a" * 64,
    ],
)
def test_foreign_or_inconsistent_uris_are_rejected(uri: str) -> None:
    with pytest.raises(ValueError, match="not a"):
        parse_uri(uri)
