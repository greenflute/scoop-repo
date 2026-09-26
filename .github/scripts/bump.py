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

Floating-URL packages
---------------------
Not every upstream versions its download path.  aardio publishes a single fixed URL
(`https://d.aardio.com/ide/aardio.7z`) and overwrites that file in place on every
release -- its own website links to exactly that URL -- so there is no immutable
per-version artifact to point at and no version segment to rewrite.  For such a
package the version is read from an upstream metadata endpoint instead, and because
the bytes behind a fixed URL can move without the reported version moving, the
declared hash cannot be assumed to stay valid: it is re-derived from whatever is
served, on every run.  The manifest is correct exactly when its (version, hash) pair
equals upstream's (reported version, served bytes).  aardio is ~7 MB, which is
negligible next to the multi-hundred-MB Tokcos artifacts.

Cost
----
Artifacts are downloaded only when the declared version actually changed, so the
daily run normally performs two small JSON fetches and nothing else.  The one
exception is a floating-URL package, whose artifact is small and is fetched every
run because checking it is the only way to know it is still current.
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


@dataclass(frozen=True)
class FloatingPackage:
    """A manifest whose download URL carries no version segment.

    See "Floating-URL packages" in the module docstring: the version comes from a
    metadata endpoint, the url is never rewritten, and the hash is re-derived from
    the served bytes on every run.
    """

    name: str
    manifest: Path
    meta_url: str
    #: metadata key holding the version string
    version_key: str


FLOATING_PACKAGES = (
    FloatingPackage(
        name="aardio",
        manifest=REPO / "bucket/aardio.json",
        meta_url="https://d.aardio.com/ide/check/",
        version_key="version",
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


def bump_floating(package: FloatingPackage) -> tuple[str | None, str | None]:
    """Sync a floating-URL manifest.  Returns (version, commit_subject), or (None, None).

    The subject is built here rather than in the workflow because a run can end in
    either of two genuinely different edits: a new version, or the same version with
    fresh bytes behind it.  Those deserve different commit messages.
    """
    meta = fetch_json(package.meta_url)
    latest = meta[package.version_key]
    text = package.manifest.read_text(encoding="utf-8")
    document = json.loads(text)
    current = document["version"]
    url = document["url"]
    declared_hash = document["hash"]

    # The whole entry rests on this url being unversioned and stable.  If upstream
    # ever publishes per-version artifacts, stop: the url no longer means "latest"
    # and this code path is the wrong one.
    if latest in url:
        fail(
            f"{package.name}: download url {url} now contains {latest}; upstream appears to "
            "have moved to versioned artifacts, which needs the versioned-url code path"
        )

    indent = json_indent(text)
    if json.dumps(document, indent=indent, ensure_ascii=False) + "\n" != text:
        fail(
            f"{package.name}: manifest is not in canonical json.dumps(indent={indent}) form; "
            "reformat it deliberately before relying on automated bumps"
        )

    with tempfile.TemporaryDirectory() as workdir:
        archive = Path(workdir) / artifact_basename(url)
        real, size = download(url, archive)
    log(f"{package.name}: upstream reports {latest}; served artifact {real} ({size} bytes)")

    if current == latest and declared_hash == real:
        log(f"{package.name}: already at {latest}")
        return None, None

    if current != latest:
        log(f"{package.name}: {current} -> {latest}")
        assert_not_a_downgrade(current, latest, package.name)
        subject = f"Update {package.name} to {latest}"
    else:
        warn(
            f"{package.name}: upstream still reports {latest} but the served bytes changed "
            f"({declared_hash} -> {real}); refreshing the hash"
        )
        subject = f"Refresh {package.name} hash for {latest}"

    document["version"] = latest
    document["hash"] = real
    updated = json.dumps(document, indent=indent, ensure_ascii=False) + "\n"

    # As in bump(): assert against the exact bytes to be committed, so the rewrite
    # can only ever have touched the version and the hash.  The url must come
    # through untouched -- that is the point of this code path.
    intended = json.loads(text)
    intended["version"] = latest
    intended["hash"] = real
    if json.loads(updated) != intended:
        fail(f"{package.name}: rewritten manifest differs from the intended document")
    if url not in updated:
        fail(f"{package.name}: the download url was lost in the rewrite")
    if updated.count(real) != 1:
        fail(f"{package.name}: expected exactly one occurrence of hash {real}")

    package.manifest.write_text(updated, encoding="utf-8")
    log(f"  {package.manifest.relative_to(REPO)} rewritten")
    return latest, subject


def main() -> int:
    versions = {package.name: bump(package) for package in PACKAGES}
    floating = {package.name: bump_floating(package) for package in FLOATING_PACKAGES}

    for package in PACKAGES:
        key = package.name.replace("tokcos-", "")
        version = versions[package.name]
        set_output(f"{key}_changed", "true" if version else "false")
        set_output(f"{key}_version", version or "")

    for package in FLOATING_PACKAGES:
        version, subject = floating[package.name]
        set_output(f"{package.name}_changed", "true" if version else "false")
        set_output(f"{package.name}_version", version or "")
        set_output(f"{package.name}_subject", subject or "")

    changed = any(versions.values()) or any(version for version, _ in floating.values())
    set_output("changed", "true" if changed else "false")
    if not changed:
        log("All Scoop manifests are already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
