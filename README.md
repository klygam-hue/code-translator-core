# Code Translator Core

Command-line source-to-source translator. It parses supported constructs into a shared typed representation and generates source code in the target language.

**Languages:** Python, JavaScript, TypeScript, C, C++, C#, Java, Go, Rust, Zig, Ruby, SQL (SQLite), and Assembly (NASM x86-64).

## Quick start

Requires Python 3.11 or later. The portable mode uses only the Python standard library. Running the generated code requires the target language's compiler or interpreter.

~~~sh
python translator.py --list
python translator.py portable_example.py --to javascript -o translated.js
node translated.js
python translator.py portable_example.py -o translated.ts
python translator.py portable_example.py --mode portable --to c --to ruby
~~~

The translator infers the source language from the file extension. Use `--from` to override it. Repeat `--to` to generate multiple outputs. With `-o` and a recognized extension, the extension selects the target unless `--to` is specified. If you supply neither option, Python input defaults to C++.

## Translation modes

| Mode | Behavior |
| --- | --- |
| `auto` | Selects an available generator. Python → C++ can generate code that uses CPython or embed the original script. C → Zig uses Zig when installed. |
| `portable` | Generates code from the shared typed representation. |
| `strict` | Requires Python → C++ to generate code that uses CPython directly. Other language pairs use the portable profile. |
| `compatible` | Embeds the original script for Python → C++. Running the result requires CPython and the original script's dependencies. |

To build C++ output that uses CPython, run `python build_cpp.py translated.cpp`. This requires the headers and libraries for the current CPython installation and a C++17 compiler. To use `zig translate-c`, install Zig and add it to `PATH`.

## Supported profile

The shared core handles individual files with scalar functions, variables, conditionals, loops, and console output. It supports `int32` integers, booleans, and strings. Capabilities vary by language pair. General collections, asynchronous code, floating-point arithmetic, and external libraries are outside the shared profile. Diagnostics include a source position when available.

- Python functions in the same file support keyword arguments and scalar defaults. Python integer lists support literals, aliases, `append`, returning a list, and printing to JavaScript, TypeScript, Ruby, Java, and C#. List indexing, slicing, and comprehensions are outside this profile.
- SQLite supports scalar `SELECT` expressions without database table references.
- NASM accepts straight-line arithmetic on 32-bit registers. To build the generated Linux x86-64 program, run `nasm -f elf64 translated.asm`, followed by `cc translated.o -o translated`.

## Tests

~~~sh
python -m unittest -v test_portable
~~~

The test suite covers code generation, profile limits, integer behavior, and a comparison of Python and JavaScript execution. The execution comparison runs when Node.js is available.

## License

[MIT](LICENSE).
