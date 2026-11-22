"""Command-line entry point for source-to-source translation."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

import portable_emit
import portable_ir
import portable_parse
import python_to_cpp
from translation import Translation
import zig_tool


def translate_c_to_zig(source: Path) -> Translation | None:
    try:
        zig = zig_tool.command()
    except ValueError:
        return None
    command = [*zig, "translate-c", "-lc", str(source.resolve(strict=True))]
    result = subprocess.run(command, stdout=subprocess.PIPE, check=True)
    return Translation(result.stdout.decode("utf-8"))


EXTENSIONS = portable_parse.EXTENSIONS
LANGUAGES = {**{extension[1:]: name for name, extension in EXTENSIONS.items()},
             **{name: name for name in EXTENSIONS}, "c++": "cpp", "cxx": "cpp", "cc": "cpp", "c#": "csharp"}
SOURCE_EXTENSIONS = {**LANGUAGES, "pyw": "python", "mjs": "javascript", "cjs": "javascript",
                     "mts": "typescript", "cts": "typescript", "hpp": "cpp", "nasm": "assembly"}


def language(argument: str) -> str:
    name = LANGUAGES.get(argument.lower())
    if name is None:
        raise argparse.ArgumentTypeError(f"неизвестный язык {argument}. Список доступен через --list")
    return name


def directions() -> None:
    for source in EXTENSIONS:
        print(f"{source:12} → {', '.join(EXTENSIONS)}")
    print("Python → C++ поддерживает совместимость с CPython. C → Zig использует Zig при наличии.")


def translate_outputs(source: Path, original: str, targets: list[str], mode: str) -> list[Translation]:
    analysis = None
    results = []
    for target in targets:
        pair = original, target
        if original == target and original in EXTENSIONS:
            results.append(Translation(source.read_bytes()))
            continue
        if pair == ("python", "cpp") and mode != "portable":
            results.append(python_to_cpp.translate(source, mode))
            continue
        if pair == ("c", "zig") and mode == "auto":
            result = translate_c_to_zig(source)
            if result is not None:
                results.append(result)
                continue
        if mode == "compatible":
            raise ValueError("режим compatible доступен для перевода Python → C++")
        if original not in EXTENSIONS or target not in EXTENSIONS:
            raise ValueError(f"направление {original} → {target} недоступно в режиме {mode}. См. --list")
        if analysis is None:
            analysis = portable_ir.Analysis(portable_parse.parse(source, original)).run()
        results.append(Translation(portable_emit.render_analysis(analysis, target)))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перевод исходного кода между языками программирования.")
    parser.add_argument("input", nargs="?", type=Path, help="исходный файл, язык определяется по расширению")
    parser.add_argument("-o", "--output", type=Path, help="имя файла, когда указан один язык назначения")
    parser.add_argument("--from", dest="source", type=language, help="переопределить исходный язык")
    parser.add_argument("--to", dest="targets", type=language, action="append", help="язык назначения, флаг можно повторить")
    parser.add_argument("--list", action="store_true", help="показать доступные направления")
    parser.add_argument("--mode", choices=("auto", "strict", "compatible", "portable"), default="auto",
                        help="auto выбирает генератор, portable использует общее ядро, compatible встраивает Python")
    args = parser.parse_args(argv)
    if args.list:
        directions()
        return 0
    if args.input is None:
        parser.error("укажите исходный файл или --list")

    original = args.source or SOURCE_EXTENSIONS.get(args.input.suffix.lower().removeprefix("."))
    if original is None:
        parser.error("не удалось определить язык по расширению. Укажите --from или посмотрите --list")
    default_target = "cpp" if original == "python" else "python"
    if original == "c" and args.mode == "auto":
        default_target = "zig"
    targets = list(dict.fromkeys(args.targets or [default_target]))
    if args.output and len(targets) != 1:
        parser.error("--output несовместим с несколькими языками назначения")
    outputs = [args.output or (args.input.with_stem(args.input.stem + "_copy") if target == original
                              else args.input.with_suffix(EXTENSIONS[target])) for target in targets]
    try:
        for output in outputs:
            if args.input.resolve() == output.resolve() or (output.exists() and args.input.samefile(output)):
                parser.error("исходный и выходной файлы должны различаться")
        translations = translate_outputs(args.input, original, targets, args.mode)
        for output, translation in zip(outputs, translations):
            with tempfile.TemporaryDirectory(prefix=".code-translator-", dir=output.parent) as temporary:
                staged = Path(temporary) / output.name
                if isinstance(translation.code, bytes):
                    staged.write_bytes(translation.code)
                else:
                    staged.write_text(translation.code, encoding="utf-8", newline="\n")
                staged.replace(output)
            if translation.fallback_reason:
                print(f"{args.input}:{translation.fallback_reason}. Выбран режим совместимости с CPython.", file=sys.stderr)
            print(output)
    except SyntaxError as error:
        print(f"{args.input}:{error.lineno}:{error.offset}: {error.msg}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        return error.returncode
    except (OSError, ValueError) as error:
        print(f"{args.input}: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
