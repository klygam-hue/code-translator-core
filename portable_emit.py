"""Render the shared typed syntax tree as standalone source code."""

from __future__ import annotations

from contextlib import contextmanager
import json
import re
from textwrap import indent

import portable_ir as ir
from python_to_cpp import cpp_bytes


TYPES = {
    "python": {"int": "int", "bool": "bool", "str": "str", "void": "None"},
    "typescript": {"int": "number", "bool": "boolean", "str": "string", "void": "void"},
    "c": {"int": "int32_t", "bool": "bool", "str": "const char*", "void": "void"},
    "cpp": {"int": "int32_t", "bool": "bool", "str": "std::string", "void": "void"},
    "csharp": {"int": "int", "bool": "bool", "str": "string", "void": "void"},
    "java": {"int": "int", "bool": "boolean", "str": "String", "void": "void"},
    "go": {"int": "int32", "bool": "bool", "str": "string", "void": ""},
    "rust": {"int": "i32", "bool": "bool", "str": "&'static str", "void": "()"},
    "zig": {"int": "i32", "bool": "bool", "str": "[]const u8", "void": "void"},
}

CHECKED_INT = {
    "python": """\
def ct_i(value: int) -> int:
    if not -2147483648 <= value <= 2147483647:
        raise OverflowError("portable int32 overflow")
    return value
""",
    "javascript": """\
function ct_i(value) {
    if (!Number.isInteger(value) || value < -2147483648 || value > 2147483647) {
        throw new RangeError("portable int32 overflow");
    }
    return value === 0 ? 0 : value;
}
""",
    "typescript": """\
function ct_i(value: number): number {
    if (!Number.isInteger(value) || value < -2147483648 || value > 2147483647) {
        throw new RangeError("portable int32 overflow");
    }
    return value === 0 ? 0 : value;
}
""",
    "c": """\
static int32_t ct_i(int64_t value) {
    if (value < INT32_MIN || value > INT32_MAX) {
        fputs("portable int32 overflow\\n", stderr);
        exit(EXIT_FAILURE);
    }
    return (int32_t)value;
}
""",
    "java": """\
private static int ct_i(long value) {
    if (value < Integer.MIN_VALUE || value > Integer.MAX_VALUE) {
        throw new ArithmeticException("portable int32 overflow");
    }
    return (int)value;
}
""",
    "go": """\
func ct_i(value int64) int32 {
    if value < -2147483648 || value > 2147483647 {
        panic("portable int32 overflow")
    }
    return int32(value)
}
""",
}
CHECKED_INT["cpp"] = CHECKED_INT["c"]
CHECKED_INT["rust"] = """\
fn ct_i(value: i64) -> i32 {
    if value < i32::MIN as i64 || value > i32::MAX as i64 {
        panic!("portable int32 overflow");
    }
    value as i32
}
"""
CHECKED_INT["zig"] = """\
fn ct_i(value: i64) i32 {
    if (value < -2147483648 or value > 2147483647) @panic("portable int32 overflow");
    return @intCast(value);
}
"""
CHECKED_INT["csharp"] = CHECKED_INT["java"].replace("Integer.MIN_VALUE", "int.MinValue").replace(
    "Integer.MAX_VALUE", "int.MaxValue").replace("new ArithmeticException", "new System.ArithmeticException")

DIVISION = {
    "python": """\
def ct_div(a: int, b: int, floor: bool, remainder: bool) -> int:
    if floor:
        return ct_i(a % b if remainder else a // b)
    q = abs(a) // abs(b)
    if (a < 0) != (b < 0):
        q = -q
    return ct_i(a - q * b if remainder else q)
""",
    "javascript": """\
function ct_div(a, b, floor, remainder) {
    if (b === 0) throw new RangeError("portable division by zero");
    let q = Math.trunc(a / b);
    let r = a % b;
    if (floor && r !== 0 && ((r < 0) !== (b < 0))) {
        q -= 1;
        r += b;
    }
    return ct_i(remainder ? r : q);
}
""",
    "c": """\
static int32_t ct_div(int32_t a, int32_t b, bool floor, bool remainder) {
    if (b == 0) {
        fputs("portable division by zero\\n", stderr);
        exit(EXIT_FAILURE);
    }
    int64_t q = (int64_t)a / b;
    int64_t r = (int64_t)a % b;
    if (floor && r != 0 && ((r < 0) != (b < 0))) {
        --q;
        r += b;
    }
    return ct_i(remainder ? r : q);
}
""",
    "java": """\
private static int ct_div(int a, int b, boolean floor, boolean remainder) {
    long q = (long)a / b;
    long r = (long)a % b;
    if (floor && r != 0 && ((r < 0) != (b < 0))) {
        --q;
        r += b;
    }
    return ct_i(remainder ? r : q);
}
""",
    "go": """\
func ct_div(a, b int32, floor, remainder bool) int32 {
    q := int64(a) / int64(b)
    r := int64(a) % int64(b)
    if floor && r != 0 && ((r < 0) != (b < 0)) {
        q--
        r += int64(b)
    }
    if remainder {
        return ct_i(r)
    }
    return ct_i(q)
}
""",
}
DIVISION["cpp"] = DIVISION["c"]
DIVISION["rust"] = """\
fn ct_div(a: i32, b: i32, floor: bool, remainder: bool) -> i32 {
    let (a, b) = (i64::from(a), i64::from(b));
    let (mut q, mut r) = (a / b, a % b);
    if floor && r != 0 && ((r < 0) != (b < 0)) {
        q -= 1;
        r += b;
    }
    ct_i(if remainder { r } else { q })
}
"""
DIVISION["zig"] = """\
fn ct_div(a: i32, b: i32, floor: bool, remainder: bool) i32 {
    const wide_a: i64 = a;
    const wide_b: i64 = b;
    var q = @divTrunc(wide_a, wide_b);
    var r = @rem(wide_a, wide_b);
    if (floor and r != 0 and ((r < 0) != (b < 0))) {
        q -= 1;
        r += wide_b;
    }
    return ct_i(if (remainder) r else q);
}
"""
DIVISION["csharp"] = DIVISION["java"].replace("boolean", "bool")
DIVISION["typescript"] = DIVISION["javascript"].replace(
    "ct_div(a, b, floor, remainder)", "ct_div(a: number, b: number, floor: boolean, remainder: boolean): number", 1)


CHECKED_INT["ruby"] = """\
def ct_i(value)
    raise RangeError, "portable int32 overflow" unless (-2147483648..2147483647).cover?(value)
    value
end
"""

DIVISION["ruby"] = """\
def ct_div(a, b, floor, remainder)
    r = floor ? a % b : a.remainder(b)
    ct_i(remainder ? r : (a - r) / b)
end
"""


def safe_name(name: str) -> str:
    parts = [char if char.isascii() and (char.isalnum() or char == "_") else f"_u{ord(char):x}_" for char in name]
    return "".join(parts)


def unique_names(names: list[str], prefix: str) -> list[str]:
    allocated = set()
    result = []
    for name in names:
        base = re.sub("_+", "_", prefix + safe_name(name))
        candidate = base
        index = 1
        while candidate in allocated:
            index += 1
            candidate = f"{base}_{index}"
        result.append(candidate)
        allocated.add(candidate)
    return result


class Emitter:
    def __init__(self, analysis: ir.Analysis, target: str):
        self.analysis = analysis
        self.program = analysis.program
        self.target = target
        self.python = target == "python"
        self.ruby = target == "ruby"
        self.go = target == "go"
        self.rust = target == "rust"
        self.zig = target == "zig"
        self.c_family = target in {"c", "cpp"}
        self.class_language = target in {"java", "csharp"}
        self.script = target in {"javascript", "typescript"}
        self.semicolon = "" if self.python or self.ruby or self.go else ";"
        self.level = int(self.class_language)
        self.lines: list[str] = []
        self.variables: dict[ir.Variable, str] = {}
        self.functions = dict(zip(analysis.functions, unique_names(list(analysis.functions), "ct_f_")))
        self.needs: set[str] = set()
        self.loop_updates: list[tuple[ir.Stmt, ...]] = []

    def line(self, text: str = "") -> None:
        self.lines.append("    " * self.level + text if text else "")

    def statement(self, code: str) -> None:
        self.line(code + self.semicolon)

    @contextmanager
    def block(self, header: str):
        self.line(header + (":" if self.python else "" if self.ruby else " {"))
        self.level += 1
        start = len(self.lines)
        yield
        if self.python and len(self.lines) == start:
            self.line("pass")
        self.level -= 1
        if not self.python:
            self.line("end" if self.ruby else "}")

    def kind(self, node: ir.Expr) -> ir.Type:
        return self.analysis.expressions[id(node)].kind()

    def type_name(self, kind: ir.Type) -> str:
        return TYPES[self.target][kind]

    def literal(self, value: int | bool | str) -> str:
        if isinstance(value, bool):
            return str(value) if self.python else str(value).lower()
        if isinstance(value, int):
            return str(value)
        if self.python:
            return repr(value)
        if self.ruby:
            return json.dumps(value, ensure_ascii=False).replace("#", "\\#")
        if self.c_family:
            return cpp_bytes(value.encode("utf-8"))
        if self.rust or self.zig:
            escaped = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
            return '"' + "".join(escaped.get(ch, f"\\x{ord(ch):02x}" if ord(ch) < 32 or ord(ch) == 127 else ch)
                                 for ch in value) + '"'
        return json.dumps(value, ensure_ascii=False)

    def widen(self, code: str) -> str:
        if self.c_family:
            return f"(int64_t)({code})"
        if self.go:
            return f"int64({code})"
        if self.rust:
            return f"i64::from({code})"
        if self.zig:
            return f"@as(i64, {code})"
        if self.class_language:
            return f"(long)({code})"
        return f"({code})"

    def truth(self, node: ir.Expr) -> str:
        code = self.expression(node)
        kind = self.kind(node)
        if kind == "int":
            return f"({code} != 0)"
        if kind == "str":
            if self.target == "c":
                return f"(({code})[0] != 0)"
            if self.target == "java":
                return f"(!({code}).isEmpty())"
            if self.rust:
                return f"(!({code}).is_empty())"
            if self.zig:
                return f"(({code}).len != 0)"
            return f'({code} != "")'
        return code

    def expression(self, node: ir.Expr) -> str:
        match node:
            case ir.Literal(value=value):
                return self.literal(value)
            case ir.Name():
                return self.variables[self.analysis.references[id(node)]]
            case ir.Call(name=name, arguments=arguments):
                return f"{self.functions[name]}({', '.join(self.expression(arg) for arg in arguments)})"
            case ir.Choice(condition=condition, yes=when_true, no=when_false):
                test, yes, no = self.truth(condition), self.expression(when_true), self.expression(when_false)
                if self.python:
                    return f"({yes} if {test} else {no})"
                if self.go:
                    result = self.type_name(self.kind(node))
                    return f"(func() {result} {{ if {test} {{ return {yes} }}; return {no} }}())"
                if self.rust:
                    return f"(if {test} {{ {yes} }} else {{ {no} }})"
                if self.zig:
                    result = self.type_name(self.kind(node))
                    return f"(@as({result}, if ({test}) {yes} else {no}))"
                return f"({test} ? {yes} : {no})"
            case ir.Unary(operator="!", operand=operand):
                operator = "not " if self.python else "!"
                return f"({operator}{self.truth(operand)})"
            case ir.Unary(operator="+", operand=operand):
                return self.expression(operand)
            case ir.Unary(operator="-", operand=operand):
                self.needs.add("int")
                return f"ct_i(-{self.widen(self.expression(operand))})"
            case ir.Binary(operator=op, left=left_node, right=right_node):
                left, right = self.expression(left_node), self.expression(right_node)
                if op in {"+", "-", "*"}:
                    self.needs.add("int")
                    rhs = self.widen(right) if self.go or self.rust or self.zig else f"({right})"
                    return f"ct_i({self.widen(left)} {op} {rhs})"
                if op in {"tdiv", "tmod", "fdiv", "fmod"}:
                    self.needs.update(("int", "division"))
                    return f"ct_div({left}, {right}, {self.literal(op.startswith('f'))}, {self.literal(op.endswith('mod'))})"
                if op in {"==", "!="} and self.kind(left_node) == "str":
                    if self.target == "c":
                        self.needs.add("strcmp")
                        return f"(strcmp({left}, {right}) {op} 0)"
                    if self.target == "java":
                        prefix = "!" if op == "!=" else ""
                        return f"({prefix}({left}).equals({right}))"
                    if self.target == "cpp":
                        return f"(std::string({left}) {op} {right})"
                    if self.zig:
                        self.needs.add("zig_std")
                        return f"({'!' if op == '!=' else ''}std.mem.eql(u8, {left}, {right}))"
                if self.script and op in {"==", "!="}:
                    op += "="
                elif self.python and op in {"&&", "||"}:
                    op = "and" if op == "&&" else "or"
                elif self.zig and op in {"&&", "||"}:
                    op = "and" if op == "&&" else "or"
                return f"({left} {op} {right})"
            case _:
                raise AssertionError(node)

    def output_value(self, node: ir.Expr, style: str) -> tuple[str, ir.Type]:
        value, kind = self.expression(node), self.kind(node)
        if kind != "bool":
            return value, kind
        words = {"upper": ("True", "False"), "lower": ("true", "false"), "numeric": ("1", "0")}
        yes, no = (self.literal(text) for text in words[style])
        if self.python:
            result = f"({yes} if {value} else {no})"
        elif self.go:
            self.needs.add("bool_text")
            result = f"ct_bool({value}, {yes}, {no})"
        elif self.rust:
            result = f"(if {value} {{ {yes} }} else {{ {no} }})"
        elif self.zig:
            result = f"(@as([]const u8, if ({value}) {yes} else {no}))"
        else:
            result = f"(({value}) ? {yes} : {no})"
        return result, "str"

    def output(self, node: ir.Print) -> None:
        values = [self.output_value(value, node.boolean_style) for value in node.values]
        if node.stderr:
            self.needs.add("stderr")
        if self.python:
            arguments = [value for value, _ in values]
            arguments.extend((f"sep={self.literal(node.separator)}", f"end={self.literal(node.ending)}"))
            if node.stderr:
                arguments.append("file=sys.stderr")
            self.statement(f"print({', '.join(arguments)})")
            return
        pieces = []
        for index, value in enumerate(values):
            if index:
                pieces.append((self.literal(node.separator), "str"))
            pieces.append(value)
        if node.ending and (not self.script or node.ending != "\n"):
            pieces.append((self.literal(node.ending), "str"))
        if self.ruby:
            stream = "$stderr" if node.stderr else "$stdout"
            self.statement(f"{stream}.print({', '.join(value for value, _ in pieces)})")
        elif self.target == "c":
            self.needs.add("print")
            form = "".join("%lld" if kind == "int" else "%s" for _, kind in pieces)
            args = [self.literal(form), *(f"(long long)({value})" if kind == "int" else value for value, kind in pieces)]
            if node.stderr:
                self.statement(f"fprintf(stderr, {', '.join(args)})")
            else:
                self.statement(f"printf({', '.join(args)})")
        elif self.target == "cpp":
            if pieces:
                self.needs.add("print")
                values = " << ".join(f"({value})" for value, _ in pieces)
                stream = "std::cerr" if node.stderr else "std::cout"
                self.statement(f"{stream} << {values}")
        elif self.go:
            self.needs.add("print")
            args = [value for value, _ in pieces]
            if node.stderr:
                self.statement(f"fmt.Fprint({', '.join(['os.Stderr', *args])})")
            else:
                self.statement(f"fmt.Print({', '.join(args)})")
        elif self.rust:
            form = self.literal("{}" * len(pieces))
            args = ", ".join([form, *(value for value, _ in pieces)])
            self.statement(f"{'eprint' if node.stderr else 'print'}!({args})")
        elif self.zig:
            self.zig_output(pieces, node.stderr)
        elif self.class_language:
            arguments = " + ".join(['""', *(f"({value})" for value, _ in pieces)])
            if self.target == "java":
                output = "System.err.print" if node.stderr else "System.out.print"
            else:
                output = "System.Console.Error.Write" if node.stderr else "System.Console.Write"
            self.statement(f"{output}({arguments})")
        else:
            contents = " + ".join(['""', *(f"String({value})" for value, _ in pieces)])
            if node.ending == "\n":
                self.statement(f"console.{'error' if node.stderr else 'log'}({contents})")
            else:
                self.statement(f"{self.node_process('stderr' if node.stderr else 'stdout')}.write({contents})")

    def zig_output(self, pieces: list[tuple[str, ir.Type]], stderr: bool) -> None:
        if stderr:
            self.needs.add("zig_std")
            form = self.literal("".join("{d}" if kind == "int" else "{s}" for _, kind in pieces))
            args = ", ".join(value for value, _ in pieces)
            self.statement(f"std.debug.print({form}, .{{{args}}})")
            return
        self.needs.add("zig_print")
        form = self.literal("".join("%d" if kind == "int" else "%.*s" for _, kind in pieces))
        args = [form]
        for value, kind in pieces:
            if kind == "int":
                args.append(f"@as(c_int, {value})")
            else:
                string = f"@as([]const u8, {value})"
                args.extend((f"@as(c_int, @intCast({string}.len))", f"{string}.ptr"))
        self.statement(f"_ = printf({', '.join(args)})")

    def node_process(self, attribute: str) -> str:
        if self.target == "javascript":
            return f"process.{attribute}"
        return f"(globalThis as unknown as {{ process: {{ {attribute}: " + {
            "stdout": "{ write(value: string): unknown }",
            "stderr": "{ write(value: string): unknown }",
            "exitCode": "number",
        }[attribute] + f" }} }}).process.{attribute}"

    def condition_header(self, keyword: str, expression: str) -> str:
        if self.python or self.go or self.rust or (expression.startswith("(") and expression.endswith(")")):
            return f"{keyword} {expression}"
        return f"{keyword} ({expression})"

    def body(self, statements: tuple[ir.Stmt, ...]) -> None:
        for node in statements:
            match node:
                case ir.Bind(value=value):
                    variable = self.variables[self.analysis.references[id(node)]]
                    self.statement(f"{variable} = {self.expression(value)}")
                case ir.If(condition=test, body=body, otherwise=other):
                    with self.block(self.condition_header("if", self.truth(test))):
                        self.body(body)
                    if other:
                        if self.go or self.ruby:
                            self.lines[-1] = "    " * self.level + "else" if self.ruby else self.lines[-1] + " else {"
                            self.level += 1
                            self.body(other)
                            self.level -= 1
                            self.line("end" if self.ruby else "}")
                        else:
                            with self.block("else"):
                                self.body(other)
                case ir.Loop(condition=test, initial=initial, update=update, body=body):
                    self.body(initial)
                    self.loop_updates.append(update)
                    condition = self.truth(test)
                    if self.target == "java" and isinstance(test, ir.Literal):
                        condition = f"Boolean.valueOf({condition}).booleanValue()"
                    with self.block(self.condition_header("for" if self.go else "while", condition)):
                        self.body(body)
                        if id(node) in self.analysis.loop_fallthrough:
                            self.body(update)
                    self.loop_updates.pop()
                case ir.Return(value=value):
                    suffix = "" if value is None else " " + self.expression(value)
                    self.statement("return" + suffix)
                case ir.Print():
                    self.output(node)
                case ir.Invoke(call=call):
                    prefix = "_ = " if self.zig and self.kind(call) != "void" else ""
                    self.statement(prefix + self.expression(call))
                case ir.Jump(command=command):
                    if command == "continue":
                        self.body(self.loop_updates[-1])
                    self.statement("next" if self.ruby and command == "continue" else command)
                case _:
                    raise AssertionError(node)

    def local_names(self, signature: ir.Signature) -> None:
        variables = (*signature.parameters, *signature.variables)
        names = unique_names([variable.name for variable in variables], "v_")
        self.variables = dict(zip(variables, names))

    def declaration(self, variable: ir.Variable) -> None:
        kind, name = variable.slot.kind(), self.variables[variable]
        if self.python or self.ruby:
            return
        if self.target == "javascript":
            self.statement(f"let {name}")
        elif self.target == "typescript":
            self.statement(f"let {name}: {self.type_name(kind)}")
        elif self.go:
            self.statement(f"var {name} {self.type_name(kind)}")
            self.statement(f"_ = {name}")
        elif self.rust:
            self.statement(f"let mut {name}: {self.type_name(kind)}")
        elif self.zig:
            self.statement(f"var {name}: {self.type_name(kind)} = undefined")
            self.statement(f"_ = &{name}")
        else:
            self.statement(f"{self.type_name(kind)} {name}")

    def function_header(self, name: str, signature: ir.Signature) -> str:
        parameters = []
        for variable in signature.parameters:
            target_name = self.variables[variable]
            if self.target == "javascript" or self.ruby:
                parameters.append(target_name)
                continue
            kind = self.type_name(variable.slot.kind())
            if self.python or self.target == "typescript":
                parameters.append(f"{target_name}: {kind}")
            elif self.rust:
                parameters.append(f"mut {target_name}: {kind}")
            elif self.zig:
                parameters.append(f"arg_{target_name}: {kind}")
            elif self.go:
                parameters.append(f"{target_name} {kind}")
            else:
                parameters.append(f"{kind} {target_name}")
        joined = ", ".join(parameters) or ("void" if self.target == "c" else "")
        if self.target == "javascript":
            return f"function {name}({joined})"
        if self.ruby:
            return f"def {name}({joined})"
        returns = self.type_name(signature.result.kind())
        if self.python:
            return f"def {name}({joined}) -> {returns}"
        if self.target == "typescript":
            return f"function {name}({joined}): {returns}"
        if self.go:
            result = f" {returns}" if returns else ""
            return f"func {name}({joined}){result}"
        if self.rust:
            return f"fn {name}({joined}) -> {returns}"
        if self.zig:
            return f"fn {name}({joined}) {returns}"
        return f"{'static ' if self.class_language else ''}{returns} {name}({joined})"

    def function(self, node: ir.Function) -> None:
        signature = self.analysis.functions[node.name]
        self.local_names(signature)
        with self.block(self.function_header(self.functions[node.name], signature)):
            if self.zig:
                for parameter in signature.parameters:
                    name = self.variables[parameter]
                    self.statement(f"var {name} = arg_{name}")
                    self.statement(f"_ = &{name}")
            for variable in signature.variables:
                self.declaration(variable)
            self.body(node.body)
            if self.analysis.implicit_main_return and self.program.main == node.name:
                self.statement("return 0")
        self.line()

    def entry_body(self) -> None:
        signature = self.analysis.entry
        self.local_names(signature)
        for variable in signature.variables:
            self.declaration(variable)
        self.body(self.program.body)
        main = self.program.main
        if main is not None:
            call = f"{self.functions[main]}()"
            returns = self.analysis.functions[main].result.kind() == "int"
            if returns:
                if self.python:
                    self.statement(f"raise SystemExit({call})")
                elif self.go:
                    self.needs.add("exit")
                    self.statement(f"os.Exit(int({call}))")
                elif self.target == "java":
                    self.statement(f"System.exit({call})")
                elif self.target == "csharp":
                    self.statement(f"System.Environment.Exit({call})")
                elif self.script:
                    self.statement(f"{self.node_process('exitCode')} = {call}")
                elif self.ruby:
                    self.statement(f"exit({call})")
                elif self.rust:
                    self.statement(f"let ct_status = {call}")
                    self.statement("let _ = std::io::Write::flush(&mut std::io::stdout())")
                    self.statement("std::process::exit(ct_status)")
                elif self.zig:
                    self.needs.add("zig_exit")
                    self.statement(f"exit({call})")
                else:
                    self.statement(f"return {call}")
                return
            self.statement(call)
        if self.c_family:
            self.statement("return 0")

    def entry(self) -> None:
        if not (self.program.body or self.program.main or self.c_family or self.go or self.rust or self.zig):
            return
        if self.script or self.ruby:
            self.entry_body()
            return
        headers = {
            "python": 'if __name__ == "__main__"', "c": "int main(void)", "cpp": "int main()",
            "csharp": "public static void Main(string[] args)", "java": "public static void main(String[] args)", "go": "func main()",
            "rust": "fn main()", "zig": "pub fn main() void",
        }
        with self.block(headers[self.target]):
            self.entry_body()

    def prelude(self) -> list[str]:
        if self.python and "stderr" in self.needs:
            return ["import sys", ""]
        if self.zig:
            lines = []
            if "zig_std" in self.needs:
                lines.append('const std = @import("std");')
            if "zig_print" in self.needs:
                lines.append('extern "c" fn printf(format: [*:0]const u8, ...) c_int;')
            if "zig_exit" in self.needs:
                lines.append('extern "c" fn exit(status: c_int) noreturn;')
            return lines + ([""] if lines else [])
        if self.c_family:
            includes = ["<stdint.h>"]
            if self.target == "c":
                includes.append("<stdbool.h>")
            else:
                includes.append("<string>")
            if "int" in self.needs:
                includes.extend(("<stdio.h>", "<stdlib.h>"))
            elif self.target == "c" and "print" in self.needs:
                includes.append("<stdio.h>")
            if "strcmp" in self.needs:
                includes.append("<string.h>")
            if self.target == "cpp" and "print" in self.needs:
                includes.append("<iostream>")
            return [*(f"#include {header}" for header in includes), ""]
        if self.go:
            imports = [module for purposes, module in (({"print"}, "fmt"), ({"exit", "stderr"}, "os"))
                       if purposes & self.needs]
            return ["package main", "", *(f'import "{module}"' for module in imports), ""]
        if self.target == "java":
            return ["class Main {"]
        if self.target == "csharp":
            return ["class Program {"]
        return []

    def render(self) -> str:
        if self.c_family:
            for function in self.program.functions:
                signature = self.analysis.functions[function.name]
                self.local_names(signature)
                self.statement(self.function_header(self.functions[function.name], signature))
            if self.program.functions:
                self.line()
        for function in self.program.functions:
            self.function(function)
        self.entry()
        if self.class_language:
            self.line()
            self.lines.append("}")
        helpers = []
        if "int" in self.needs:
            helpers.append(CHECKED_INT[self.target])
        if "division" in self.needs:
            helpers.append(DIVISION[self.target])
        if "bool_text" in self.needs:
            helpers.append("func ct_bool(value bool, yes, no string) string {\n    if value {\n        return yes\n    }\n    return no\n}\n")
        if self.class_language:
            helpers = [indent(helper, "    ") for helper in helpers]
        comment = "#" if self.python or self.ruby else "//"
        header = f"{comment} Generated by Code Translator from {self.program.source} to {self.target} (portable int32 profile)."
        return "\n".join([header, *self.prelude(), *helpers, *self.lines]).rstrip() + "\n"


def render(program: ir.Program, target: str) -> str:
    return render_analysis(ir.Analysis(program).run(), target)


def render_analysis(analysis: ir.Analysis, target: str) -> str:
    if target in {"sql", "assembly"}:
        from scalar_languages import render_sql, render_assembly
        return (render_sql if target == "sql" else render_assembly)(analysis)
    return Emitter(analysis, target).render()
