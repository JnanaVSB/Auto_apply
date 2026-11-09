"""`./setup.sh`, run as a copy in a throwaway folder with fake system tools, conda and curl: never the real conda,
the network, the keychain or data/."""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "setup.sh"
# The only real programs on the PATH: none of them downloads anything.
REAL_TOOLS = ("awk", "bash", "cat", "cp", "dirname", "mkdir", "mktemp", "rm", "sha256sum", "touch")
IMPORT = "run -n auto_apply python -c import sqlcipher3, app.setup, app.web.main"  # does the environment work?
BUILD = "create -y -n auto_apply --override-channels -c conda-forge --file conda-linux-64.lock"  # conda-forge only
PIP = "run --no-capture-output -n auto_apply pip install --require-hashes --no-deps -r requirements-pip.txt"
STOPPED = "Stopped. Nothing is undone; run ./setup.sh again to continue.\n"
RELEASE = "https://github.com/conda-forge/miniforge/releases/download/26.9.4-1"
MINIFORGE = f"{RELEASE}/Miniforge3-26.9.4-1-Linux-x86_64.sh"
# What Macs' bash 3.2 lacks (or what reads oddly there), from the 8F review
BASH_4_ONLY = r"\b(?:declare|local|mapfile|readarray|coproc)\b|,,|\^\^|\[\[|\|&|;&|&>>|\$\{\w+@|\$\{!|<<<|printf -v|=~"
FAKES = {
    "uname": 'case $1 in -s) echo "$FAKE_SYSTEM" ;; -m) echo "$FAKE_MACHINE" ;; esac',
    "sw_vers": 'echo "$FAKE_MACOS"',
    "sysctl": '[ -n "$FAKE_TRANSLATED" ] || exit 1\necho "$FAKE_TRANSLATED"',  # an Intel Mac has no such setting
    # spaces in the disk's name and in where it is mounted, as macOS can have
    "df": 'echo "$*" >> "$STATE/df.log"\necho "Filesystem 1024-blocks Used Available Capacity Mounted on"\n'
    'echo "map auto home 100 50 $FAKE_FREE_KB 50% /System/My Volumes"',
    "curl": 'echo "$*" >> "$STATE/curl.log"\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
    'cp "$STATE/installer" "$out"',
    "conda": """if [ "$1" = --version ]; then
    [ "$FAKE_CONDA_VERSION" != fails ] || exit 1; echo "$FAKE_CONDA_VERSION"; exit 0
fi
echo "$*" >> "$STATE/conda.log"
case "$1 $2" in
"env list") echo "# conda environments:"; echo "base  /fake/base"
    if [ -e "$STATE/listed" ]; then echo "auto_apply  $STATE/envs/auto_apply"; fi; exit 0 ;;
"env remove") [ ! -e "$STATE/remove-fails" ] || exit 1; rm -f "$STATE/listed" "$STATE/works"; exit 0 ;;
"env create") touch "$STATE/listed" "$STATE/works"; exit 0 ;;
esac
case "$1" in
create) [ ! -e "$STATE/ctrl-c" ] || { kill -INT "$PPID"; exit 1; }  # stopped by Ctrl+C, as conda exits then
    [ ! -e "$STATE/build-fails" ] || exit 1; touch "$STATE/listed" "$STATE/works" ;;
run) case "$*" in
    *"print(sys.executable)"*) if [ -e "$STATE/no-python" ]; then echo "$STATE/gone"; else echo "$STATE/python"; fi ;;
    *"pip install"*) ;;
    *) [ -e "$STATE/works" ] ;;
    esac ;;
esac""",
}
OLD_CONDA = """if [ "$1" = --version ]; then
    [ "{version}" != fails ] || exit 1; echo "{version}"; exit 0
fi
echo "$*" >> "$STATE/old.log"
exit 1"""
# $BASH names the bash that runs it
INSTALLER = (
    b'#!/bin/bash\necho "$BASH $*" >> "$STATE/installer.log"\nmkdir -p "$3/bin"\ncp "$STATE/conda" "$3/bin/conda"\n'
)


def executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)
    return path


class Sandbox:
    """A project folder holding a copy of setup.sh, an empty home, fake tools first on the PATH, and a state folder
    where the fakes keep their logs and the fake environment's condition."""

    def __init__(self, root: Path) -> None:
        self.project, self.home, self.state, self.temp = (root / name for name in ("project", "home", "state", "tmp"))
        for folder in (self.project, self.home, self.state, self.temp):
            folder.mkdir()
        shutil.copy(SCRIPT, self.project / "setup.sh")
        for name in ("conda-linux-64.lock", "environment.yml", "requirements-pip.txt"):
            (self.project / name).write_text("made-up\n")
        self.fakes, self.tools = root / "fakes", root / "tools"
        for name, body in FAKES.items():
            executable(self.fakes / name, body)
        executable(self.state / "conda", FAKES["conda"])  # what the fake installer puts in ~/miniforge3
        executable(self.state / "python", 'echo "$PWD $*" >> "$STATE/python.log"')
        (self.state / "installer").write_bytes(INSTALLER)
        self.tools.mkdir()
        for name in REAL_TOOLS:
            (self.tools / name).symlink_to(shutil.which(name))
        self.env = {  # built from nothing, so no real CONDA_EXE or PATH leaks in
            "HOME": str(self.home),
            "PATH": f"{self.fakes}:{self.tools}",
            "TMPDIR": str(self.temp),
            "STATE": str(self.state),
            "FAKE_SYSTEM": "Linux",
            "FAKE_MACHINE": "x86_64",
            "FAKE_MACOS": "14.5",
            "FAKE_TRANSLATED": "",
            "FAKE_FREE_KB": "10485760",
            "FAKE_CONDA_VERSION": "conda 26.9.4",
        }
        (self.state / "works").touch()  # a good environment, unless a test says otherwise
        (self.state / "listed").touch()

    def run(self, *args: str, answers: str = "", **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603 (a copy of our own script, with fakes only)
            [str(self.project / "setup.sh"), *args],
            env=self.env | env,
            input=answers,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def log(self, name: str) -> list[str]:
        path = self.state / f"{name}.log"
        return path.read_text().splitlines() if path.exists() else []

    def no_environment(self) -> None:
        (self.state / "works").unlink()
        (self.state / "listed").unlink()

    def no_conda(self) -> None:
        (self.fakes / "conda").unlink()
        self.no_environment()

    def old_conda(self, version: str = "conda 4.8.3") -> None:
        """An older conda first on the PATH, which logs anything but its version to old.log."""
        executable(self.fakes / "conda", OLD_CONDA.format(version=version))

    def pin(self, data: bytes, name: str = "LINUX_SHA256") -> None:
        """The copy's fingerprint for one system replaced by that of these bytes: the real script's pin can't be
        changed."""
        script = self.project / "setup.sh"
        lines = script.read_text().splitlines(keepends=True)
        lines = [
            f"{name}={hashlib.sha256(data).hexdigest()}\n" if line.startswith(f"{name}=") else line for line in lines
        ]
        script.write_text("".join(lines))


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


def test_the_real_script_is_valid_bash_and_executable():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0  # noqa: S603, S607
    assert os.access(SCRIPT, os.X_OK)
    assert SCRIPT.read_text().startswith("#!/bin/bash\n")  # never a Homebrew Intel bash found on the PATH


def test_the_real_script_uses_nothing_newer_than_bash_3_2():
    code = re.sub(r"(^|\s)#.*", "", SCRIPT.read_text(), flags=re.MULTILINE)  # comments may say anything
    assert re.findall(BASH_4_ONLY, code) == []


@pytest.mark.parametrize("args", [["--fast"], ["--environment-only", "again"]])
def test_an_unknown_argument_shows_the_usage(sandbox, args):
    result = sandbox.run(*args)
    assert result.returncode == 2
    assert result.stderr == "Usage: ./setup.sh [--environment-only]\n"
    assert sandbox.log("conda") == []


@pytest.mark.parametrize(
    ("system", "machine", "macos", "translated", "stop"),
    [
        ("Linux", "aarch64", "", "", "Only Linux on x86-64 processors is supported; this one is aarch64."),
        ("Darwin", "x86_64", "14.5", "", "Intel Macs are not supported for now: one package has no Intel build"),
        ("Darwin", "x86_64", "14.5", "1", "This Terminal runs under Rosetta: open Terminal without Rosetta"),
        ("Darwin", "arm64", "10.15.7", "", "This needs macOS 11 or newer; this Mac has macOS 10.15.7."),
        ("FreeBSD", "amd64", "", "", "This system (FreeBSD) is not supported"),
    ],
)
def test_an_unsupported_system_stops_first(sandbox, system, machine, macos, translated, stop):
    result = sandbox.run(FAKE_SYSTEM=system, FAKE_MACHINE=machine, FAKE_MACOS=macos, FAKE_TRANSLATED=translated)
    assert result.returncode == 1
    assert result.stderr.startswith(stop)
    assert sandbox.log("df") == sandbox.log("conda") == []


def test_linux_on_x86_goes_on_and_hands_over_to_the_app_setup(sandbox):
    result = sandbox.run()
    assert result.returncode == 0, result.stderr
    assert "macOS is untested" not in result.stdout
    assert "Environment auto_apply: ready (left as it is)." in result.stdout
    assert sandbox.log("python") == [f"{sandbox.project} -m app.setup"]  # the environment's Python, in the project


def test_an_apple_silicon_mac_goes_on_with_the_untested_note(sandbox):
    result = sandbox.run(FAKE_SYSTEM="Darwin", FAKE_MACHINE="arm64", FAKE_MACOS="11.0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("macOS is untested so far: please tell whoever gave you this how it went.\n")


def test_little_free_space_is_fine_when_nothing_needs_building(sandbox):
    result = sandbox.run(FAKE_FREE_KB="1048576")
    assert result.returncode == 0, result.stderr
    assert sandbox.log("df") == []
    assert sandbox.log("python") == [f"{sandbox.project} -m app.setup"]


def test_too_little_free_space_for_the_environment_names_the_folder(sandbox):
    sandbox.no_environment()
    result = sandbox.run(FAKE_FREE_KB="1048576")
    assert result.returncode == 1
    assert result.stderr == f"Setup needs at least 3 GB free in {sandbox.home}, which has 1.0 GB free.\n"
    assert sandbox.log("df") == [f"-Pk {sandbox.home}"]
    assert not any(line.startswith(("create", "env ")) for line in sandbox.log("conda"))


def test_too_little_free_space_for_miniforge_stops_before_the_download(sandbox):
    sandbox.no_conda()
    result = sandbox.run(answers="y\n", FAKE_FREE_KB="1048576")
    assert result.returncode == 1
    assert result.stderr.startswith(f"Setup needs at least 3 GB free in {sandbox.home}")
    assert sandbox.log("curl") == []


@pytest.mark.parametrize("version", ["conda 26.1.0", "conda 27.0.1"])
def test_a_new_enough_conda_builds_the_environment(sandbox, version):
    sandbox.no_environment()
    assert sandbox.run(FAKE_CONDA_VERSION=version).returncode == 0
    assert BUILD in sandbox.log("conda")


def test_an_old_conda_with_a_good_environment_is_left_alone(sandbox):
    result = sandbox.run(FAKE_CONDA_VERSION="conda 4.8.3")  # it built the environment already, so it reads the lock
    assert result.returncode == 0, result.stderr
    assert "setup needs 26.1" not in result.stdout
    assert sandbox.log("python") == [f"{sandbox.project} -m app.setup"]


@pytest.mark.parametrize(
    ("version", "problem"),
    [
        ("conda 4.8.3", "is version 4.8.3"),
        ("conda 26.0.9", "is version 26.0.9"),
        ("fails", "doesn't run"),
        ("hello", "doesn't run"),
        ("conda 26", "doesn't run"),
        ("conda 2x.1", "doesn't run"),
        ("", "doesn't run"),
    ],
)
def test_an_old_or_broken_conda_gets_miniforge_offered_instead(sandbox, version, problem):
    sandbox.no_environment()
    result = sandbox.run(answers="n\n", FAKE_CONDA_VERSION=version)
    assert result.returncode == 1
    assert result.stdout.startswith(
        f"The conda at {sandbox.fakes / 'conda'} {problem}; setup needs 26.1 or newer, so it can install its own "
        "Miniforge in ~/miniforge3 instead. That conda is left as it is.\nInstall Miniforge 26.9.4-1"
    )
    assert result.stderr.startswith("Nothing installed. Install Miniforge yourself in ~/miniforge3")
    assert sandbox.log("conda") == [IMPORT]  # only asked whether the environment works, then its version
    assert sandbox.log("curl") == []


def test_yes_installs_miniforge_beside_an_old_conda_and_builds_with_it(sandbox):
    sandbox.old_conda()
    sandbox.no_environment()
    sandbox.pin(INSTALLER)
    result = sandbox.run(answers="y\n")
    assert result.returncode == 0, result.stderr
    assert "That conda is left as it is." in result.stdout
    assert sandbox.log("installer") == [f"/bin/bash -b -p {sandbox.home}/miniforge3"]
    assert BUILD in sandbox.log("conda")  # the new Miniforge's conda
    assert sandbox.log("old") == [IMPORT]  # the old conda only asked whether the environment works
    assert sandbox.log("python") == [f"{sandbox.project} -m app.setup"]


def test_miniforge_comes_before_an_older_conda_on_the_path(sandbox):
    sandbox.old_conda()
    executable(sandbox.home / "miniforge3" / "bin" / "conda", FAKES["conda"])
    result = sandbox.run()
    assert result.returncode == 0, result.stderr
    assert sandbox.log("old") == []
    assert sandbox.log("conda")[0] == IMPORT


@pytest.mark.parametrize(("version", "problem"), [("conda 4.8.3", "is version 4.8.3"), ("fails", "doesn't run")])
def test_an_old_or_broken_miniforge_of_setups_own_stops_and_is_left(sandbox, version, problem):
    sandbox.no_conda()
    conda = executable(sandbox.home / "miniforge3" / "bin" / "conda", OLD_CONDA.format(version=version))
    result = sandbox.run(answers="y\n")
    assert result.returncode == 1
    assert result.stderr == (
        f"The Miniforge in ~/miniforge3 {problem}; setup needs conda 26.1 or newer. Move or remove that folder, then "
        "run ./setup.sh again: it installs a fresh one.\n"
    )
    assert conda.exists()
    assert "Install Miniforge" not in result.stdout
    assert sandbox.log("old") == [IMPORT]


@pytest.mark.parametrize("place", ["CONDA_EXE", "miniconda3", "anaconda3"])
def test_a_conda_elsewhere_is_used_as_it_is(sandbox, place):
    (sandbox.fakes / "conda").unlink()
    found = sandbox.home / place / "bin" / "conda" if place != "CONDA_EXE" else sandbox.home / "tools" / "conda"
    executable(found, FAKES["conda"])
    result = sandbox.run(**({"CONDA_EXE": str(found)} if place == "CONDA_EXE" else {}))
    assert result.returncode == 0, result.stderr
    assert sandbox.log("conda")[0] == IMPORT
    assert not (sandbox.home / "miniforge3").exists()


def test_no_conda_and_no_stops_with_the_manual_steps_and_downloads_nothing(sandbox):
    sandbox.no_conda()
    result = sandbox.run(answers="n\n")
    assert result.returncode == 1
    assert result.stdout.startswith(
        "Install Miniforge 26.9.4-1 (about 130 MB download) in ~/miniforge3? It changes no shell settings. [y/N] "
    )
    assert result.stderr == (
        "Nothing installed. Install Miniforge yourself in ~/miniforge3 (https://github.com/conda-forge/miniforge) or "
        "update your conda to 26.1 or newer, then run ./setup.sh again.\n"
    )
    assert sandbox.log("curl") == []


def test_ctrl_d_at_a_question_is_no(sandbox):
    sandbox.no_conda()
    assert sandbox.run(answers="").returncode == 1
    assert sandbox.log("curl") == []


def test_a_conda_shell_function_is_not_taken_for_conda(sandbox):
    sandbox.no_conda()
    function = {"BASH_FUNC_conda%%": '() {  echo "$*" >> "$STATE/function.log"; }'}  # as `conda init` makes one
    result = sandbox.run(answers="no\n", **function)
    assert "Install Miniforge" in result.stdout
    assert sandbox.log("function") == []


def test_a_miniforge_folder_without_conda_is_never_overwritten(sandbox):
    sandbox.no_conda()
    (sandbox.home / "miniforge3").mkdir()
    result = sandbox.run(answers="y\n")
    assert result.returncode == 1
    assert result.stderr.startswith(f"{sandbox.home}/miniforge3 exists but has no conda in it")
    assert "Install Miniforge" not in result.stdout
    assert sandbox.log("curl") == []


def test_no_curl_is_named(sandbox):
    sandbox.no_conda()
    (sandbox.fakes / "curl").unlink()
    result = sandbox.run(answers="y\n")
    assert result.returncode == 1
    assert result.stderr == "curl is needed to download Miniforge: install curl, then run ./setup.sh again.\n"


def test_a_wrong_fingerprint_installs_nothing_and_leaves_no_download(sandbox):
    sandbox.no_conda()
    result = sandbox.run(answers="y\n")  # the fake installer's fingerprint isn't the pinned one
    assert result.returncode == 1
    assert result.stderr == "The download's fingerprint is wrong; nothing was installed. Run ./setup.sh again later.\n"
    installer = MINIFORGE.rsplit("/", 1)[1]
    (download,) = sandbox.log("curl")  # into a folder of its own in the temp folder, from the pinned address
    assert download.startswith(f"-q -fL --proto =https --tlsv1.2 --connect-timeout 30 -o {sandbox.temp}/")
    assert download.endswith(f"/{installer} {MINIFORGE}")
    assert sandbox.log("installer") == []
    assert not (sandbox.home / "miniforge3").exists()
    assert list(sandbox.temp.iterdir()) == []


def test_miniforge_with_the_right_fingerprint_is_installed_then_the_environment_built(sandbox):
    sandbox.no_conda()
    sandbox.pin(INSTALLER)
    result = sandbox.run(answers=" Yes \n")
    assert result.returncode == 0, result.stderr
    assert sandbox.log("installer") == [f"/bin/bash -b -p {sandbox.home}/miniforge3"]
    assert list(sandbox.temp.iterdir()) == []
    assert BUILD in sandbox.log("conda")
    assert sandbox.log("python") == [f"{sandbox.project} -m app.setup"]


def test_a_good_environment_is_left_alone(sandbox):
    result = sandbox.run()
    assert result.returncode == 0, result.stderr
    assert all(line.startswith("run -n auto_apply python -c ") for line in sandbox.log("conda"))


def test_a_broken_environment_is_rebuilt_only_after_yes(sandbox):
    (sandbox.state / "works").unlink()
    result = sandbox.run(answers="n\n")
    assert result.returncode == 1
    assert "Environment auto_apply is there but can't load the app (broken or half-built). Rebuild it?" in result.stdout
    assert not any(line.startswith(("env remove", "create")) for line in sandbox.log("conda"))
    assert (sandbox.state / "listed").exists()
    result = sandbox.run(answers="y\n")
    assert result.returncode == 0, result.stderr
    changes = [line for line in sandbox.log("conda") if line.startswith(("env remove", "create"))]
    assert changes == ["env remove -y -n auto_apply", BUILD]


def test_a_missing_environment_on_linux_comes_from_the_lock_file_and_hashed_pip(sandbox):
    sandbox.no_environment()
    result = sandbox.run()
    assert result.returncode == 0, result.stderr
    assert sandbox.log("conda")[2:4] == [BUILD, PIP]
    assert "Environment auto_apply: ready." in result.stdout


def test_a_missing_environment_on_a_mac_comes_from_environment_yml(sandbox):
    sandbox.no_environment()
    result = sandbox.run(FAKE_SYSTEM="Darwin", FAKE_MACHINE="arm64")
    assert result.returncode == 0, result.stderr
    assert "env create -f environment.yml" in sandbox.log("conda")
    assert "its versions are pinned, but not exact like Linux's" in result.stdout


def test_a_failed_build_says_what_to_check(sandbox):
    sandbox.no_environment()
    (sandbox.state / "build-fails").touch()
    result = sandbox.run()
    assert result.returncode == 1
    assert result.stderr.startswith(f"Building the environment failed: check the free space in {sandbox.home}")
    assert sandbox.log("python") == []


def test_environment_only_stops_before_the_app_setup(sandbox):
    result = sandbox.run("--environment-only")
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("Environment ready; --environment-only, so the app's own setup was not run.\n")
    assert sandbox.log("python") == []


def test_a_second_run_changes_nothing(sandbox):
    sandbox.no_environment()
    assert sandbox.run().returncode == 0
    before = sorted(sandbox.project.iterdir()), sorted(sandbox.home.iterdir())
    (sandbox.state / "conda.log").unlink()
    result = sandbox.run()
    assert result.returncode == 0, result.stderr
    assert (sorted(sandbox.project.iterdir()), sorted(sandbox.home.iterdir())) == before
    assert all(line.startswith("run -n auto_apply python -c ") for line in sandbox.log("conda"))
    assert "Install Miniforge" not in result.stdout
    assert "Rebuild it?" not in result.stdout


def test_ctrl_c_during_the_download_leaves_no_download(sandbox):
    sandbox.no_conda()
    executable(sandbox.fakes / "curl", FAKES["curl"] + '\nkill -INT "$PPID"')  # Ctrl+C once the file is there
    result = sandbox.run(answers="y\n")
    assert result.returncode == 130
    assert result.stderr == STOPPED
    assert list(sandbox.temp.iterdir()) == []
    assert sandbox.log("installer") == []


def test_ctrl_c_while_building_says_stopped_not_failed(sandbox):
    sandbox.no_environment()
    (sandbox.state / "ctrl-c").touch()
    result = sandbox.run()
    assert result.returncode == 130
    assert result.stderr == STOPPED
    assert sandbox.log("python") == []


@pytest.mark.skipif(not Path("/usr/bin/shasum").exists(), reason="needs shasum, the hashing tool a Mac has")
def test_a_mac_installs_its_own_miniforge_checked_with_shasum(sandbox):
    sandbox.no_conda()
    sandbox.pin(INSTALLER, "MAC_SHA256")
    (sandbox.tools / "sha256sum").unlink()  # a Mac has no sha256sum
    (sandbox.tools / "shasum").symlink_to("/usr/bin/shasum")
    result = sandbox.run(answers="y\n", FAKE_SYSTEM="Darwin", FAKE_MACHINE="arm64", FAKE_MACOS="14.5")
    assert result.returncode == 0, result.stderr
    assert "(about 90 MB download)" in result.stdout
    (download,) = sandbox.log("curl")
    assert download.endswith(f"/Miniforge3-26.9.4-1-MacOSX-arm64.sh {RELEASE}/Miniforge3-26.9.4-1-MacOSX-arm64.sh")
    assert sandbox.log("installer") == [f"/bin/bash -b -p {sandbox.home}/miniforge3"]
    assert "env create -f environment.yml" in sandbox.log("conda")
    assert list(sandbox.temp.iterdir()) == []


def test_unreadable_free_space_stops(sandbox):
    sandbox.no_environment()
    result = sandbox.run(FAKE_FREE_KB="junk")
    assert result.returncode == 1
    assert result.stderr == f"Could not check the free space in {sandbox.home}.\n"


def test_a_failed_miniforge_install_says_to_remove_its_folder(sandbox):
    sandbox.no_conda()
    failing = b"#!/bin/bash\nexit 1\n"
    (sandbox.state / "installer").write_bytes(failing)
    sandbox.pin(failing)
    result = sandbox.run(answers="y\n")
    assert result.returncode == 1
    assert result.stderr == (
        f"Installing Miniforge failed: remove the folder {sandbox.home}/miniforge3, then run ./setup.sh again.\n"
    )


def test_a_failed_removal_of_the_broken_environment_stops(sandbox):
    (sandbox.state / "works").unlink()
    (sandbox.state / "remove-fails").touch()
    result = sandbox.run(answers="y\n")
    assert result.returncode == 1
    assert result.stderr == (
        "Removing the broken environment auto_apply failed (if it is active, run conda deactivate first).\n"
    )
    assert not any(line.startswith("create") for line in sandbox.log("conda"))


def test_an_environment_python_that_is_not_there_stops(sandbox):
    (sandbox.state / "no-python").touch()
    result = sandbox.run()
    assert result.returncode == 1
    assert result.stderr == "Could not find the Python of environment auto_apply.\n"
    assert sandbox.log("python") == []


def test_the_fingerprint_is_read_whatever_the_temp_folder_is_called(sandbox, tmp_path):
    odd = tmp_path / "odd\\temp"  # sha256sum would put a \\ before the hash of a file named like this
    odd.mkdir()
    sandbox.no_conda()
    sandbox.pin(INSTALLER)
    result = sandbox.run(answers="y\n", TMPDIR=str(odd))
    assert result.returncode == 0, result.stderr
    assert list(odd.iterdir()) == []


def test_an_exported_cdpath_doesnt_move_setup_to_another_folder(sandbox, tmp_path):
    (tmp_path / "decoy" / "project").mkdir(parents=True)  # where `cd project` would go with this CDPATH
    result = subprocess.run(  # noqa: S603 (a copy of our own script, with fakes only)
        ["project/setup.sh"],  # noqa: S607 (by its relative path on purpose)
        cwd=tmp_path,
        env=sandbox.env | {"CDPATH": str(tmp_path / "decoy")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert sandbox.log("python") == [f"{sandbox.project} -m app.setup"]
