"""Regression checks for target-independent semantics and rejection paths."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import portable_emit
import portable_parse
import translator


class PortableSemantics(unittest.TestCase):
    def test_python_list_length_and_negative_index_translate(self):
        code = "numbers = [6, 7, 42]\nprint(len(numbers), numbers[-1], numbers[0])\n"
        self.assertEqual(self.run_in_python("python", code), ("3 42 6\n", 0))
        source = self.make_source("python", code)
        result, = translator.translate_outputs(source, "python", ["javascript"], "auto")
        self.assertFalse(result.requires_python)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for this execution comparison")
    def test_python_compatibility_preserves_arguments_streams_and_exit_status(self):
        code = (
            "import sys\n"
            "from helper import multiply\n"
            "values = [multiply(n) for n in range(3)]\n"
            "print(input(), *values, sys.argv[1], sep='|')\n"
            "print('diagnostic', file=sys.stderr)\n"
            "sys.exit(7)\n"
        )
        source = self.make_source("python", code)
        source.with_name("helper.py").write_text("def multiply(n): return n * 7\n", encoding="utf-8")
        result, = translator.translate_outputs(source, "python", ["javascript"], "auto")
        self.assertTrue(result.requires_python)
        self.assertIsNotNone(result.fallback_reason)
        target = source.with_suffix(".mjs")
        target.write_text(result.code, encoding="utf-8")
        env = dict(os.environ, CODE_TRANSLATOR_PYTHON=sys.executable, PYTHONIOENCODING="utf-8")
        original = subprocess.run([sys.executable, source, "quoted arg"], input="input\n", text=True,
                                  capture_output=True, timeout=10, env=env)
        translated = subprocess.run(["node", target, "quoted arg"], input="input\n", text=True,
                                    capture_output=True, timeout=10, env=env)
        self.assertEqual((translated.stdout, translated.stderr, translated.returncode),
                         (original.stdout, original.stderr, original.returncode))

    def test_compatibility_does_not_emit_an_invalid_python_script(self):
        source = self.make_source("python", "def missing(:\n")
        with self.assertRaises(SyntaxError):
            translator.translate_outputs(source, "python", ["javascript"], "auto")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for this execution comparison")
    def test_python_lists_and_keyword_arguments_translate_to_javascript(self):
        source = self.make_source("python", (
            'def squares(limit=6, start=0) -> list[int]:\n'
            '    values = []\n    alias = values\n'
            '    for n in range(start, limit):\n'
            '        if n % 2 == 0:\n            alias.append(n ** 2)\n'
            '    return values\n'
            'print(f"values: {squares(start=1)}")\n'
            'print(squares(start=2, limit=5))\n'
        ))
        target = source.with_suffix(".js")
        with redirect_stdout(StringIO()):
            self.assertEqual(translator.main([str(source), "--output", str(target)]), 0)
        expected = subprocess.run([sys.executable, source], capture_output=True, text=True, check=True, timeout=10)
        actual = subprocess.run(["node", target], capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual((actual.stdout, actual.stderr), (expected.stdout, expected.stderr))

    def test_python_formats_and_parallel_assignment_preserve_output(self):
        source = ('a = b = 1\na, b = a + 1, b + 2\na, b = b, a\n'
                  'print(f"values: {a!s}, {b}", True, sep=" | ", end="!")\n')
        self.assertEqual(self.run_in_python("python", source), ("values: 3, 2 | True!", 0))

    def test_conditional_short_circuits_in_both_input_languages(self):
        for language, text in [
            ("python", 'print(42 if "text" else 1 // 0)'),
            ("javascript", 'console.log("" ? 0 : (true ? 42 : Math.trunc(1 / 0)));'),
        ]:
            with self.subTest(language=language):
                self.assertEqual(self.run_in_python(language, text), ("42\n", 0))

    def test_console_log_formats_string_and_integer(self):
        source = 'let label = "answer"; console.log("%s: %d (%%)", label, 42);'
        self.assertEqual(self.run_in_python("javascript", source), ("answer: 42 (%)\n", 0))

    def test_ruby_function_and_floor_division(self):
        source = 'def calculate(n)\n  n / 3\nend\nputs calculate(-7)\n'
        self.assertEqual(self.run_in_python("ruby", source), ("-3\n", 0))

    def test_sql_literal_and_generated_query(self):
        self.assertEqual(self.run_in_python("sql", "SELECT 'it''s 42' AS result;"), ("it's 42\n", 0))
        source = self.make_source("python", "n = -7\nn %= 3\nprint(n)\n")
        sql = portable_emit.render(portable_parse.parse(source, "python"), "sql")
        with sqlite3.connect(":memory:") as database:
            self.assertEqual(database.execute(sql).fetchone(), (2,))

    def test_nasm_register_operations_and_explicit_limit(self):
        source = "bits 64\nmov eax, 6\nmov ecx, 7\nimul eax, ecx\n"
        self.assertEqual(self.run_in_python("assembly", source), ("42\n", 0))
        unsupported = self.make_source("assembly", "mov eax, [rbp - 4]")
        with self.assertRaisesRegex(ValueError, "NASM register or integer"):
            portable_parse.parse(unsupported, "assembly")

    def test_nasm_wraps_32_bit_register_arithmetic(self):
        source = "mov eax, 0x7fffffff\ninc eax\nneg eax\n"
        self.assertEqual(self.run_in_python("assembly", source), ("-2147483648\n", 0))

    def test_known_javascript_signed_zero_is_rejected(self):
        for expression in ("Math.trunc(-1 / 2)", "-2 % 2", "0 * -2", "Math.floor(0 / -2)"):
            with self.subTest(expression=expression):
                source = self.make_source("javascript", f"console.log({expression});")
                with self.assertRaisesRegex(ValueError, "negative zero"):
                    portable_parse.parse(source, "javascript")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for this execution comparison")
    def test_translated_range_matches_execution_at_int32_edges(self):
        code = (
            "for n in range(2147483646, 2147483647, 2):\n    print(n)\n    continue\n"
            "for n in range(-2147483647, -2147483648, -2):\n    print(n)\n"
        )
        source = self.make_source("python", code)
        target = source.with_suffix(".js")
        translated, = translator.translate_outputs(source, "python", ["javascript"], "portable")
        target.write_text(translated.code, encoding="utf-8")
        original_process = subprocess.run([sys.executable, source], capture_output=True, text=True, timeout=10)
        generated_process = subprocess.run(["node", target], capture_output=True, text=True, timeout=10)
        self.assertEqual(original_process.returncode, 0)
        self.assertEqual(
            (generated_process.returncode, generated_process.stdout, generated_process.stderr),
            (original_process.returncode, original_process.stdout, original_process.stderr),
        )

    def test_python_range_steps_preserve_continue_and_loop_variable(self):
        for start, stop, step in [(1, 8, 2), (7, 0, -2), (2, 2, 3), (1, 7, -1)]:
            with self.subTest(step=step, start=start, stop=stop):
                text = (f'total = 0\ni = 99\nfor i in range({start}, {stop}, {step}):\n'
                        '    if i == 3:\n        continue\n    total += i\nprint(total, i)\n')
                expected = StringIO()
                with redirect_stdout(expected):
                    exec(text)
                self.assertEqual(self.run_in_python("python", text), (expected.getvalue(), 0))
                source = self.make_source("python", text)
                targets = [language for language in portable_parse.EXTENSIONS if language not in {"sql", "assembly"}]
                outputs = translator.translate_outputs(source, "python", targets, "portable")
                self.assertTrue(all(output.code for output in outputs))

    def test_python_range_zero_step_is_reported(self):
        source = self.make_source("python", "for i in range(0, 10, 0):\n    print(i)\n")
        with self.assertRaisesRegex(ValueError, "range step cannot be zero"):
            portable_parse.parse(source, "python")

    def make_source(self, language: str, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source = Path(directory.name) / ("source" + portable_parse.EXTENSIONS[language])
        source.write_text(text, encoding="utf-8")
        return source

    def run_in_python(self, language: str, text: str) -> tuple[str, int]:
        source = self.make_source(language, text)
        result = portable_emit.render(portable_parse.parse(source, language), "python")
        output = StringIO()
        exit_code = 0
        with redirect_stdout(output):
            try:
                exec(compile(result, "<generated test>", "exec"), {"__name__": "__main__"})
            except SystemExit as error:
                exit_code = error.code
        return output.getvalue(), exit_code

    def test_signed_division_print_and_exit_on_c_input(self):
        text = '#include <stdio.h>\nint main(void) { printf("%d %d\\n", -7 / 3, -7 % 3); return 7; }'
        self.assertEqual(self.run_in_python("c", text), ("-2 -1\n", 7))

    def test_for_continue_updates_counter_before_retesting(self):
        text = """\
let total = 0;
for (let i = 1; i < 6; ++i) {
    if (i === 3) continue;
    total += i;
}
console.log(total);
"""
        self.assertEqual(self.run_in_python("javascript", text), ("12\n", 0))

    def test_javascript_temporal_dead_zone_is_reported(self):
        text = """\
let value = 1;
if (true) {
    console.log(value);
    let value = 2;
}
"""
        source = self.make_source("javascript", text)
        with self.assertRaisesRegex(ValueError, r"3:17: variable value is not defined"):
            portable_emit.render(portable_parse.parse(source, "javascript"), "python")

    def test_unsupported_mode_preserves_other_requested_output(self):
        source = self.make_source("python", "print(1)\n")
        existing = source.with_suffix(".cpp")
        existing.write_text("previous", encoding="utf-8")
        with redirect_stderr(StringIO()):
            status = translator.main([str(source), "--mode", "compatible", "--to", "cpp", "--to", "go"])
        self.assertEqual(status, 1)
        self.assertEqual(existing.read_text(encoding="utf-8"), "previous")

    def test_same_language_copies_original_bytes(self):
        source = self.make_source("python", "")
        original = b"# coding: cp1251\r\n# \xf2\xe5\xea\xf1\xf2\r\n"
        source.write_bytes(original)
        with redirect_stdout(StringIO()):
            status = translator.main([str(source), "--to", "python"])
        self.assertEqual(status, 0)
        self.assertEqual(source.with_stem("source_copy").read_bytes(), original)

    def test_rust_tail_result_and_output(self):
        text = 'fn twice(n: i32) -> i32 { n * 2 }\nfn main() { println!("{}", twice(21)); }'
        self.assertEqual(self.run_in_python("rust", text), ("42\n", 0))

    def test_zig_debug_output_and_floor_remainder(self):
        text = '''\
const std = @import("std");
pub fn main() void { std.debug.print("{d}\\n", .{@mod(-7, 3)}); }
'''
        source = self.make_source("zig", text)
        result = portable_emit.render(portable_parse.parse(source, "zig"), "python")
        stderr = StringIO()
        with redirect_stderr(stderr):
            exec(compile(result, "<generated test>", "exec"), {"__name__": "__main__"})
        self.assertEqual(stderr.getvalue(), "2\n")


if __name__ == "__main__":
    unittest.main()
