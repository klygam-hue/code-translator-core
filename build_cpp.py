"""Compile a generated translation against the current CPython installation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile

import zig_tool


def windows_command(source: Path, output: Path, config: Path) -> list[str]:
    major, minor = sys.version_info[:2]
    suffix = "t" if sysconfig.get_config_var("Py_GIL_DISABLED") else ""
    library = Path(sys.base_prefix) / "libs" / f"python{major}{minor}{suffix}.lib"
    include = Path(sysconfig.get_path("include"))
    if not library.is_file() or not (include / "Python.h").is_file():
        raise ValueError("Python.h and the import library (.lib) for this CPython installation are required")

    if compiler := shutil.which("cl.exe"):
        object_file = config.with_name("program.obj")
        return [compiler, "/nologo", "/std:c++17", "/EHsc", "/MD", f"/I{include}",
                f"/FI{config}", f"/Fo{object_file}", f"/Fe{output}", str(source), str(library)]

    return [*zig_tool.command(), "c++", "-std=c++17", "-municode", "-I", str(include), "-include", str(config),
            str(source), str(library), "-o", str(output)]


def unix_command(source: Path, output: Path, config: Path) -> list[str]:
    major, minor = sys.version_info[:2]
    name = f"python{major}.{minor}-config"
    bindir = Path(sysconfig.get_config_var("BINDIR"))
    local_config = bindir / name
    config_command = str(local_config) if local_config.is_file() else shutil.which(name)
    compiler = shutil.which("c++")
    if not compiler or not config_command:
        raise ValueError(f"A C++ compiler and {name} from the CPython development kit are required")

    includes = shlex.split(subprocess.check_output([config_command, "--includes"], text=True))
    libraries = shlex.split(subprocess.check_output([config_command, "--embed", "--ldflags"], text=True))
    return [compiler, "-std=c++17", "-include", str(config), *includes, str(source), *libraries, "-o", str(output)]


def write_python_location(config: Path) -> None:
    encoding = "utf-16-le" if os.name == "nt" else "utf-32-le"
    unit_size = 2 if os.name == "nt" else 4
    encoded = sys.executable.encode(encoding, errors="surrogatepass")
    units = (int.from_bytes(encoded[index:index + unit_size], "little")
             for index in range(0, len(encoded), unit_size))
    path_literal = "".join(f"\\x{unit:x}" for unit in units)
    config.write_text(f'#define CODE_TRANSLATOR_PYTHON_EXECUTABLE L"{path_literal}"\n', encoding="ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a generated C++ file against CPython.")
    parser.add_argument("source", type=Path, help="generated .cpp file")
    parser.add_argument("-o", "--output", type=Path, help="path to the executable")
    args = parser.parse_args()

    source = args.source.resolve()
    if source.suffix.lower() != ".cpp" or not source.is_file():
        parser.error("provide an existing .cpp file")
    suffix = ".exe" if os.name == "nt" else ""
    output = args.output or source.with_suffix(suffix)
    if suffix and not output.suffix:
        output = output.with_suffix(suffix)
    output = output.resolve()
    if output == source or (output.exists() and output.samefile(source)):
        parser.error("the input and output files must be different")

    try:
        with tempfile.TemporaryDirectory(prefix=".code-translator-", dir=output.parent) as temporary:
            staging = Path(temporary)
            config = staging / "python_location.h"
            executable = staging / output.name
            write_python_location(config)
            if os.name == "nt":
                command = windows_command(source, executable, config)
            else:
                command = unix_command(source, executable, config)
            subprocess.run(command, check=True)
            executable.replace(output)
    except subprocess.CalledProcessError as error:
        return error.returncode
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1

    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
