#!/usr/bin/env python3
"""Sync the Tokcos Scoop manifests in this bucket with the latest upstream release.

Invoked by .github/workflows/bump-packages.yml.

Checksum policy
---------------
The *served artifact* is the source of truth for every hash written here, because
that is what `scoop install` downloads and validates.  Upstream `latest.json`
supplies the version and is cross-checked, but it has been observed to be wrong --
tokcos-cli 0.5.9 advertises a win32-x64 `archive` hash and `size` that do not match
the zip actually served.  Such a disagreement is surfaced as a GitHub warning
rather than being silently trusted or silently ignored.

Beware the two schemas: for tokscos-work the usable zip hash is `files.<p>.sha256`,
whereas for tokscos-cli `platforms.<p>.sha256` is the hash of the *unpacked exe*
and `platforms.<p>.archive` is the zip.  Getting that wrong yields a manifest that
passes review but fails at install time.

For tokscos-cli the unpacked binary hash doubles as an identity check: if it does
not match, the artifact is not the build the metadata describes and the run fails.

Rewriting
---------
The manifest is edited as parsed JSON, not by regex over the text.  A Scoop
manifest holds several `"url"` keys -- the download, `checkver.url` pointing at
latest.json, and the `autoupdate` template -- so a global textual replacement
would rewrite the version-discovery machinery too and silently break checkver.
Editing the parsed document keeps the change to exactly the three fields that
should move.

Cost
----
Artifacts are downloaded only when the declared version actually changed, so the
daily run normally performs two small JSON fetches and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COS = "https://tokcos-1328134559.cos.ap-guangzhou.myqcloud.com"
UA = {"User-Agent": "greenflute-scoop-repo-bump"}


@dataclass(frozen=True)
class Package:
    """One Scoop manifest plus the upstream layout it is fed from."""

    name: str
    manifest: Path
    meta_url: str
    platform: str
    #: where the artifact list lives in latest.json ("files" or "platforms")
    meta_section: str
    #: metadata key holding the hash of the downloadable archive
    archive_hash_key: str
    #: metadata key holding a sha256 of the unpacked binary, if upstream has one
    binary_hash_key: str | None
    #: path of the main binary inside the archive, if binary_hash_key is set
    binary_member: str | None
    #: filename upstream publishes for this platform (may contain {version})
    artifact_name: str


PACKAGES = (
    Package(
        name="tokcos-work",
        manifest=REPO / "bucket/tokcos-work.json",
        meta_url=f"{COS}/tokcos-gui/release/latest.json",
        platform="win32-x64",
        meta_section="files",
        archive_hash_key="sha256",
        binary_hash_key=None,
        binary_member=None,
        artifact_name="Tokcos Work-{version}-x64.exe",
    ),
    Package(
        name="tokcos-cli",
        manifest=REPO / "bucket/tokcos-cli.json",
        meta_url=f"{COS}/tokcos-cli/release/latest.json",
        platform="win32-x64",
        meta_section="platforms",
        archive_hash_key="archive",
        binary_hash_key="sha256",
        binary_member="win32-x64/tokcos-cli.exe",
        artifact_name="tokcos-cli-win32-x64.zip",
    ),
)


def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"::warning::{message}", flush=True)


def fail(message: str) -> None:
    print(f"::error::{message}", flush=True)
    raise SystemExit(1)


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def sha256_stream(stream) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1 << 20):
        digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path) -> tuple[str, int]:
    request = urllib.request.Request(url, headers=UA)
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=900) as response, dest.open("wb") as handle:
        while chunk := response.read(1 << 20):
            handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def sha256_zip_member(archive: Path, member: str) -> str:
    with zipfile.ZipFile(archive) as zf:
        try:
            stream = zf.open(member)
        except KeyError:
            fail(f"{archive.name}: member {member!r} not found")
        with stream:
            return sha256_stream(stream)


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def assert_not_a_downgrade(current: str, latest: str, what: str) -> None:
    """Refuse to move backwards if upstream appears to have rolled a release back.

    A downgrade is occasionally the right call after a retracted release, but it
    should be a deliberate edit rather than something a nightly job commits.
    """
    before, after = version_tuple(current), version_tuple(latest)
    if not before or not after:
        warn(f"{what}: cannot compare {current!r} with {latest!r} numerically")
        return
    for old, new in zip_longest(before, after, fillvalue=0):
        if old != new:
            if new < old:
                fail(
                    f"{what}: upstream reports {latest} which is older than the committed "
                    f"{current}; refusing to downgrade. Edit the manifest by hand if this is intended."
                )
            return


def json_indent(text: str) -> int:
    """Indentation width the manifest already uses, so bumps stay one-line diffs."""
    for line in text.splitlines()[1:]:
        stripped = line.lstrip()
        if stripped:
            return len(line) - len(stripped)
    return 4


def artifact_basename(url: str) -> str:
    return urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])


def bump(package: Package) -> str | None:
    meta = fetch_json(package.meta_url)
    latest = meta["version"]
    text = package.manifest.read_text(encoding="utf-8")
    document = json.loads(text)
    current = document["version"]

    if current == latest:
        log(f"{package.name}: already at {latest}")
        return None

    log(f"{package.name}: {current} -> {latest}")
    assert_not_a_downgrade(current, latest, package.name)

    indent = json_indent(text)
    # Refuse to silently reformat a manifest that was hand-formatted; a bump should
    # change the version, url and hash and nothing else.
    if json.dumps(document, indent=indent, ensure_ascii=False) + "\n" != text:
        fail(
            f"{package.name}: manifest is not in canonical json.dumps(indent={indent}) form; "
            "reformat it deliberately before relying on automated bumps"
        )

    # The manifest's URL must name the artifact upstream publishes for the version
    # currently declared -- that is what the file still holds.  Comparing against
    # `latest` here would reject every legitimate bump; comparing against the
    # version being replaced catches an upstream rename instead of shipping a 404.
    declared_url = document["architecture"]["64bit"]["url"]
    was_expected = package.artifact_name.format(version=current)
    if artifact_basename(declared_url) != was_expected:
        fail(
            f"{package.name}: manifest downloads {artifact_basename(declared_url)!r} but version "
            f"{current} should be {was_expected!r}; update the manifest by hand"
        )

    # Derive the new URL from the manifest's own url so its quoting and any
    # `#/dl.7z`-style fragment are preserved exactly.
    new_url = declared_url.replace(current, latest)
    expected_name = package.artifact_name.format(version=latest)
    if artifact_basename(new_url) != expected_name:
        fail(f"{package.name}: new url points at {artifact_basename(new_url)!r}, expected {expected_name!r}")

    with tempfile.TemporaryDirectory() as workdir:
        archive = Path(workdir) / expected_name
        real, size = download(urllib.parse.urldefrag(new_url)[0], archive)

        info = meta[package.meta_section][package.platform]

        if package.binary_hash_key is not None:
            inner = sha256_zip_member(archive, package.binary_member)
            claimed_inner = info[package.binary_hash_key]
            if inner != claimed_inner:
                fail(
                    f"{package.name}: unpacked {package.binary_member} hashes to {inner} but "
                    f"upstream declares {claimed_inner}; refusing to bump an unverified artifact"
                )
            log(f"  binary {inner} verified")

        claimed = info[package.archive_hash_key]
        if real != claimed:
            warn(
                f"{package.name}: upstream {package.archive_hash_key} {claimed} != artifact "
                f"{real}; using the artifact value"
            )
        if "size" in info and size != info["size"]:
            warn(f"{package.name}: upstream size {info['size']} != artifact {size}")
        log(f"  archive {real} ({size} bytes)")

    document["version"] = latest
    document["architecture"]["64bit"]["url"] = new_url
    document["architecture"]["64bit"]["hash"] = real
    updated = json.dumps(document, indent=indent, ensure_ascii=False) + "\n"

    # Everything below is asserted against the exact bytes that will be committed.
    # The intended document is built independently and compared whole, so the bump
    # can only ever touch the version, the download url and the hash.  Substring
    # tests on the version are deliberately avoided: "0.5.9" is a substring of
    # "0.5.90", which would make a legitimate prefix bump look like a failure.
    intended = json.loads(text)
    intended["version"] = latest
    intended["architecture"]["64bit"]["url"] = new_url
    intended["architecture"]["64bit"]["hash"] = real
    if json.loads(updated) != intended:
        fail(f"{package.name}: rewritten manifest differs from the intended document")
    if declared_url in updated:
        fail(f"{package.name}: the old download url is still present after rewrite")
    if updated.count(real) != 1:
        fail(f"{package.name}: expected exactly one occurrence of hash {real}")

    package.manifest.write_text(updated, encoding="utf-8")
    log(f"  {package.manifest.relative_to(REPO)} rewritten")
    return latest


def main() -> int:
    versions = {package.name: bump(package) for package in PACKAGES}

    for package in PACKAGES:
        key = package.name.replace("tokcos-", "")
        version = versions[package.name]
        set_output(f"{key}_changed", "true" if version else "false")
        set_output(f"{key}_version", version or "")

    set_output("changed", "true" if any(versions.values()) else "false")
    if not any(versions.values()):
        log("All Tokcos Scoop manifests are already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
