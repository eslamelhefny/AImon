"""Release files must not carry anything from the build machine or its owner: no home folder paths,
no user name in paths, no keys or tokens, no private notes. Runs on whatever is in dist/ (and the .vsix);
skipped when nothing has been built. The builder's own name and paths are worked out at run time,
so they are never written into this repository."""
import getpass
import os
import re
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = sorted([*ROOT.glob("dist/*.whl"), *ROOT.glob("dist/*.tar.gz"), *ROOT.glob("dist/*.zip"),
                    *ROOT.glob("vscode-extension/*.vsix")])

SECRET_SHAPES = re.compile(
    rb"sk-ant-[A-Za-z0-9_-]{20,}|sk-or-v1-[a-f0-9]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}"
    rb"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{20,}|xox[bp]-[\w-]{10,}|AIza[0-9A-Za-z_-]{30,}"
    rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
PRIVATE_FILES = ("CLAUDE.md", "docs/vps.md", ".env", "config.yaml", "monitor.db", "/tests/", "/.claude/",
                 "aimeter.log")


def builder_markers():
    """Byte strings that would identify the person or machine that built the files."""
    user = getpass.getuser()
    home = os.path.expanduser("~")
    out = {home, home.replace("\\", "/"), "/".join(home.replace("\\", "/").split("/")[-2:])}
    if len(user) >= 3:
        out |= {f"Users\\{user}", f"Users/{user}", f"home/{user}", f"Users\\\\{user}"}
    return [m.encode() for m in out if len(m) >= 6]


def files_in(path: Path):
    if path.name.endswith((".whl", ".zip", ".vsix")):
        z = zipfile.ZipFile(path)
        for n in z.namelist():
            yield n, z.read(n)
    else:
        t = tarfile.open(path)
        for m in t.getmembers():
            if m.isfile():
                yield m.name, t.extractfile(m).read()


pytestmark = pytest.mark.skipif(not ARTIFACTS, reason="nothing built yet (run scripts/build_*.sh)")


@pytest.mark.parametrize("artifact", ARTIFACTS, ids=lambda p: p.name)
def test_no_trace_of_the_build_machine_or_secrets(artifact):
    markers, problems = builder_markers(), []
    for name, data in files_in(artifact):
        low = name.replace("\\", "/")
        if low.endswith("direct_url.json"):
            problems.append(f"{name}: pip's record of the local wheel path")
        if "__pycache__" in low and "/python/Lib/" in low and "site-packages" in low:
            problems.append(f"{name}: compiled file from the build machine")
        third_party = "site-packages/" in low and "site-packages/ai_meter" not in low
        if not third_party and any(p in "/" + low for p in PRIVATE_FILES):
            problems.append(f"{name}: private file")
        for m in markers:
            if m in data:
                problems.append(f"{name}: contains the builder's home folder or user name")
                break
        hit = SECRET_SHAPES.search(data)
        if hit:
            problems.append(f"{name}: something shaped like a key or token ({hit.group(0)[:8]!r}...)")
    assert not problems, "\n".join(problems[:20])


def test_windows_app_does_not_ship_pip():
    for z in ROOT.glob("dist/AI-Meter-Windows.zip"):
        names = zipfile.ZipFile(z).namelist()
        assert not [n for n in names if "/site-packages/pip/" in n or "/site-packages/pip-" in n]


def test_python_package_ships_only_the_app():
    for whl in ROOT.glob("dist/*.whl"):
        names = zipfile.ZipFile(whl).namelist()
        assert all(n.startswith(("ai_meter/", "ai_meter-")) for n in names), names
