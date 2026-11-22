"""Translate a Python AST to C++17 with CPython object semantics."""

from __future__ import annotations

import ast
from contextlib import contextmanager
import math
from pathlib import Path
import symtable

from translation import Translation


class UnsupportedSyntax(ValueError):
    def __init__(self, node: ast.AST, detail: str | None = None):
        line = getattr(node, "lineno", 1)
        column = getattr(node, "col_offset", 0) + 1
        description = detail or type(node).__name__
        super().__init__(f"{line}:{column}: {description}")


class Writer:
    def __init__(self):
        self.lines: list[str] = []
        self.indent = 0

    def line(self, text: str = "") -> None:
        self.lines.append("    " * self.indent + text if text else "")

    @contextmanager
    def block(self, header: str = "", end: str = "}"):
        self.line(f"{header} {{" if header else "{")
        self.indent += 1
        try:
            yield
        finally:
            self.indent -= 1
            self.line(end)

    def render(self) -> str:
        return "\n".join(self.lines)


def cpp_bytes(data: bytes) -> str:
    escaped = []
    for byte in data:
        if byte in (34, 92):
            escaped.append("\\" + chr(byte))
        elif 32 <= byte < 127:
            escaped.append(chr(byte))
        else:
            escaped.append(f"\\{byte:03o}")
    return '"' + "".join(escaped) + '"'


def cpp_string(text: str) -> str:
    return cpp_bytes(text.encode("utf-8", errors="surrogatepass"))


def unicode_value(text: str) -> str:
    data = text.encode("utf-8", errors="surrogatepass")
    return f"py::unicode({cpp_bytes(data)}, {len(data)})"


NUMBER_OPERATORS = {
    ast.Add: "Add", ast.Sub: "Subtract", ast.Mult: "Multiply", ast.MatMult: "MatrixMultiply",
    ast.Div: "TrueDivide", ast.FloorDiv: "FloorDivide", ast.Mod: "Remainder", ast.Pow: "Power",
    ast.LShift: "Lshift", ast.RShift: "Rshift", ast.BitOr: "Or", ast.BitXor: "Xor", ast.BitAnd: "And",
}
UNARY_OPERATORS = {ast.UAdd: "Positive", ast.USub: "Negative", ast.Invert: "Invert"}
COMPARISONS = {ast.Lt: "LT", ast.LtE: "LE", ast.Gt: "GT", ast.GtE: "GE", ast.Eq: "EQ", ast.NotEq: "NE"}
FRAME_BUILTINS = {
    "breakpoint", "compile", "dir", "eval", "exec", "globals", "locals", "super", "vars", "__import__",
}
DYNAMIC_ATTRIBUTES = {"__class__", "__closure__", "__code__", "__globals__", "__qualname__", "__self__"}


class FrameEmitter:
    def __init__(self, module: CppEmitter, writer: Writer, locals_: tuple[str, ...] | None = None):
        self.module = module
        self.out = writer
        self.locals = None if locals_ is None else {
            name: f"local_{index}" for index, name in enumerate(locals_)
        }
        self.loop_flags: list[str | None] = []
        self.temporary = 0

    def fresh(self, prefix: str = "value") -> str:
        name = f"{prefix}_{self.temporary}"
        self.temporary += 1
        return name

    def bind(self, expression: str, prefix: str = "value") -> str:
        name = self.fresh(prefix)
        self.out.line(f"auto {name} = {expression};")
        return name

    def checked(self, function: str, *arguments: str) -> str:
        return self.bind(f"py::checked({function}({', '.join(arguments)}))")

    def body(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            self.statement(statement)

    def load_name(self, node: ast.Name) -> str:
        name = cpp_string(node.id)
        if self.locals is not None and node.id in self.locals:
            return self.bind(f"py::load_local({self.locals[node.id]}, {name})")
        if node.id in FRAME_BUILTINS:
            raise UnsupportedSyntax(node, f"{node.id}() requires a Python execution frame")
        if node.id == "type":
            raise UnsupportedSyntax(node, "type() may inspect or bind Python function objects")
        return self.bind(f"py::load_global(globals, {name})")

    def store(self, target: ast.expr, value: str) -> None:
        match target:
            case ast.Name(id=name) if self.locals is not None:
                self.out.line(f"{self.locals[name]} = {value};")
            case ast.Name(id=name):
                self.out.line(f"py::store_global(globals, {cpp_string(name)}, {value});")
            case ast.Subscript(value=owner, slice=index):
                container = self.expression(owner)
                key = self.expression(index)
                self.out.line(f"py::check(PyObject_SetItem({container}.get(), {key}.get(), {value}.get()));")
            case ast.Attribute(value=owner, attr=name):
                instance = self.expression(owner)
                self.out.line(f"py::check(PyObject_SetAttrString({instance}.get(), {cpp_string(name)}, {value}.get()));")
            case _:
                raise UnsupportedSyntax(target, "assignment target requires a Python execution frame")

    def evaluate(self, node: ast.expr, prefix: str, statement: str) -> str:
        result = self.fresh(prefix)
        with self.out.block(f"auto {result} = [&]()", "}();"):
            value = self.expression(node)
            self.out.line(statement.format(value=value))
        return result

    def condition(self, node: ast.expr) -> str:
        if isinstance(node, ast.BoolOp):
            result = self.fresh("condition")
            with self.out.block(f"auto {result} = [&]()", "}();"):
                for operand in node.values[:-1]:
                    value = self.condition(operand)
                    if isinstance(node.op, ast.And):
                        self.out.line(f"if (!{value}) return false;")
                    else:
                        self.out.line(f"if ({value}) return true;")
                value = self.condition(node.values[-1])
                self.out.line(f"return {value};")
            return result
        if isinstance(node, ast.Compare) and len(node.ops) > 1:
            return self.comparison(node, condition=True)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value = self.condition(node.operand)
            return self.bind(f"!{value}", "condition")
        if isinstance(node, ast.IfExp):
            result = self.fresh("condition")
            with self.out.block(f"auto {result} = [&]()", "}();"):
                test = self.condition(node.test)
                with self.out.block(f"if ({test})"):
                    value = self.condition(node.body)
                    self.out.line(f"return {value};")
                value = self.condition(node.orelse)
                self.out.line(f"return {value};")
            return result
        return self.evaluate(node, "condition", "return py::truth({value});")

    def loop(self, node: ast.For | ast.While) -> None:
        flag = self.fresh("broken") if node.orelse else None
        if flag:
            self.out.line(f"bool {flag} = false;")
        if isinstance(node, ast.For):
            iterator = self.evaluate(node.iter, "iterator", "return py::checked(PyObject_GetIter({value}.get()));")
        self.loop_flags.append(flag)
        with self.out.block("while (true)"):
            self.out.line("py::check(PyErr_CheckSignals());")
            if isinstance(node, ast.For):
                with self.out.block():
                    item = self.bind(f"py::next({iterator})", "item")
                    self.out.line(f"if (!{item}.get()) break;")
                    self.store(node.target, item)
            else:
                test = self.condition(node.test)
                self.out.line(f"if (!{test}) break;")
            self.body(node.body)
        self.loop_flags.pop()
        if flag:
            with self.out.block(f"if (!{flag})"):
                self.body(node.orelse)

    def statement(self, node: ast.stmt) -> None:
        self.out.line(f"// Python line {node.lineno}")
        match node:
            case ast.If(test=test, body=body, orelse=otherwise):
                condition = self.condition(test)
                with self.out.block(f"if ({condition})"):
                    self.body(body)
                if otherwise:
                    with self.out.block("else"):
                        self.body(otherwise)
            case ast.For() | ast.While():
                with self.out.block():
                    self.loop(node)
            case ast.Break():
                if self.loop_flags[-1]:
                    self.out.line(f"{self.loop_flags[-1]} = true;")
                self.out.line("break;")
            case ast.Continue():
                self.out.line("continue;")
            case ast.Pass():
                self.out.line(";")
            case ast.FunctionDef():
                if self.locals is not None:
                    raise UnsupportedSyntax(node, "nested function")
                definition = self.module.function(node)
                literal = cpp_string(node.name)
                with self.out.block():
                    self.out.line('auto module_name = py::load_global(globals, "__name__");')
                    self.out.line(f"py::store_global(globals, {literal}, py::checked(PyCFunction_NewEx(&{definition}, globals, module_name.get())));")
            case ast.Assign(targets=targets, value=expression):
                with self.out.block():
                    value = self.expression(expression)
                    for target in targets:
                        self.store(target, value)
            case ast.AugAssign(target=ast.Name() as target, op=operator, value=expression):
                with self.out.block():
                    lhs = self.load_name(target)
                    rhs = self.expression(expression)
                    result = self.arithmetic(operator, lhs, rhs, inplace=True)
                    self.store(target, result)
            case ast.Expr(value=expression):
                with self.out.block():
                    self.expression(expression)
            case ast.Return(value=expression):
                with self.out.block():
                    value = self.expression(expression) if expression is not None else self.bind("py::none()")
                    self.out.line(f"return {value}.release();")
            case _:
                raise UnsupportedSyntax(node)

    def literal(self, node: ast.Constant) -> str:
        value = node.value
        if value is None:
            return self.bind("py::none()")
        if value is Ellipsis:
            return self.bind("py::borrow(Py_Ellipsis)")
        if isinstance(value, bool):
            return self.bind(f"py::boolean({str(value).lower()})")
        if isinstance(value, int):
            digits = hex(value)
            if len(digits) > 16384:
                raise UnsupportedSyntax(node, "large integer literal")
            return self.bind(f"py::integer({cpp_string(digits)})")
        if isinstance(value, float):
            number = "std::numeric_limits<double>::infinity()" if math.isinf(value) else value.hex()
            return self.checked("PyFloat_FromDouble", number)
        if isinstance(value, complex):
            real, imaginary = value.real.hex(), value.imag.hex()
            if math.isfinite(value.imag):
                return self.checked("PyComplex_FromDoubles", real, imaginary)
        if isinstance(value, str) and len(value.encode("utf-8", "surrogatepass")) <= 16384:
            return self.bind(unicode_value(value))
        if isinstance(value, bytes) and len(value) <= 16384:
            return self.checked("PyBytes_FromStringAndSize", cpp_bytes(value), str(len(value)))
        raise UnsupportedSyntax(node, "literal requires compatibility mode")

    def arithmetic(self, operator: ast.operator, lhs: str, rhs: str, inplace: bool = False) -> str:
        prefix = "PyNumber_InPlace" if inplace else "PyNumber_"
        arguments = [f"{lhs}.get()", f"{rhs}.get()"]
        if isinstance(operator, ast.Pow):
            arguments.append("Py_None")
        return self.checked(prefix + NUMBER_OPERATORS[type(operator)], *arguments)

    def boolean_operation(self, node: ast.BoolOp) -> str:
        result = self.fresh()
        with self.out.block(f"auto {result} = [&]() -> py::Value", "}();"):
            for operand in node.values[:-1]:
                with self.out.block():
                    value = self.expression(operand)
                    negate = "!" if isinstance(node.op, ast.And) else ""
                    self.out.line(f"if ({negate}py::truth({value})) return {value};")
            value = self.expression(node.values[-1])
            self.out.line(f"return {value};")
        return result

    def compare_pair(self, operator: ast.cmpop, lhs: str, rhs: str) -> str:
        if isinstance(operator, (ast.Is, ast.IsNot)):
            symbol = "==" if isinstance(operator, ast.Is) else "!="
            return self.bind(f"py::boolean({lhs}.get() {symbol} {rhs}.get())")
        if isinstance(operator, (ast.In, ast.NotIn)):
            contains = self.bind(f"PySequence_Contains({rhs}.get(), {lhs}.get())", "contains")
            self.out.line(f"py::check({contains});")
            invert = "==" if isinstance(operator, ast.NotIn) else "!="
            return self.bind(f"py::boolean({contains} {invert} 0)")
        return self.checked("PyObject_RichCompare", f"{lhs}.get()", f"{rhs}.get()", f"Py_{COMPARISONS[type(operator)]}")

    def comparison(self, node: ast.Compare, condition: bool = False) -> str:
        if len(node.ops) == 1:
            lhs = self.expression(node.left)
            rhs = self.expression(node.comparators[0])
            return self.compare_pair(node.ops[0], lhs, rhs)
        result = self.fresh()
        result_type = "bool" if condition else "py::Value"
        with self.out.block(f"auto {result} = [&]() -> {result_type}", "}();"):
            lhs = self.expression(node.left)
            for index, (operator, comparator) in enumerate(zip(node.ops, node.comparators)):
                rhs = self.expression(comparator)
                test = self.compare_pair(operator, lhs, rhs)
                if index == len(node.ops) - 1:
                    value = f"py::truth({test})" if condition else test
                    self.out.line(f"return {value};")
                else:
                    value = "false" if condition else test
                    self.out.line(f"if (!py::truth({test})) return {value};")
                lhs = rhs
        return result

    def container(self, kind: str, items: list[ast.expr]) -> str:
        values = [self.expression(item) for item in items]
        return self.bind(f"py::{kind}({{{', '.join(values)}}})")

    def dictionary(self, keys: list[ast.expr | None], values: list[ast.expr]) -> str:
        entries = []
        for key, value in zip(keys, values):
            if key is None:
                raise UnsupportedSyntax(value, "dictionary unpacking")
            lhs, rhs = self.expression(key), self.expression(value)
            entries.append(f"{{{lhs}, {rhs}}}")
        return self.bind(f"py::dict({{{', '.join(entries)}}})")

    def call(self, node: ast.Call) -> str:
        function = self.expression(node.func)
        args = self.container("tuple", node.args)
        keywords = "nullptr"
        if node.keywords:
            keys = []
            values = []
            for keyword in node.keywords:
                if keyword.arg is None:
                    raise UnsupportedSyntax(keyword, "keyword unpacking")
                keys.append(ast.copy_location(ast.Constant(value=keyword.arg), keyword))
                values.append(keyword.value)
            keywords = f"{self.dictionary(keys, values)}.get()"
        return self.checked("PyObject_Call", f"{function}.get()", f"{args}.get()", keywords)

    def expression(self, node: ast.expr) -> str:
        match node:
            case ast.Constant():
                return self.literal(node)
            case ast.Name():
                return self.load_name(node)
            case ast.BinOp(left=left, op=operator, right=right):
                lhs, rhs = self.expression(left), self.expression(right)
                return self.arithmetic(operator, lhs, rhs)
            case ast.UnaryOp(op=operator, operand=operand):
                value = self.expression(operand)
                if isinstance(operator, ast.Not):
                    return self.bind(f"py::boolean(!py::truth({value}))")
                return self.checked("PyNumber_" + UNARY_OPERATORS[type(operator)], f"{value}.get()")
            case ast.BoolOp():
                return self.boolean_operation(node)
            case ast.Compare():
                return self.comparison(node)
            case ast.IfExp(test=test, body=if_true, orelse=if_false):
                result = self.fresh()
                with self.out.block(f"auto {result} = [&]() -> py::Value", "}();"):
                    test_value = self.condition(test)
                    with self.out.block(f"if ({test_value})"):
                        value = self.expression(if_true)
                        self.out.line(f"return {value};")
                    value = self.expression(if_false)
                    self.out.line(f"return {value};")
                return result
            case ast.Tuple(elts=items):
                return self.container("tuple", items)
            case ast.List(elts=items):
                return self.container("list", items)
            case ast.Set(elts=items):
                return self.container("set", items)
            case ast.Dict(keys=keys, values=values):
                return self.dictionary(keys, values)
            case ast.Attribute(value=owner, attr=key):
                if key in DYNAMIC_ATTRIBUTES:
                    raise UnsupportedSyntax(node, f"attribute {key} requires Python object introspection")
                obj = self.expression(owner)
                return self.checked("PyObject_GetAttrString", f"{obj}.get()", cpp_string(key))
            case ast.Subscript(value=owner, slice=index):
                obj, item = self.expression(owner), self.expression(index)
                return self.checked("PyObject_GetItem", f"{obj}.get()", f"{item}.get()")
            case ast.Slice(lower=lower, upper=upper, step=step):
                arguments = [f"{self.expression(index)}.get()" if index is not None else "Py_None"
                             for index in (lower, upper, step)]
                return self.checked("PySlice_New", *arguments)
            case ast.Call():
                return self.call(node)
            case ast.JoinedStr(values=items):
                return self.container("join_strings", items)
            case ast.FormattedValue(value=expression, conversion=conversion, format_spec=spec):
                value = self.expression(expression)
                format_spec = f"{self.expression(spec)}.get()" if spec is not None else "nullptr"
                if conversion != -1:
                    convert = {ord("s"): "PyObject_Str", ord("r"): "PyObject_Repr", ord("a"): "PyObject_ASCII"}
                    value = self.checked(convert[conversion], f"{value}.get()")
                return self.checked("PyObject_Format", f"{value}.get()", format_spec)
            case _:
                raise UnsupportedSyntax(node)


class CppEmitter:
    def __init__(self, symbols: symtable.SymbolTable):
        self.symbols = {(table.get_name(), table.get_lineno()): table for table in symbols.get_children()
                        if isinstance(table, symtable.Function)}
        self.functions = Writer()
        self.function_count = 0

    def function(self, node: ast.FunctionDef) -> str:
        args = node.args
        if (node.decorator_list or node.returns or getattr(node, "type_params", ()) or
                args.posonlyargs or args.vararg or args.kwarg or args.defaults or args.kwonlyargs or
                any(arg.annotation for arg in args.args)):
            raise UnsupportedSyntax(node, "function signature or decorator")
        parameters = [arg.arg for arg in args.args]
        docstring = ast.get_docstring(node, clean=False)
        if docstring is not None and ("\0" in docstring or len(docstring.encode("utf-8", "surrogatepass")) > 16384):
            raise UnsupportedSyntax(node, "function docstring requires compatibility mode")

        function = f"translated_function_{self.function_count}"
        self.function_count += 1
        definition = f"{function}_definition"
        out = self.functions
        local_names = self.symbols[node.name, node.lineno].get_locals()
        frame = FrameEmitter(self, out, local_names)
        with out.block(f"static PyObject* {function}([[maybe_unused]] PyObject* globals, PyObject* args, PyObject* kwargs)"):
            with out.block("try"):
                out.line("py::RecursionGuard guard;")
                for index, name in enumerate(parameters):
                    out.line(f"static char parameter_{index}[] = {cpp_string(name)};")
                    out.line(f"PyObject* argument_{index};")
                keywords = ", ".join([*(f"parameter_{i}" for i in range(len(parameters))), "nullptr"])
                out.line(f"static char* keywords[] = {{{keywords}}};")
                signature = cpp_string("O" * len(parameters) + ":" + node.name)
                result_pointers = "".join(f", &argument_{i}" for i in range(len(parameters)))
                out.line(f"if (!PyArg_ParseTupleAndKeywords(args, kwargs, {signature}, keywords{result_pointers})) return nullptr;")
                for name, variable in frame.locals.items():
                    initializer = f" = py::borrow(argument_{parameters.index(name)})" if name in parameters else ""
                    out.line(f"py::Value {variable}{initializer};")
                statements = node.body[1:] if docstring is not None else node.body
                frame.body(statements)
                out.line("return Py_NewRef(Py_None);")
            with out.block("catch (const py::Error&)"):
                out.line("return nullptr;")
        doc = cpp_string(docstring) if docstring is not None else "nullptr"
        out.line(f"static PyMethodDef {definition} = {{")
        out.line(f"    {cpp_string(node.name)}, reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)(void)>({function})),")
        out.line(f"    METH_VARARGS | METH_KEYWORDS, {doc}")
        out.line("};")
        out.line()
        return definition

    def module(self, tree: ast.Module) -> str:
        out = Writer()
        frame = FrameEmitter(self, out)
        with out.block("static void run_program([[maybe_unused]] PyObject* globals, [[maybe_unused]] const py::Value& file)"):
            docstring = ast.get_docstring(tree, clean=False)
            if docstring is not None:
                value = frame.literal(tree.body[0].value)
                out.line(f'py::store_global(globals, "__doc__", {value});')
            statements = tree.body[1:] if docstring is not None else tree.body
            frame.body(statements)
        return "\n".join([self.functions.render(), out.render()]).lstrip("\n")


def embedded_module(source: bytes) -> str:
    out = Writer()
    with out.block("static void run_program(PyObject* globals, const py::Value& file)"):
        payload = source + b"\0"
        out.line("static constexpr unsigned char source[] = {")
        for start in range(0, len(payload), 16):
            out.line("    " + " ".join(f"0x{byte:02x}," for byte in payload[start:start + 16]))
        out.line("};")
        out.line("auto bytes = py::checked(PyBytes_FromStringAndSize(")
        out.line("    reinterpret_cast<const char*>(source), sizeof(source) - 1));")
        out.line("py::run_embedded(globals, bytes, file);")
    return out.render()


def translate(source_path: Path, mode: str, *, display_path: Path | None = None) -> Translation:
    source = source_path.read_bytes()
    source_argument = display_path if display_path is not None else source_path
    filename = display_path if display_path is not None else source_path.absolute()
    directory = filename.parent if display_path is not None else filename.resolve().parent
    compile(source, str(filename), "exec", dont_inherit=True)
    fallback_reason = None
    if mode == "compatible":
        program = embedded_module(source)
    else:
        tree = ast.parse(source, filename=str(filename))
        symbols = symtable.symtable(source, str(filename), "exec")
        try:
            program = CppEmitter(symbols).module(tree)
        except UnsupportedSyntax as error:
            if mode == "strict":
                raise
            fallback_reason = str(error)
            program = embedded_module(source)

    runtime_dir = Path(__file__).resolve().parent
    runtime = (runtime_dir / "py_runtime.hpp").read_text(encoding="utf-8")
    entry = (runtime_dir / "py_entry.hpp").read_text(encoding="utf-8")
    paths = (
        "static py::Value source_filename() {\n"
        f"    return {unicode_value(str(filename))};\n"
        "}\n"
        "static py::Value source_directory() {\n"
        f"    return {unicode_value(str(directory))};\n"
        "}\n"
        "static py::Value source_argument() {\n"
        f"    return {unicode_value(str(source_argument))};\n"
        "}\n"
    )
    generated_mode = "CPython compatibility" if mode == "compatible" or fallback_reason else "translated control flow"
    chunks = [f"// Generated Python to C++17 ({generated_mode}).", runtime.strip(),
              "// Program generated from the input file.\n" + program, paths.strip(), entry.strip()]
    return Translation("\n\n".join(chunks) + "\n", fallback_reason)
