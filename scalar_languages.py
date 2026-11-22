"""Translate small SQLite queries and straight-line NASM integer programs."""

import operator
import re

import portable_ir as ir
from portable_parse import BracedParser, Token, tokens, unary


SQL_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>--[^\n]*|/\*[\s\S]*?\*/)"
    r"|(?P<string>'(?:''|[^'])*')|(?P<number>0[xX][0-9a-fA-F]+|[0-9]+)"
    r"|(?P<name>[A-Za-z_][A-Za-z_0-9]*)|(?P<punct>[(),+*/%;\-])"
)
ASM_REGISTER = re.compile(r"(?:eax|ecx|edx|edi|esi|r(?:8|9|10|11)d)")
ASM_INTEGER = re.compile(r"[+-]?(?:0x[0-9a-f]+|[0-9]+)")
ASM_BINARY = {"add": "+", "sub": "-", "imul": "*"}
INT_BINARY = {"+": operator.add, "-": operator.sub, "*": operator.mul}


class SqlParser(BracedParser):
    def __init__(self, source: str):
        self.items = tokens(source, False, SQL_TOKEN)
        self.index = 0
        self.script = False
        self.language = "sql"

    def string_literal(self, token: Token) -> str:
        return token.text[1:-1].replace("''", "'")

    def keyword(self, name: str) -> Token:
        token = self.take()
        if token.text.lower() != name:
            raise token.at.error(f"в скалярном запросе SQLite ожидается {name.upper()}")
        return token

    def sql_name(self) -> Token:
        token = self.take()
        if token.kind != "name" or token.text.lower() in {"select", "from", "where", "as", "with", "null", "true", "false"}:
            raise token.at.error("ожидается имя вычисления или столбца")
        return token

    def from_step(self, previous: str) -> None:
        self.keyword("from")
        name = self.sql_name()
        if name.text.lower() != previous:
            raise name.at.error("FROM должен ссылаться на предыдущее скалярное вычисление WITH")

    def parse(self) -> ir.Program:
        body = []
        values = {}
        previous = ""
        steps = set()
        if self.token.text.lower() == "with":
            self.take()
            while True:
                step = self.sql_name()
                name = step.text.lower()
                if name in steps:
                    raise step.at.error("повторное имя вычисления WITH")
                steps.add(name)
                self.keyword("as")
                self.take("(")
                self.keyword("select")
                if previous:
                    self.take("*")
                    self.take(",")
                expression = self.expression()
                self.keyword("as")
                column = self.sql_name()
                variable = column.text.lower()
                if variable in values:
                    raise column.at.error("повторное имя столбца WITH")
                if previous:
                    self.from_step(previous)
                self.take(")")
                values[variable] = constant(expression, values)
                body.append(ir.Bind(column.at, variable, expression, declaration=True, constant=True))
                previous = name
                if not self.accept(","):
                    break
        start = self.keyword("select")
        result = self.expression()
        if self.token.text.lower() == "as":
            self.take()
            self.sql_name()
        if previous:
            self.from_step(previous)
        self.accept(";")
        if self.token.kind != "eof":
            raise self.token.at.error("SQLite: ожидается одно выражение SELECT без таблиц")
        constant(result, values)
        body.append(ir.Print(start.at, (result,), "", "\n", "lower"))
        return ir.Program("sql", (), tuple(body))

    def expression(self, precedence: int = 1) -> ir.Expr:
        expression = super().expression(precedence)
        if isinstance(expression, ir.Name):
            return ir.Name(expression.at, expression.name.lower())
        return expression


def asm_operand(text: str, at: ir.Position) -> ir.Expr:
    if ASM_REGISTER.fullmatch(text):
        return ir.Name(at, text)
    if ASM_INTEGER.fullmatch(text):
        base = 16 if text.lstrip("+-").startswith("0x") else 10
        value = int(text, base)
        if not ir.INT_MIN <= value <= 0xFFFFFFFF:
            raise at.error("литерал выходит за пределы 32-битного операнда NASM")
        return ir.Literal(at, signed_register_value(value))
    raise at.error("ожидается 32-битный регистр или целое число NASM")


def parse_assembly(source: str) -> ir.Program:
    body = []
    registers = {}
    entry = None
    exported = None
    finished = False
    for number, line in enumerate(source.splitlines(), 1):
        at = ir.Position(number, len(line) - len(line.lstrip()) + 1)
        text = line.split(";", 1)[0].strip()
        if not text:
            continue
        if not body and re.fullmatch(r"(?:bits\s+64|section\s+\.text)", text, re.I):
            continue
        if not body and exported is None and (match := re.fullmatch(r"global\s+([A-Za-z_]\w*)", text, re.I)):
            exported = match[1]
            continue
        if not body and entry is None and (match := re.fullmatch(r"([A-Za-z_]\w*):", text)):
            entry = match[1]
            continue
        if finished:
            raise at.error("после ret не должно быть инструкций")
        if text.lower() == "ret":
            finished = True
            continue
        parts = text.lower().split(None, 1)
        instruction = parts[0]
        operands = [part.strip() for part in parts[1].split(",")] if len(parts) == 2 else []
        if instruction in {"neg", "inc", "dec"} and len(operands) == 1 and ASM_REGISTER.fullmatch(operands[0]):
            register = operands[0]
            operand = ir.Name(at, register)
            value = (unary(at, "-", operand) if instruction == "neg" else
                     ir.Binary(at, "+" if instruction == "inc" else "-", operand, ir.Literal(at, 1)))
        elif instruction == "xor" and len(operands) == 2 and operands[0] == operands[1] and ASM_REGISTER.fullmatch(operands[0]):
            register = operands[0]
            value = ir.Literal(at, 0)
        elif instruction in {"mov", *ASM_BINARY} and len(operands) == 2 and ASM_REGISTER.fullmatch(operands[0]):
            register = operands[0]
            value = asm_operand(operands[1], at)
            if instruction in ASM_BINARY:
                value = ir.Binary(at, ASM_BINARY[instruction], ir.Name(at, register), value)
        else:
            raise at.error("ожидается простая операция NASM над 32-битными регистрами, без обращений к памяти")
        exact = constant(value, registers, checked=False)
        wrapped = signed_register_value(exact)
        registers[register] = wrapped
        body.append(ir.Bind(at, register, value if exact == wrapped else ir.Literal(at, wrapped), "int"))
    end = ir.Position(max(1, len(source.splitlines())), 1)
    if exported and exported != entry:
        raise end.error("имя global должно совпадать с меткой функции")
    # A bare instruction fragment displays EAX; a labelled routine returns it.
    value = ir.Name(end, "eax")
    if entry:
        if not finished:
            raise end.error("закончите функцию NASM инструкцией ret")
        function = ir.Function(end, entry, (), "int", (*body, ir.Return(end, value)))
        return ir.Program("assembly", (function,), main=entry if entry == "main" else None)
    return ir.Program("assembly", (), (*body, ir.Print(end, (value,), "", "\n", "lower")))


def parse_scalar(source: str, language: str) -> ir.Program:
    return SqlParser(source).parse() if language == "sql" else parse_assembly(source)


def signed_register_value(value: int) -> int:
    return (value - ir.INT_MIN) % (1 << 32) + ir.INT_MIN


def constant(node: ir.Expr, values: dict, analysis: ir.Analysis | None = None, *, checked: bool = True) -> int | str:
    match node:
        case ir.Literal(value=value) if type(value) in {int, str}:
            pass
        case ir.Name(name=name):
            key = analysis.references[id(node)] if analysis is not None else name
            if key not in values:
                raise node.at.error(f"значение {name} не определено в скалярном выражении")
            value = values[key]
        case ir.Unary(operator=op, operand=operand) if op in {"+", "-"}:
            number = constant(operand, values, analysis, checked=checked)
            if type(number) is not int:
                raise node.at.error("знак применяется к целому числу")
            value = number if op == "+" else -number
        case ir.Binary(operator=op, left=left, right=right):
            a, b = (constant(part, values, analysis, checked=checked) for part in (left, right))
            if type(a) is not int or type(b) is not int:
                raise node.at.error("арифметика поддерживается для целых чисел")
            if op in INT_BINARY:
                value = INT_BINARY[op](a, b)
            elif op in {"tdiv", "tmod", "fdiv", "fmod"}:
                if b == 0:
                    raise node.at.error("деление на ноль в скалярном выражении")
                quotient = a // b if op.startswith("f") else abs(a) // abs(b) * (-1 if (a < 0) != (b < 0) else 1)
                value = quotient if op.endswith("div") else a - b * quotient
            else:
                raise node.at.error("эта операция вне скалярного профиля")
        case _:
            raise node.at.error("ожидается вычисление из целых чисел или строковых значений")
    if checked and type(value) is int and not ir.INT_MIN <= value <= ir.INT_MAX:
        raise node.at.error("результат вычисления вне диапазона int32")
    return value


def scalar_output(analysis: ir.Analysis) -> tuple[tuple[ir.Bind, ...], ir.Expr]:
    program = analysis.program
    statements = program.body
    if program.main and len(program.functions) == 1 and not statements:
        statements = program.functions[0].body
        if statements and isinstance(statements[-1], ir.Return):
            if statements[-1].value is not None and not (
                isinstance(statements[-1].value, ir.Literal) and statements[-1].value.value == 0
            ):
                raise statements[-1].at.error("скалярный перевод требует обычного выхода из main с кодом 0")
            statements = statements[:-1]
    elif program.functions:
        raise program.functions[0].at.error("скалярный перевод поддерживает прямые вычисления без вызовов функций")
    if statements and isinstance(statements[-1], ir.Print):
        output = statements[-1]
        if len(output.values) == 1 and output.ending == "\n" and not output.stderr:
            if all(isinstance(node, ir.Bind) for node in statements[:-1]):
                return statements[:-1], output.values[0]
    at = statements[0].at if statements else ir.Position(1, 1)
    raise at.error("ожидаются присваивания и один вывод значения в конце")


def render_sql(analysis: ir.Analysis) -> str:
    binds, output = scalar_output(analysis)
    values = {}
    names = {}

    def expression(node: ir.Expr) -> str:
        match node:
            case ir.Literal(value=value):
                return "'" + value.replace("'", "''") + "'" if isinstance(value, str) else str(value)
            case ir.Name():
                return names[analysis.references[id(node)]]
            case ir.Unary(operator=op, operand=operand):
                return f"({op}({expression(operand)}))"
            case ir.Binary(operator=op, left=left, right=right):
                symbol = "/" if op.endswith("div") else "%" if op.endswith("mod") else op
                rendered = f"({expression(left)} {symbol} {expression(right)})"
                if op in {"fdiv", "fmod"}:
                    a, b = (constant(part, values, analysis) for part in (left, right))
                    if (a < 0) != (b < 0) and a % b != 0:
                        correction = "- 1" if op == "fdiv" else f"+ {expression(right)}"
                        rendered = f"({rendered} {correction})"
                return rendered
        raise AssertionError(node)

    ctes = []
    for index, bind in enumerate(binds, 1):
        variable = analysis.references[id(bind)]
        name = f"v{index}"
        value = constant(bind.value, values, analysis)
        entry = expression(bind.value)
        select = f"SELECT *, {entry} AS {name} FROM step{index - 1}" if index > 1 else f"SELECT {entry} AS {name}"
        ctes.append(f"step{index} AS ({select})")
        values[variable] = value
        names[variable] = name
    constant(output, values, analysis)
    prefix = "WITH " + ",\n     ".join(ctes) + "\n" if ctes else ""
    suffix = f" FROM step{len(ctes)}" if ctes else ""
    return f"{prefix}SELECT {expression(output)} AS result{suffix};\n"


def render_assembly(analysis: ir.Analysis) -> str:
    binds, output = scalar_output(analysis)
    slots = {}
    values = {}
    size = ((len(binds) * 4 + 15) // 16) * 16
    lines = [
        "; NASM x86-64, Linux SysV. Assemble: nasm -f elf64 file.asm. Link: cc file.o -o program.",
        "bits 64", "default rel", "global main", "extern printf", "section .rodata",
        "result_format: db '%d', 10, 0", "section .text", "main:",
        "    push rbp", "    mov rbp, rsp",
    ]
    if size:
        lines.append(f"    sub rsp, {size}")

    def expression(node: ir.Expr) -> None:
        match node:
            case ir.Literal(value=value) if type(value) is int:
                lines.append(f"    mov eax, {value}")
            case ir.Name():
                offset = slots[analysis.references[id(node)]]
                lines.append(f"    mov eax, [rbp - {offset}]")
            case ir.Unary(operator=op, operand=operand) if op in {"+", "-"}:
                expression(operand)
                if op == "-":
                    lines.append("    neg eax")
            case ir.Binary(operator=op, left=left, right=right) if op in INT_BINARY:
                expression(left)
                lines.append("    push rax")
                expression(right)
                lines.extend(("    mov ecx, eax", "    pop rax"))
                instruction = {"+": "add", "-": "sub", "*": "imul"}[op]
                lines.append(f"    {instruction} eax, ecx")
            case _:
                raise node.at.error("вывод NASM поддерживает целые числа и операции +, -, *")

    for index, bind in enumerate(binds, 1):
        value = constant(bind.value, values, analysis)
        if type(value) is not int:
            raise bind.at.error("вывод NASM поддерживает целочисленные присваивания")
        expression(bind.value)
        variable = analysis.references[id(bind)]
        offset = index * 4
        slots[variable] = offset
        values[variable] = value
        lines.append(f"    mov [rbp - {offset}], eax")
    if type(constant(output, values, analysis)) is not int:
        raise output.at.error("вывод NASM поддерживает одно целое число")
    expression(output)
    lines.extend(("    mov esi, eax", "    lea rdi, [rel result_format]", "    xor eax, eax",
                  "    call printf wrt ..plt", "    xor eax, eax", "    leave", "    ret",
                  "section .note.GNU-stack noalloc noexec nowrite progbits"))
    return "\n".join(lines) + "\n"
