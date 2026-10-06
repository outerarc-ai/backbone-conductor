"""Check release archives before publishing."""

from __future__ import annotations

import argparse
import ast
import re
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "backbone_conductor"


def check_release(root: Path, tag: str = "", dist: Path | None = None) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    name = project["name"]
    version = project["version"]
    if name != "backbone-conductor" or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("unexpected package name or non-final version")
    if project.get("license") != "MIT" or project.get("license-files") != ["LICENSE"]:
        raise ValueError("MIT license metadata is missing")
    if any(value.startswith("License ::") for value in project.get("classifiers", [])):
        raise ValueError("legacy license classifier must not accompany the SPDX license")
    if tag and tag != f"v{version}":
        raise ValueError(f"tag {tag!r} does not match v{version}")
    source = (root / "src" / PACKAGE / "__init__.py").read_text()
    tree = ast.parse(source)
    versions = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
    ]
    if versions != [version]:
        raise ValueError("package __version__ differs from release version")

    dist = dist or root / "dist"
    wheel = dist / f"{PACKAGE}-{version}-py3-none-any.whl"
    sdist = dist / f"{PACKAGE}-{version}.tar.gz"
    files = {path for path in dist.iterdir() if path.name != ".gitignore"}
    if files != {wheel, sdist}:
        raise ValueError("dist must contain exactly this version's wheel and sdist")

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_path = f"{PACKAGE}-{version}.dist-info/METADATA"
        license_path = f"{PACKAGE}-{version}.dist-info/licenses/LICENSE"
        if not {metadata_path, license_path, f"{PACKAGE}/__init__.py"} <= names:
            raise ValueError("wheel is missing metadata, license, or package")
        web_files = {
            *(f"{PACKAGE}/web/decision_map.{ext}" for ext in ("html", "css", "js")),
            f"{PACKAGE}/web/lifecycle_map.html",
            f"{PACKAGE}/web/lifecycle_map.js",
        }
        if not web_files <= names:
            raise ValueError("wheel is missing map assets")
        metadata = Parser().parsestr(archive.read(metadata_path).decode())
        if (
            metadata.get("Name") != name
            or metadata.get("Version") != version
            or metadata.get("License-Expression") != "MIT"
            or metadata.get("License") is not None
            or any(value.startswith("License ::") for value in metadata.get_all("Classifier", []))
        ):
            raise ValueError("wheel metadata does not match project release")
        if archive.read(license_path) != (root / "LICENSE").read_bytes():
            raise ValueError("wheel license differs from source")
        if archive.read(f"{PACKAGE}/__init__.py").decode() != source:
            raise ValueError("wheel package version differs from source")

    prefix = f"{PACKAGE}-{version}/"
    with tarfile.open(sdist, "r:gz") as archive:
        names = {member.name for member in archive.getmembers() if member.isfile()}
        required = {
            f"{prefix}{path}"
            for path in ("LICENSE", "README.md", "pyproject.toml", f"src/{PACKAGE}/__init__.py")
        }
        if not required <= names:
            raise ValueError("sdist is missing the license, README, project metadata, or package")
        web_files = {
            *(f"{prefix}src/{PACKAGE}/web/decision_map.{ext}" for ext in ("html", "css", "js")),
            f"{prefix}src/{PACKAGE}/web/lifecycle_map.html",
            f"{prefix}src/{PACKAGE}/web/lifecycle_map.js",
        }
        if not web_files <= names:
            raise ValueError("sdist is missing map assets")
        for path in ("LICENSE", "pyproject.toml", f"src/{PACKAGE}/__init__.py"):
            member = archive.extractfile(f"{prefix}{path}")
            if member is None or member.read() != (root / path).read_bytes():
                raise ValueError(f"sdist {path} differs from source")
        package_info = archive.extractfile(f"{prefix}PKG-INFO")
        if package_info is None:
            raise ValueError("sdist is missing PKG-INFO")
        metadata = Parser().parsestr(package_info.read().decode())
        if (
            metadata.get("Name") != name
            or metadata.get("Version") != version
            or metadata.get("License-Expression") != "MIT"
            or metadata.get("License") is not None
            or any(value.startswith("License ::") for value in metadata.get_all("Classifier", []))
        ):
            raise ValueError("sdist metadata does not match project release")
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="", help="Expected release tag, such as v0.1.0")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist", help="Archive directory")
    args = parser.parse_args()
    print(f"Release archives verified: {check_release(ROOT, args.tag, args.dist)}")
