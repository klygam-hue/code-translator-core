"""Parse the supported subset of each input language into a shared syntax tree."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re

import portable_ir as ir


EXTENSIONS = {
    "python": ".py", "javascript": ".js", "typescript": ".ts",
    "c": ".c", "cpp": ".cpp", "csharp": ".cs", "java": ".java", "go": ".go",
    "rust": ".rs", "zig": ".zig",
    "ruby": ".rb", "sql": ".sql", "assembly": ".asm",
}
TYPES = {
    "int": "int", "int32": "int", "int32_t": "int", "std::int32_t": "int", "number": "int",
    "bool": "bool", "boolean": "bool", "str": "str", "string": "str", "std::string": "str",
    "String": "str", "char*": "str", "void": "void", "None": "void",
}


def literal(at: ir.Position, value: object) -> ir.Literal:
    if type(value) not in (int, bool, str):
        raise at.error("ожидается целое число, логическое значение или строка")
    return ir.Literal(at, value)


def unary(at: ir.Position, operator: str, value: ir.Expr) -> ir.Expr:
    if operator in {"+", "-"} and isinstance(value, ir.Literal) and type(value.value) is int:
        return ir.Literal(at, -value.value if operator == "-" else value.value)
    return ir.Unary(at, operator, value)


class PythonParser:
    binary = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "floatdiv", ast.FloorDiv: "fdiv", ast.Mod: "fmod"}
    comparisons = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
    reserved = {"print", "range", "__name__"}

    def __init__(self):
        self.ranges = 0
        self.assignments = 0

    @staticmethod
    def at(node: ast.AST) -> ir.Position:
        return ir.Position(node.lineno, node.col_offset + 1)

    def name(self, node: ast.AST, name: str) -> str:
        if name in self.reserved:
            raise self.at(node).error(f"переносимый профиль сохраняет встроенное имя {name}")
        return name

    def annotation(self, node: ast.expr | None, *, result: bool = False) -> ir.Type | None:
        if node is None:
            return None
        if result and isinstance(node, ast.Constant) and node.value is None:
            return "void"
        if isinstance(node, ast.Name) and node.id in {"bool", "int", "str"}:
            return node.id
        supported = "int, bool, str, None" if result else "int, bool, str"
        raise self.at(node).error(f"поддерживаемые аннотации Python: {supported}")

    def expression(self, node: ast.expr) -> ir.Expr:
        at = self.at(node)
        match node:
            case ast.Constant(value=value):
                return literal(at, value)
            case ast.Name(id=name):
                return ir.Name(at, name)
            case ast.UnaryOp(op=operator, operand=operand):
                operators = {ast.Not: "!", ast.UAdd: "+", ast.USub: "-"}
                if type(operator) in operators:
                    return unary(at, operators[type(operator)], self.expression(operand))
            case ast.BinOp(left=left, op=operator, right=right):
                if type(operator) in self.binary:
                    return ir.Binary(at, self.binary[type(operator)], self.expression(left), self.expression(right))
            case ast.BoolOp(op=operator, values=values):
                operator = "&&" if isinstance(operator, ast.And) else "||"
                left = self.expression(values[0])
                for value in values[1:]:
                    left = ir.Binary(at, operator, left, self.expression(value))
                return left
            case ast.Compare(left=left, ops=operators, comparators=others):
                if all(type(op) in self.comparisons for op in operators):
                    values = [self.expression(value) for value in (left, *others)]
                    pairs = [ir.Binary(at, self.comparisons[type(op)], values[index], values[index + 1])
                             for index, op in enumerate(operators)]
                    result = pairs[0]
                    for comparison in pairs[1:]:
                        result = ir.Binary(at, "&&", result, comparison)
                    return result
            case ast.Call(func=ast.Name(id=name), args=arguments, keywords=[]):
                return ir.Call(at, name, tuple(self.expression(arg) for arg in arguments))
            case ast.IfExp(test=test, body=yes, orelse=no):
                return ir.Choice(at, self.expression(test), self.expression(yes), self.expression(no))
        raise at.error(f"выражение {type(node).__name__} вне переносимого профиля")

    def printed(self, node: ast.Call) -> ir.Print:
        settings = {"sep": " ", "end": "\n"}
        for keyword in node.keywords:
            value = keyword.value
            if keyword.arg not in settings or not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                raise self.at(value).error("print поддерживает строковые константы sep и end")
            if "\0" in value.value or any(0xD800 <= ord(char) <= 0xDFFF for char in value.value):
                raise self.at(value).error("разделитель содержит NUL или непарный суррогат Unicode")
            settings[keyword.arg] = value.value
        at = self.at(node)
        if not any(isinstance(argument, ast.JoinedStr) for argument in node.args):
            return ir.Print(at, tuple(self.expression(arg) for arg in node.args),
                            settings["sep"], settings["end"], "upper")
        parts = []
        for index, argument in enumerate(node.args):
            if index and settings["sep"]:
                parts.append(ir.Literal(at, settings["sep"]))
            if not isinstance(argument, ast.JoinedStr):
                parts.append(self.expression(argument))
                continue
            for fragment in argument.values:
                if isinstance(fragment, ast.FormattedValue):
                    if fragment.conversion not in {-1, ord("s")} or fragment.format_spec is not None:
                        raise self.at(fragment).error("поддерживается подстановка {значение} или {значение!s} без спецификатора формата")
                    fragment = fragment.value
                parts.append(self.expression(fragment))
        return ir.Print(at, tuple(parts), "", settings["end"], "upper")

    def body(self, nodes: list[ast.stmt]) -> tuple[ir.Stmt, ...]:
        return tuple(statement for node in nodes if not isinstance(node, ast.Pass) and not docstring(node)
                     for statement in self.lowered(node))

    def lowered(self, node: ast.stmt) -> tuple[ir.Stmt, ...]:
        if isinstance(node, ast.Assign):
            at = self.at(node)
            if len(node.targets) > 1 and all(isinstance(target, ast.Name) for target in node.targets):
                self.assignments += 1
                temporary = f"$assignment{self.assignments}"
                return (ir.Bind(at, temporary, self.expression(node.value)),
                        *(ir.Bind(at, self.name(target, target.id), ir.Name(at, temporary)) for target in node.targets))
            if (len(node.targets) == 1 and isinstance(node.targets[0], (ast.Tuple, ast.List))
                    and isinstance(node.value, (ast.Tuple, ast.List))):
                targets, values = node.targets[0].elts, node.value.elts
                if len(targets) != len(values) or not all(isinstance(target, ast.Name) for target in targets):
                    raise at.error("распаковка требует одинакового количества значений и обычных имён")
                self.assignments += 1
                temporaries = [f"$assignment{self.assignments}_{i}" for i in range(len(values))]
                evaluate = tuple(ir.Bind(at, name, self.expression(value)) for name, value in zip(temporaries, values))
                assign = tuple(ir.Bind(at, self.name(target, target.id), ir.Name(at, name))
                               for target, name in zip(targets, temporaries))
                return evaluate + assign
        return (self.statement(node),)

    def statement(self, node: ast.stmt) -> ir.Stmt:
        at = self.at(node)
        match node:
            case ast.Assign(targets=[ast.Name(id=name)], value=value):
                return ir.Bind(at, self.name(node, name), self.expression(value))
            case ast.AnnAssign(target=ast.Name(id=name), annotation=annotation, value=value) if value is not None:
                return ir.Bind(at, self.name(node, name), self.expression(value), self.annotation(annotation))
            case ast.AugAssign(target=ast.Name(id=name), op=operator, value=value) if type(operator) in self.binary:
                result = ir.Binary(at, self.binary[type(operator)], ir.Name(at, name), self.expression(value))
                return ir.Bind(at, self.name(node, name), result)
            case ast.If(test=condition, body=body, orelse=other):
                return ir.If(at, self.expression(condition), self.body(body), self.body(other))
            case ast.While(test=condition, body=body, orelse=[]):
                return ir.Loop(at, self.expression(condition), self.body(body))
            case ast.For(target=ast.Name(id=name), iter=ast.Call(func=ast.Name(id="range"), args=arguments, keywords=[]),
                         body=body, orelse=[]) if len(arguments) in {1, 2, 3}:
                self.ranges += 1
                index, stop = f"$index{self.ranges}", f"$stop{self.ranges}"
                lo, hi = arguments[:2] if len(arguments) >= 2 else (None, arguments[0])
                step = self.expression(arguments[2]) if len(arguments) == 3 else ir.Literal(at, 1)
                if not isinstance(step, ir.Literal) or type(step.value) is not int:
                    raise at.error("шаг range должен быть постоянным целым числом")
                if step.value == 0:
                    raise at.error("шаг range не может быть равен нулю")
                initial = (ir.Bind(at, index, self.expression(lo) if lo else ir.Literal(at, 0)),
                           ir.Bind(at, stop, self.expression(hi)))
                condition = ir.Binary(at, "<" if step.value > 0 else ">", ir.Name(at, index), ir.Name(at, stop))
                # An overflowing next index is necessarily past the int32 stop.
                limit = ir.INT_MAX - step.value if step.value > 0 else ir.INT_MIN - step.value
                exhausted = ir.Binary(at, ">" if step.value > 0 else "<", ir.Name(at, index), ir.Literal(at, limit))
                advance = ir.Binary(at, "+", ir.Name(at, index), step)
                update = (ir.Bind(at, index, ir.Choice(at, exhausted, ir.Name(at, stop), advance)),)
                assign_index = ir.Bind(at, self.name(node, name), ir.Name(at, index))
                return ir.Loop(at, condition, (assign_index, *self.body(body)), initial, update)
            case ast.Return(value=value):
                if isinstance(value, ast.Constant) and value.value is None:
                    value = None
                return ir.Return(at, self.expression(value) if value is not None else None)
            case ast.Break():
                return ir.Jump(at, "break")
            case ast.Continue():
                return ir.Jump(at, "continue")
            case ast.Expr(value=ast.Call(func=ast.Name(id="print")) as call):
                return self.printed(call)
            case ast.Expr(value=ast.Call() as call):
                expression = self.expression(call)
                return ir.Invoke(at, expression)
        raise at.error(f"инструкция {type(node).__name__} вне переносимого профиля")

    def function(self, node: ast.FunctionDef) -> ir.Function:
        args = node.args
        if (node.decorator_list or getattr(node, "type_params", ()) or args.posonlyargs or args.defaults
                or args.vararg or args.kwarg or args.kwonlyargs):
            raise self.at(node).error("поддерживаются обычные параметры без значений по умолчанию и декораторов")
        parameters = tuple(ir.Parameter(self.at(arg), self.name(arg, arg.arg), self.annotation(arg.annotation))
                           for arg in args.args)
        body = node.body[1:] if docstring(node.body[0]) else node.body
        return ir.Function(self.at(node), self.name(node, node.name), parameters,
                           self.annotation(node.returns, result=True), self.body(body))

    def parse(self, source: bytes, path: Path) -> ir.Program:
        compile(source, str(path), "exec", dont_inherit=True)
        tree = ast.parse(source, filename=str(path))
        functions, statements = [], []
        body = tree.body[1:] if tree.body and docstring(tree.body[0]) else tree.body
        for node in body:
            if isinstance(node, ast.FunctionDef):
                if statements:
                    raise self.at(node).error("объявите функции перед выполняемыми инструкциями")
                functions.append(self.function(node))
            elif isinstance(node, ast.If) and not node.orelse and is_main_guard(node.test):
                statements.extend(self.body(node.body))
            elif not isinstance(node, ast.Pass) and not docstring(node):
                statements.extend(self.lowered(node))
        return ir.Program("python", tuple(functions), tuple(statements))


def docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def is_main_guard(node: ast.expr) -> bool:
    match node:
        case ast.Compare(left=ast.Name(id="__name__"), ops=[ast.Eq()], comparators=[ast.Constant(value="__main__")]):
            return True
    return False


@dataclass(frozen=True)
class Token:
    at: ir.Position
    kind: str
    text: str


SOURCE_TOKEN = re.compile(
    r"(?P<space>[ \t\f\r\n]+)"
    r"|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)"
    r"|(?P<directive>\#[^\n]*)"
    r"|(?P<lifetime>'static\b(?!'))"
    r"""|(?P<string>"(?:\\[^\r\n]|[^"\\\r\n])*"|'(?:\\[^\r\n]|[^'\\\r\n])*')"""
    r"|(?P<number>0[xX][0-9a-fA-F]+|0[bB][01]+|[0-9]+)"
    r"|(?P<name>@?[A-Za-z_][A-Za-z_0-9]*)"
    r"|(?P<punct>===|!==|==|!=|<=|>=|&&|\|\||\+\+|--|::|:=|->|[+\-*/%]=|<<|[{}(),:;.=\[\]+*/%<>!?&\-])"
)


def tokens(source: str, go: bool, pattern: re.Pattern[str] = SOURCE_TOKEN) -> list[Token]:
    result = []
    offset, line, column = 0, 1, 1
    while offset < len(source):
        at = ir.Position(line, column)
        match = pattern.match(source, offset)
        if match is None:
            raise at.error(f"неподдерживаемый символ {source[offset]!r}")
        kind, text = match.lastgroup, match.group()
        if kind not in {"space", "comment"}:
            result.append(Token(at, kind, text))
        newlines = text.count("\n")
        if newlines:
            if go:
                go_semicolon(result, at)
            line, column = line + newlines, len(text.rsplit("\n", 1)[1]) + 1
        else:
            column += len(text)
        offset = match.end()
    at = ir.Position(line, column)
    if go:
        go_semicolon(result, at)
    result.append(Token(at, "eof", "<конец файла>"))
    return result


def go_semicolon(result: list[Token], at: ir.Position) -> None:
    if not result:
        return
    previous = result[-1]
    if (previous.kind in {"number", "string"} or previous.text in {")", "]", "}", "++", "--"}
            or (previous.kind == "name" and previous.text not in {"case", "const", "else", "for", "func", "if", "import",
                                                                  "package", "range", "struct", "switch", "type", "var"})):
        result.append(Token(at, "punct", ";"))


def string_value(token: Token) -> str:
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "\\": "\\", "'": "'", '"': '"'}
    content = token.text[1:-1]

    def decode(match: re.Match[str]) -> str:
        escape = match.group()[1:]
        if escape not in escapes:
            raise token.at.error(f"escape-последовательность \\{escape} вне переносимого профиля")
        return escapes[escape]

    return re.sub(r"\\.", decode, content)


PRECEDENCE = {
    "||": 1, "&&": 2, "==": 3, "!=": 3, "===": 3, "!==": 3,
    "<": 4, "<=": 4, ">": 4, ">=": 4, "+": 5, "-": 5, "*": 6, "/": 6, "%": 6,
}
SOURCE_TYPES = {
    "javascript": set(), "typescript": {"number", "boolean", "string", "void"},
    "c": {"int", "int32_t", "bool", "char*", "void"},
    "cpp": {"int", "int32_t", "std::int32_t", "bool", "char*", "std::string", "void"},
    "csharp": {"int", "bool", "string", "void"},
    "java": {"int", "boolean", "String", "void"},
    "go": {"int", "int32", "bool", "string"},
    "rust": set(), "zig": set(),
}
HEADERS = {"stdio.h", "stdbool.h", "stdint.h", "cstdio", "iostream", "string", "cstdint"}
IDENTIFIERS_RESERVED = {
    "if", "else", "while", "for", "return", "continue", "break", "true", "false", "const",
    "var", "let", "function", "class", "static", "public", "private", "final", "void", "func",
    "package", "import", "new", "switch", "case", "default", "try", "catch", "throw", "enum",
}


class BracedParser:
    def __init__(self, source: str, language: str):
        self.language = language
        self.script = language in {"javascript", "typescript"}
        self.c_family = language in {"c", "cpp"}
        self.go = language == "go"
        self.items = tokens(source, self.go)
        self.index = 0
        self.imports: set[str] = set()
        self.prototypes: list[ir.Function] = []

    @property
    def token(self) -> Token:
        return self.items[self.index]

    def is_at(self, *choices: str) -> bool:
        return self.token.text in choices

    def take(self, text: str | None = None) -> Token:
        token = self.token
        if text is not None and token.text != text:
            raise token.at.error(f"ожидается {text}, получено {token.text}")
        if token.kind == "eof":
            raise token.at.error("неожиданный конец файла")
        self.index += 1
        return token

    def accept(self, text: str) -> bool:
        if self.is_at(text):
            self.take()
            return True
        return False

    def identifier(self) -> Token:
        token = self.take()
        reserved_library = (
            (self.script and token.text in {"console", "Math"})
            or (self.go and token.text in {"fmt", "os", "_"})
            or (self.language == "java" and token.text == "System")
            or (self.language == "csharp" and token.text in {"System", "Console"})
            or (self.c_family and token.text == "printf")
        )
        if token.kind != "name" or token.text in IDENTIFIERS_RESERVED or reserved_library:
            raise token.at.error(f"ожидается пользовательское имя, получено {token.text}")
        if token.text.startswith("@"):
            raise token.at.error(f"ожидается пользовательское имя, получено {token.text}")
        return token

    def end(self) -> None:
        if self.accept(";"):
            return
        if self.go and self.is_at("}", ")"):
            return
        if self.script and (self.is_at("}") or self.token.kind == "eof"
                            or self.token.at.line > self.items[self.index - 1].at.line):
            return
        self.take(";")

    def type_name(self, *, include_void: bool = False) -> ir.Type:
        token = self.take()
        name = token.text
        if name == "std" and self.accept("::"):
            name += "::" + self.take().text
        if name == "const" and self.c_family:
            name = self.take().text
        if name == "char" and self.c_family:
            self.take("*")
            name = "char*"
        if name not in SOURCE_TYPES[self.language] or (name == "void" and not include_void):
            raise token.at.error(f"тип {name} вне переносимого профиля {self.language}")
        if name == "bool" and self.language == "c" and "stdbool.h" not in self.imports:
            raise token.at.error("для bool в C добавьте #include <stdbool.h>")
        return TYPES[name]

    def expression(self, precedence: int = 1) -> ir.Expr:
        token = self.take()
        at, text = token.at, token.text
        if text == "(":
            left = self.expression()
            self.take(")")
        elif text in {"!", "+", "-"}:
            operand = self.expression(max(PRECEDENCE.values()) + 1)
            if self.script and text == "-" and isinstance(operand, ir.Literal) and operand.value == 0:
                raise at.error("отрицательный ноль JavaScript вне переносимого профиля")
            left = unary(at, text, operand)
        elif token.kind == "number":
            if len(text) > 1 and text.startswith("0") and text[1].isdigit():
                raise at.error("используйте десятичные числа без ведущего нуля или префикс 0x")
            left = ir.Literal(at, int(text, 0))
        elif text in {"true", "false"}:
            if self.language == "c" and "stdbool.h" not in self.imports:
                raise at.error("для bool в C добавьте #include <stdbool.h>")
            left = ir.Literal(at, text == "true")
        elif token.kind == "string":
            left = ir.Literal(at, self.string_literal(token))
        elif token.kind == "name" and text not in IDENTIFIERS_RESERVED:
            while self.is_at(".", "::"):
                text += self.take().text
                member = self.take()
                if member.kind != "name":
                    raise member.at.error("ожидается имя метода")
                text += member.text
            if self.accept("("):
                left = self.call_expression(at, text, self.arguments())
            else:
                left = ir.Name(at, text)
        else:
            raise at.error(f"неподдерживаемое выражение {text}")

        while (binding := PRECEDENCE.get(self.binary_operator(self.token.text), 0)) >= precedence:
            operator_token = self.take()
            op = self.binary_operator(operator_token.text)
            if op in {"===", "!=="} and not self.script:
                raise operator_token.at.error("строгое сравнение ===/!== доступно в JavaScript и TypeScript")
            right = self.expression(binding + 1)
            if self.script and isinstance(left, ir.Literal) and isinstance(right, ir.Literal):
                a, b = left.value, right.value
                if type(a) is int and type(b) is int and (
                    (op == "%" and a < 0 and b != 0 and a % b == 0)
                    or (op == "*" and ((a == 0 and b < 0) or (b == 0 and a < 0)))
                ):
                    raise operator_token.at.error("операция даёт отрицательный ноль JavaScript, который не переносится в int32")
            left = ir.Binary(at, self.ir_operator(op), left, right)
        if self.script and precedence == 1 and self.accept("?"):
            yes = self.expression()
            self.take(":")
            return ir.Choice(at, left, yes, self.expression())
        return left

    def string_literal(self, token: Token) -> str:
        if token.text.startswith("'") and not self.script:
            raise token.at.error("символьные литералы вне переносимого профиля")
        return string_value(token)

    def ir_operator(self, operator: str) -> str:
        return {"/": "floatdiv" if self.script else "tdiv", "%": "tmod", "===": "==", "!==": "!="}.get(operator, operator)

    def binary_operator(self, text: str) -> str:
        return text

    def call_expression(self, at: ir.Position, name: str, arguments: tuple[ir.Expr, ...]) -> ir.Expr:
        if self.script and name in {"Math.trunc", "Math.floor"} and len(arguments) == 1:
            argument = arguments[0]
            if isinstance(argument, ir.Binary) and argument.operator == "floatdiv":
                left, right = argument.left, argument.right
                if isinstance(left, ir.Literal) and isinstance(right, ir.Literal):
                    a, b = left.value, right.value
                    if type(a) is int and type(b) is int and b != 0 and (
                        (a == 0 and b < 0)
                        or (name == "Math.trunc" and a * b < 0 and abs(a) < abs(b))
                    ):
                        raise at.error("операция даёт отрицательный ноль JavaScript, который не переносится в int32")
                return ir.Binary(at, "tdiv" if name == "Math.trunc" else "fdiv", argument.left, argument.right)
            raise at.error("ожидается Math.trunc(a / b) или Math.floor(a / b)")
        return ir.Call(at, name, arguments)

    def arguments(self) -> tuple[ir.Expr, ...]:
        arguments = []
        if not self.accept(")"):
            while True:
                arguments.append(self.expression())
                if not self.accept(",") or (self.language not in {"c", "cpp", "java"} and self.is_at(")")):
                    self.take(")")
                    break
        return tuple(arguments)

    def block(self) -> tuple[ir.Stmt, ...]:
        self.take("{")
        statements = []
        while not self.accept("}"):
            if not self.accept(";"):
                statements.append(self.statement())
        return tuple(statements)

    def branch(self) -> tuple[ir.Stmt, ...]:
        if self.is_at("{") or self.go:
            return self.block()
        return (self.statement(),)

    def test(self) -> ir.Expr:
        if self.go:
            return self.expression()
        self.take("(")
        expression = self.expression()
        self.take(")")
        return expression

    def statement(self) -> ir.Stmt:
        at = self.token.at
        if self.accept("if"):
            condition = self.test()
            body = self.branch()
            if self.accept("else"):
                otherwise = (self.statement(),) if self.is_at("if") else self.branch()
            else:
                otherwise = ()
            return ir.If(at, condition, body, otherwise)
        if not self.go and self.accept("while"):
            return ir.Loop(at, self.test(), self.branch())
        if self.accept("for"):
            return self.loop(at)
        if self.accept("return"):
            line_break = self.script and self.token.at.line > at.line
            value = None if line_break or self.is_at(";", "}") or self.token.kind == "eof" else self.expression()
            self.end()
            return ir.Return(at, value)
        if self.is_at("break", "continue"):
            command = self.take().text
            self.end()
            return ir.Jump(at, command)
        if self.language == "cpp" and self.is_at("std") and [token.text for token in self.items[self.index:self.index + 4]] == ["std", "::", "cout", "<<"]:
            return self.cout()
        statement = self.simple()
        self.end()
        return statement

    def simple(self) -> ir.Stmt:
        at = self.token.at
        start = self.token.text
        following = self.items[min(self.index + 1, len(self.items) - 1)].text
        if (self.script and start in {"let", "const"}) or (self.go and start in {"var", "const"}) or (self.language == "csharp" and start == "var"):
            constant = self.take().text == "const"
            name = self.identifier()
            annotation = None
            if self.language == "typescript" and self.accept(":"):
                annotation = self.type_name()
            if self.go and not self.is_at("="):
                annotation = self.type_name()
            self.take("=")
            return ir.Bind(at, name.text, self.expression(), annotation, declaration=True, constant=constant)
        modifier = "final" if self.language == "java" else "const"
        if not self.script and not self.go and (
            start in SOURCE_TYPES[self.language] or start == modifier or (self.c_family and start == "char")
            or (self.language == "cpp" and start == "std" and following == "::"
                and self.items[min(self.index + 2, len(self.items) - 1)].text in {"string", "int32_t"})
        ):
            constant = self.is_at(modifier)
            if constant:
                self.take()
            annotation = self.type_name()
            name = self.identifier().text
            self.take("=")
            return ir.Bind(at, name, self.expression(), annotation, declaration=True, constant=constant)
        if start in {"++", "--"} and not self.go:
            operator = self.take().text
            name = self.identifier()
            return self.increment(at, name.text, operator)
        if self.token.kind == "name" and following in {"=", ":=", "+=", "-=", "*=", "/=", "%=", "++", "--"}:
            name = self.identifier().text
            operator = self.take().text
            if operator in {"++", "--"}:
                return self.increment(at, name, operator)
            if operator == ":=" and not self.go:
                raise at.error("сокращённое объявление := доступно для Go")
            value = self.expression()
            if operator not in {"=", ":="}:
                plain = {"/=": "floatdiv" if self.script else "tdiv", "%=": "tmod"}.get(operator, operator[0])
                value = ir.Binary(at, plain, ir.Name(at, name), value)
            return ir.Bind(at, name, value, declaration=(operator == ":="))
        expression = self.expression()
        if not isinstance(expression, ir.Call):
            raise at.error("ожидается присваивание или вызов функции")
        return self.invoke(expression)

    @staticmethod
    def increment(at: ir.Position, name: str, operator: str) -> ir.Bind:
        value = ir.Binary(at, operator[0], ir.Name(at, name), ir.Literal(at, 1))
        return ir.Bind(at, name, value)

    def loop(self, at: ir.Position) -> ir.Loop:
        if not self.go:
            self.take("(")
        if self.go and self.is_at("{"):
            return ir.Loop(at, ir.Literal(at, True), self.block())
        if self.go and not self.is_at(";"):
            start = self.index
            for token in self.items[start:]:
                if token.text == "{":
                    return ir.Loop(at, self.expression(), self.block())
                if token.text == ";" or token.kind == "eof":
                    break
        initial = () if self.is_at(";") else (self.simple(),)
        self.take(";")
        condition = ir.Literal(at, True) if self.is_at(";") else self.expression()
        self.take(";")
        update = () if self.is_at("{" if self.go else ")") else (self.simple(),)
        if not self.go:
            self.take(")")
        return ir.Loop(at, condition, self.branch(), initial, update)

    def invoke(self, call: ir.Call) -> ir.Stmt:
        at, name, argv = call.at, call.name, call.arguments
        if self.script and name == "console.log":
            if argv and isinstance(argv[0], ir.Literal) and isinstance(argv[0].value, str) and "%" in argv[0].value and len(argv) > 1:
                return formatted_print(call, c_style=False, ending="\n")
            return ir.Print(at, argv, " ", "\n", "lower")
        if self.language == "java" and name in {"System.out.print", "System.out.println"}:
            if len(argv) > 1 or (not argv and name == "System.out.print"):
                raise at.error("System.out принимает одно переносимое значение")
            return ir.Print(at, argv, "", "\n" if name.endswith("println") else "", "lower")
        if self.language == "csharp" and name in {"Console.Write", "Console.WriteLine", "System.Console.Write", "System.Console.WriteLine"}:
            newline = name.endswith("Line")
            if (name.startswith("Console") and "System" not in self.imports) or len(argv) > 1 or (not argv and not newline):
                raise at.error("Console требует using System и принимает одно переносимое значение")
            return ir.Print(at, argv, "", "\n" if newline else "", "upper")
        if self.go and name in {"fmt.Print", "fmt.Println"}:
            if "fmt" not in self.imports or (name == "fmt.Print" and len(argv) > 1):
                raise at.error('поддерживаются импорт "fmt", fmt.Println и fmt.Print с одним аргументом')
            return ir.Print(at, argv, " ", "\n" if name.endswith("Println") else "", "lower")
        if (self.c_family and name in {"printf", "std::printf"}) or (self.go and name == "fmt.Printf"):
            if not (self.imports & {"stdio.h", "cstdio", "fmt"}):
                raise at.error("добавьте стандартный заголовок или импорт для форматированного вывода")
            return formatted_print(call, c_style=self.c_family)
        return ir.Invoke(at, call)

    def cout(self) -> ir.Print:
        at = self.take("std").at
        self.take("::")
        self.take("cout")
        if "iostream" not in self.imports:
            raise at.error("для std::cout нужен #include <iostream>")
        values = []
        while self.accept("<<"):
            if self.token.kind == "string" and self.token.text.startswith("'"):
                token = self.take()
                character = string_value(token)
                if len(character) != 1 or not character.isascii():
                    raise token.at.error("в std::cout поддерживается один ASCII-символ в одинарных кавычках")
                value = ir.Literal(token.at, character)
            else:
                value = self.expression()
                if isinstance(value, ir.Name) and value.name == "std::endl":
                    value = ir.Literal(value.at, "\n")
            values.append(value)
        self.end()
        return ir.Print(at, tuple(values), "", "", "numeric")

    def function(self) -> ir.Function | None:
        at = self.token.at
        annotation = None
        if self.script:
            self.take("function")
        elif self.go:
            self.take("func")
        else:
            if self.language in {"java", "csharp"}:
                if self.is_at("public", "private") or (self.language == "csharp" and self.is_at("internal")):
                    self.take()
                self.take("static")
            else:
                self.accept("static")
            annotation = self.type_name(include_void=True)

        name = self.identifier().text
        self.take("(")
        parameters = []
        if self.c_family and self.is_at("void"):
            self.take()
        elif self.language == "java" and name == "main":
            self.take("String")
            self.take("[")
            self.take("]")
            self.identifier()
        elif self.language == "csharp" and name == "Main" and not self.is_at(")"):
            self.take("string")
            self.take("[")
            self.take("]")
            self.identifier()
        elif not self.is_at(")"):
            while True:
                if self.script:
                    parameter = self.identifier()
                    kind = self.type_name() if self.language == "typescript" and self.accept(":") else None
                    parameters.append(ir.Parameter(parameter.at, parameter.text, kind))
                elif self.go:
                    grouped = [self.identifier()]
                    while self.accept(","):
                        grouped.append(self.identifier())
                    kind = self.type_name()
                    parameters.extend(ir.Parameter(parameter.at, parameter.text, kind) for parameter in grouped)
                else:
                    kind = self.type_name()
                    parameter = self.identifier()
                    parameters.append(ir.Parameter(parameter.at, parameter.text, kind))
                if not self.accept(",") or (self.script and self.is_at(")")):
                    break
        self.take(")")
        if self.language == "typescript" and self.accept(":"):
            annotation = self.type_name(include_void=True)
        if self.go:
            annotation = "void" if self.is_at("{") else self.type_name()
        if self.c_family and self.accept(";"):
            self.prototypes.append(ir.Function(at, name, tuple(parameters), annotation, ()))
            return None
        body = self.block()
        return ir.Function(at, name, tuple(parameters), annotation, body)

    def parse(self) -> ir.Program:
        if self.c_family:
            while self.token.kind == "directive":
                token = self.take()
                match = re.fullmatch(r"#\s*include\s*<([a-z0-9.]+)>\s*", token.text)
                if not match or match[1] not in HEADERS:
                    raise token.at.error("поддерживаются стандартные заголовки без макросов препроцессора")
                self.imports.add(match[1])
        if self.go:
            self.take("package")
            self.take("main")
            self.end()
            while self.accept("import"):
                if self.accept("("):
                    while not self.accept(")"):
                        self.go_import()
                        self.end()
                else:
                    self.go_import()
                    self.end()
                self.accept(";")
        class_language = self.language in {"java", "csharp"}
        if class_language:
            if self.language == "csharp":
                while self.accept("using"):
                    self.take("System")
                    self.end()
                    self.imports.add("System")
                if not self.accept("public"):
                    self.accept("internal")
                if not self.accept("static"):
                    self.accept("sealed")
            else:
                self.accept("public")
                self.accept("final")
            self.take("class")
            self.identifier()
            self.take("{")
        functions, statements = [], []
        while self.token.kind != "eof" and not (class_language and self.is_at("}")):
            if self.accept(";"):
                continue
            if self.script and not self.is_at("function"):
                statements.append(self.statement())
            elif function := self.function():
                functions.append(function)
        if class_language:
            self.take("}")
            if self.language == "csharp":
                self.accept(";")
            if self.token.kind != "eof":
                raise self.token.at.error("переносимый профиль требует один класс со статическими методами")
        signatures = {function.name: function for function in functions}
        for prototype in self.prototypes:
            definition = signatures.get(prototype.name)
            if (definition is None or definition.returns != prototype.returns
                    or [p.annotation for p in prototype.parameters] != [p.annotation for p in definition.parameters]):
                raise prototype.at.error(f"для объявления {prototype.name} нужно совпадающее определение в этом файле")
        main = None
        main_name = "Main" if self.language == "csharp" else "main"
        if not self.script and main_name in signatures:
            entry = signatures[main_name]
            allowed = {"int", "void"} if self.language == "csharp" else {"int"} if self.c_family else {"void"}
            if entry.parameters or entry.returns not in allowed:
                raise entry.at.error(f"в точке входа ожидается результат {' или '.join(sorted(allowed))} и стандартная сигнатура")
            main = entry.name
        return ir.Program(self.language, tuple(functions), tuple(statements), main)

    def go_import(self) -> None:
        token = self.take()
        if token.text != '"fmt"':
            raise token.at.error('переносимые программы Go поддерживают импорт "fmt" для вывода')
        self.imports.add("fmt")


def formatted_print(call: ir.Call, *, c_style: bool, ending: str = "") -> ir.Print:
    if not call.arguments or not isinstance(call.arguments[0], ir.Literal) or not isinstance(call.arguments[0].value, str):
        raise call.at.error("printf требует строковый литерал формата")
    form, *arguments = call.arguments
    pattern = re.compile(r"[^%]+|%%|%[ds]")
    values, formats = [], []
    offset, index = 0, 0
    while offset < len(form.value):
        match = pattern.match(form.value, offset)
        if not match:
            raise call.at.error("поддерживаемые форматы printf: %d, %s и %%")
        fragment = match.group()
        if fragment in {"%d", "%s"}:
            if index >= len(arguments):
                raise call.at.error("недостаточно аргументов printf")
            values.append(arguments[index])
            formats.append("D" if fragment == "%d" and not c_style else fragment[1])
            index += 1
        else:
            values.append(ir.Literal(form.at, "%" if fragment == "%%" else fragment))
            formats.append("*")
        offset = match.end()
    if index != len(arguments):
        raise call.at.error("лишние аргументы printf")
    return ir.Print(call.at, tuple(values), "", ending, "numeric" if c_style else "lower", tuple(formats))


class SystemsParser(BracedParser):
    def __init__(self, source: str, language: str):
        super().__init__(source, language)
        self.rust = language == "rust"

    def type_name(self, *, include_void: bool = False) -> ir.Type:
        token = self.take()
        if token.text in {"i32", "bool"}:
            return "int" if token.text == "i32" else "bool"
        if self.rust and token.text == "&":
            self.accept("'static")
            self.take("str")
            return "str"
        if not self.rust and token.text == "[":
            self.take("]")
            self.take("const")
            self.take("u8")
            return "str"
        if include_void and not self.rust and token.text == "void":
            return "void"
        if include_void and self.rust and token.text == "(":
            self.take(")")
            return "void"
        raise token.at.error("ожидается i32, bool или неизменяемая строка")

    def test(self) -> ir.Expr:
        if self.rust:
            return self.expression()
        return super().test()

    def branch(self) -> tuple[ir.Stmt, ...]:
        return self.block() if self.rust else super().branch()

    def binary_operator(self, text: str) -> str:
        return text if self.rust else {"and": "&&", "or": "||"}.get(text, text)

    def call_expression(self, at: ir.Position, name: str, arguments: tuple[ir.Expr, ...]) -> ir.Expr:
        intrinsics = {"@divTrunc": "tdiv", "@divFloor": "fdiv", "@rem": "tmod", "@mod": "fmod"}
        if not self.rust and name in intrinsics:
            if len(arguments) != 2:
                raise at.error(f"{name} ожидает два аргумента")
            return ir.Binary(at, intrinsics[name], *arguments)
        return super().call_expression(at, name, arguments)

    def statement(self) -> ir.Stmt:
        token = self.token
        at = token.at
        if self.rust and token.text in {"print", "println", "eprint", "eprintln"}:
            self.take()
            self.take("!")
            self.take("(")
            argv = self.arguments()
            self.end()
            return brace_print(at, argv, newline=token.text.endswith("ln"),
                               stderr=token.text.startswith("e"), rust=True)
        if not self.rust and [item.text for item in self.items[self.index:self.index+3]] == ["std", ".", "debug"]:
            self.take("std")
            self.take(".")
            self.take("debug")
            self.take(".")
            self.take("print")
            if "std" not in self.imports:
                raise at.error('для std.debug.print нужен const std = @import("std");')
            self.take("(")
            form = self.expression()
            self.take(",")
            self.take(".")
            self.take("{")
            args = []
            if not self.is_at("}"):
                while True:
                    args.append(self.expression())
                    if not self.accept(",") or self.is_at("}"):
                        break
            self.take("}")
            self.accept(",")
            self.take(")")
            self.end()
            return brace_print(at, (form, *args), newline=False, stderr=True, rust=False)
        return super().statement()

    def simple(self) -> ir.Stmt:
        at = self.token.at
        if (self.rust and self.accept("let")) or (not self.rust and self.is_at("const", "var")):
            constant = not self.accept("mut") if self.rust else self.take().text == "const"
            name = self.identifier().text
            annotation = self.type_name() if self.accept(":") else None
            self.take("=")
            return ir.Bind(at, name, self.expression(), annotation, declaration=True, constant=constant)
        if self.is_at("for", "++", "--"):
            raise at.error("используйте цикл while и присваивания")
        return super().simple()

    def function(self) -> ir.Function:
        at = self.token.at
        self.accept("pub")
        self.take("fn")
        name = self.identifier().text
        self.take("(")
        parameters = []
        while not self.is_at(")"):
            if self.rust:
                self.accept("mut")
            parameter = self.identifier()
            self.take(":")
            parameters.append(ir.Parameter(parameter.at, parameter.text, self.type_name()))
            if not self.accept(","):
                break
        self.take(")")
        returns = self.type_name(include_void=True) if not self.rust or self.accept("->") else "void"
        body = self.rust_body() if self.rust and returns != "void" else self.block()
        return ir.Function(at, name, tuple(parameters), returns, body)

    def rust_body(self) -> tuple[ir.Stmt, ...]:
        self.take("{")
        body = []
        while not self.accept("}"):
            start = self.index
            try:
                value = self.expression()
            except ValueError:
                value = None
            if value is not None and self.accept("}"):
                body.append(ir.Return(value.at, value))
                break
            self.index = start
            if not self.accept(";"):
                body.append(self.statement())
        return tuple(body)

    def parse(self) -> ir.Program:
        if not self.rust and self.accept("const"):
            for text in ("std", "=", "@import", "(", '"std"', ")", ";"):
                self.take(text)
            self.imports.add("std")
        return super().parse()


def brace_print(at: ir.Position, arguments: tuple[ir.Expr, ...], *,
                newline: bool, stderr: bool, rust: bool) -> ir.Print:
    if not arguments and rust and newline:
        return ir.Print(at, (), "", "\n", "lower", stderr=stderr)
    if not arguments or not isinstance(arguments[0], ir.Literal) or not isinstance(arguments[0].value, str):
        raise at.error("формат вывода должен быть строковым литералом")
    form, *args = arguments
    values, formats = [], []
    index, offset = 0, 0
    pattern = re.compile(r"[^{}]+|\{\{|\}\}|\{[A-Za-z_0-9]*\}")
    while offset < len(form.value):
        match = pattern.match(form.value, offset)
        if not match:
            raise at.error("формат вывода вне переносимого профиля")
        part = match.group()
        offset = match.end()
        if part in {"{{", "}}"} or not part.startswith("{"):
            values.append(ir.Literal(at, part[0] if part in {"{{", "}}"} else part))
            formats.append("*")
            continue
        spec = part[1:-1]
        if rust and spec and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", spec):
            values.append(ir.Name(at, spec))
            formats.append("*")
        elif (rust and not spec) or (not rust and spec in {"", "d", "s"}):
            if index >= len(args):
                raise at.error("недостаточно аргументов формата")
            values.append(args[index])
            formats.append({"": "*", "d": "D", "s": "s"}[spec])
            index += 1
        else:
            raise at.error("формат вывода вне переносимого профиля")
    if index != len(args):
        raise at.error("лишние аргументы формата")
    return ir.Print(at, tuple(values), "", "\n" if newline else "", "lower", tuple(formats), stderr=stderr)


class RubyParser(BracedParser):
    def __init__(self, source: str):
        super().__init__(source, "ruby")

    def string_literal(self, token: Token) -> str:
        content = token.text[1:-1]
        if token.text.startswith("'"):
            return re.sub(r"\\([\\'])", r"\1", content)
        if re.search(r"(?<!\\)#(?:\{|@|\$)", content):
            raise token.at.error("интерполяция строк Ruby пока не поддерживается")
        return string_value(token)

    def ir_operator(self, operator: str) -> str:
        if operator in {"/", "%"}:
            return {"/": "fdiv", "%": "fmod"}[operator]
        return super().ir_operator(operator)

    def end(self) -> None:
        if (self.accept(";") or self.is_at("end", "else", "elsif") or self.token.kind == "eof"
                or self.token.at.line > self.items[self.index - 1].at.line):
            return
        self.take(";")

    def statements(self, stop: tuple[str, ...], *, implicit_return: bool = False) -> tuple[ir.Stmt, ...]:
        body = []
        while self.token.kind != "eof" and not self.is_at(*stop):
            if not self.accept(";"):
                body.append(self.statement(implicit_return=implicit_return))
        if implicit_return and body:
            last = body[-1]
            if isinstance(last, ir.Bind):
                body.append(ir.Return(last.at, ir.Name(last.at, last.name)))
            elif isinstance(last, ir.Invoke):
                body[-1] = ir.Return(last.at, last.call)
        return tuple(body)

    def conditional(self, at: ir.Position) -> ir.If:
        condition = self.expression()
        if not self.accept("then"):
            self.end()
        body = self.statements(("elsif", "else", "end"))
        branch = self.token.at
        if self.accept("elsif"):
            otherwise = (self.conditional(branch),)
        else:
            otherwise = self.statements(("end",)) if self.accept("else") else ()
            self.take("end")
        return ir.If(at, condition, body, otherwise)

    def statement(self, *, implicit_return: bool = False) -> ir.Stmt:
        at = self.token.at
        if self.accept("if"):
            result = self.conditional(at)
        elif self.accept("while"):
            condition = self.expression()
            if not self.accept("do"):
                self.end()
            result = ir.Loop(at, condition, self.statements(("end",)))
            self.take("end")
        elif self.accept("return"):
            empty = self.is_at("end", ";") or self.token.kind == "eof" or self.token.at.line > at.line
            result = ir.Return(at, None if empty else self.expression())
        elif self.is_at("break", "next"):
            result = ir.Jump(at, "break" if self.take().text == "break" else "continue")
        elif self.is_at("puts", "print"):
            puts = self.take().text == "puts"
            if self.accept("("):
                values = self.arguments()
            else:
                values = []
                if self.token.at.line == at.line and self.token.kind != "eof" and not self.is_at(";", "end"):
                    values.append(self.expression())
                    while self.accept(","):
                        values.append(self.expression())
            if puts and len(values) > 1:
                raise at.error("используйте отдельный puts для каждого значения")
            ending = "\n" if puts else ""
            formats = ()
            if puts and values:
                value = values[0]
                if isinstance(value, ir.Literal):
                    if isinstance(value.value, str) and value.value.endswith("\n"):
                        ending = ""
                else:
                    formats = ("d",)
            result = ir.Print(at, tuple(values), "", ending, "lower", formats)
        elif self.token.kind == "name" and self.items[self.index + 1].text in {"=", "+=", "-=", "*=", "/=", "%="}:
            name, operator = self.identifier().text, self.take().text
            if name in {"def", "end", "puts", "print", "exit", "next", "unless", "elsif", "nil", "do", "then"} or not name[0].islower() and name[0] != "_":
                raise at.error("ожидается локальная переменная Ruby")
            value = self.expression()
            if operator != "=":
                value = ir.Binary(at, self.ir_operator(operator[0]), ir.Name(at, name), value)
            result = ir.Bind(at, name, value)
        else:
            value = self.expression()
            if isinstance(value, ir.Call):
                result = ir.Invoke(at, value)
            elif implicit_return and self.is_at("end", ";"):
                result = ir.Return(at, value)
            else:
                raise at.error("ожидается присваивание, вызов или управляющая инструкция Ruby")
        self.end()
        return result

    def parse(self) -> ir.Program:
        functions = []
        while self.accept("def"):
            name = self.identifier()
            parameters = []
            if self.accept("(") and not self.accept(")"):
                while True:
                    parameter = self.identifier()
                    parameters.append(ir.Parameter(parameter.at, parameter.text, None))
                    if not self.accept(","):
                        self.take(")")
                        break
            self.end()
            body = self.statements(("end",), implicit_return=True)
            self.take("end")
            self.end()
            functions.append(ir.Function(name.at, name.text, tuple(parameters), None, body))
        return ir.Program("ruby", tuple(functions), self.statements(()))


def parse(path: Path, language: str) -> ir.Program:
    source = path.read_bytes()
    if language == "python":
        return PythonParser().parse(source, path)
    if language in {"rust", "zig"}:
        return SystemsParser(source.decode("utf-8-sig"), language).parse()
    if language == "ruby":
        return RubyParser(source.decode("utf-8-sig")).parse()
    if language in {"sql", "assembly"}:
        from scalar_languages import parse_scalar
        return parse_scalar(source.decode("utf-8-sig"), language)
    return BracedParser(source.decode("utf-8-sig"), language).parse()
