"""Syntax and type checking shared by the portable translators."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal as Kind

Type = Kind["int", "bool", "str", "void"]
INT_MIN = -(2 ** 31)
INT_MAX = 2 ** 31 - 1


@dataclass(frozen=True)
class Position:
    line: int
    column: int

    def error(self, message: str) -> ValueError:
        return ValueError(f"{self.line}:{self.column}: {message}")


@dataclass(frozen=True)
class Expr:
    at: Position


@dataclass(frozen=True)
class Literal(Expr):
    value: int | bool | str


@dataclass(frozen=True)
class Name(Expr):
    name: str


@dataclass(frozen=True)
class Unary(Expr):
    operator: str
    operand: Expr


@dataclass(frozen=True)
class Binary(Expr):
    operator: str
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Choice(Expr):
    condition: Expr
    yes: Expr
    no: Expr


@dataclass(frozen=True)
class Call(Expr):
    name: str
    arguments: tuple[Expr, ...]


@dataclass(frozen=True)
class Stmt:
    at: Position


@dataclass(frozen=True)
class Bind(Stmt):
    name: str
    value: Expr
    annotation: Type | None = None
    declaration: bool = False
    constant: bool = False


@dataclass(frozen=True)
class If(Stmt):
    condition: Expr
    body: tuple[Stmt, ...]
    otherwise: tuple[Stmt, ...]


@dataclass(frozen=True)
class Loop(Stmt):
    condition: Expr
    body: tuple[Stmt, ...]
    initial: tuple[Stmt, ...] = ()
    update: tuple[Stmt, ...] = ()


@dataclass(frozen=True)
class Return(Stmt):
    value: Expr | None


@dataclass(frozen=True)
class Print(Stmt):
    values: tuple[Expr, ...]
    separator: str
    ending: str
    boolean_style: str
    formats: tuple[str, ...] = ()
    stderr: bool = False


@dataclass(frozen=True)
class Invoke(Stmt):
    call: Call


@dataclass(frozen=True)
class Jump(Stmt):
    command: str


@dataclass(frozen=True)
class Parameter:
    at: Position
    name: str
    annotation: Type | None = None


@dataclass(frozen=True)
class Function:
    at: Position
    name: str
    parameters: tuple[Parameter, ...]
    returns: Type | None
    body: tuple[Stmt, ...]


@dataclass(frozen=True)
class Program:
    source: str
    functions: tuple[Function, ...]
    body: tuple[Stmt, ...] = ()
    main: str | None = None


SCALARS = frozenset({"int", "bool", "str"})


@dataclass(eq=False)
class TypeSlot:
    at: Position
    label: str
    possible: frozenset[Type] = SCALARS
    link: TypeSlot | None = field(default=None, repr=False)

    def root(self) -> TypeSlot:
        if self.link is not None:
            self.link = self.link.root()
            return self.link
        return self

    def constrain(self, types: set[Type] | frozenset[Type], at: Position) -> None:
        root = self.root()
        common = root.possible & types
        if not common:
            have, want = ", ".join(sorted(root.possible)), ", ".join(sorted(types))
            raise at.error(f"несовместимые типы для {self.label}: {have} и {want}")
        root.possible = common

    def merge(self, other: TypeSlot, at: Position) -> None:
        left, right = self.root(), other.root()
        if left is not right:
            left.constrain(right.possible, at)
            right.link = left

    def kind(self) -> Type:
        possible = self.root().possible
        if len(possible) != 1:
            raise self.at.error(f"не удалось вывести тип для {self.label}. Укажите тип или добавьте вызов с типизированным аргументом")
        return next(iter(possible))


@dataclass(eq=False)
class Variable:
    name: str
    slot: TypeSlot
    constant: bool = False


@dataclass
class Signature:
    parameters: tuple[Variable, ...]
    result: TypeSlot
    variables: list[Variable] = field(default_factory=list)


class Analysis:
    def __init__(self, program: Program):
        self.program = program
        self.functions: dict[str, Signature] = {}
        self.entry = Signature((), TypeSlot(Position(1, 1), "точки входа", frozenset({"void"})))
        self.expressions: dict[int, TypeSlot] = {}
        self.references: dict[int, Variable] = {}
        self.calls_in_values: list[Call] = []
        self.effects: dict[str, set[str]] = {}
        self.output_functions: set[str] = set()
        self.prints: list[Print] = []
        self.loop_fallthrough: set[int] = set()
        self.implicit_main_return = False

    def run(self) -> Analysis:
        for function in self.program.functions:
            if function.name in self.functions:
                raise function.at.error(f"повторное объявление функции {function.name}")
            params = tuple(Variable(parameter.name, TypeSlot(parameter.at, parameter.name,
                           frozenset({parameter.annotation}) if parameter.annotation else SCALARS))
                           for parameter in function.parameters)
            result_types = frozenset({function.returns}) if function.returns else SCALARS | {"void"}
            result = TypeSlot(function.at, f"результата {function.name}", result_types)
            self.functions[function.name] = Signature(params, result)
            self.effects[function.name] = set()

        for function in self.program.functions:
            signature = self.functions[function.name]
            if len({parameter.name for parameter in signature.parameters}) != len(signature.parameters):
                raise function.at.error("повторное имя параметра")
            reader = FunctionAnalysis(self, signature, function.name)
            _, falls_through = reader.block(function.body, set(signature.parameters), scoped=False)
            if falls_through:
                if self.program.source in {"c", "cpp"} and self.program.main == function.name:
                    self.implicit_main_return = True
                else:
                    signature.result.constrain({"void"}, function.at)
        FunctionAnalysis(self, self.entry, None).block(self.program.body, set(), scoped=False)
        for variable in self.entry.variables:
            if variable.name in self.functions:
                raise variable.slot.at.error(f"переменная {variable.name} перекрывает имя функции")
        for signature in (*self.functions.values(), self.entry):
            signature.result.kind()
            for variable in (*signature.parameters, *signature.variables):
                variable.slot.kind()
        for slot in self.expressions.values():
            slot.kind()
        if self.program.source in {"javascript", "typescript"}:
            for output in self.prints:
                if len(output.values) > 1 and not output.formats:
                    first = output.values[0]
                    if self.expressions[id(first)].kind() == "str" and not isinstance(first, Literal):
                        raise first.at.error("первый аргумент console.log с несколькими значениями может содержать формат %")

        effectful = set(self.output_functions)
        while additions := {name for name, calls in self.effects.items() if calls & effectful} - effectful:
            effectful.update(additions)
        for call in self.calls_in_values:
            if call.name in effectful:
                raise call.at.error(f"вызов {call.name} внутри выражения может печатать. Вызовите его отдельной инструкцией")
        return self


class FunctionAnalysis:
    def __init__(self, analysis: Analysis, signature: Signature, function: str | None):
        self.analysis = analysis
        self.signature = signature
        self.function = function
        self.python_scope = analysis.program.source in {"python", "ruby", "assembly"}
        self.javascript_scope = analysis.program.source in {"javascript", "typescript"}
        self.scopes = [{parameter.name: parameter for parameter in signature.parameters}]
        self.declarations: dict[int, Variable] = {}
        self.loops = 0

    def lookup(self, name: str) -> Variable | None:
        return next((scope[name] for scope in reversed(self.scopes) if name in scope), None)

    def expression(self, node: Expr, assigned: set[Variable], *, statement: bool = False) -> TypeSlot:
        result = TypeSlot(node.at, "выражения")
        match node:
            case Literal(value=value):
                kind = "bool" if type(value) is bool else "int" if type(value) is int else "str"
                if kind == "int" and not INT_MIN <= value <= INT_MAX:
                    raise node.at.error("целое число вне диапазона int32")
                if kind == "str" and ("\0" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
                    raise node.at.error("строка содержит NUL или непарный суррогат Unicode")
                result.constrain({kind}, node.at)
            case Name(name=name):
                variable = self.lookup(name)
                if variable is None or variable not in assigned:
                    raise node.at.error(f"переменная {name} не определена на этом пути")
                self.analysis.references[id(node)] = variable
                result = variable.slot
            case Unary(operator=op, operand=operand):
                value = self.expression(operand, assigned)
                if op == "!":
                    value.constrain(self.condition_types(), operand.at)
                    result.constrain({"bool"}, node.at)
                else:
                    value.constrain({"int"}, operand.at)
                    result.constrain({"int"}, node.at)
            case Binary(operator=op, left=left_node, right=right_node):
                left, right = self.expression(left_node, assigned), self.expression(right_node, assigned)
                if op in {"==", "!="}:
                    if self.analysis.program.source in {"java", "c", "cpp"}:
                        left.constrain({"int", "bool"}, left_node.at)
                    left.merge(right, node.at)
                    result.constrain({"bool"}, node.at)
                elif op in {"&&", "||"}:
                    for operand in (left, right):
                        operand.constrain({"bool"}, node.at)
                    result.constrain({"bool"}, node.at)
                else:
                    if op == "floatdiv":
                        raise node.at.error("дробное деление вне переносимого профиля. Для целых используйте //, Math.trunc(a / b) или Math.floor(a / b)")
                    left.constrain({"int"}, left_node.at)
                    right.constrain({"int"}, right_node.at)
                    result.constrain({"bool"} if op in {"<", "<=", ">", ">="} else {"int"}, node.at)
            case Call(name=name, arguments=arguments):
                if self.lookup(name) is not None:
                    raise node.at.error(f"вызов локальной переменной {name} вне переносимого профиля")
                signature = self.analysis.functions.get(name)
                if signature is None:
                    raise node.at.error(f"нет переносимой реализации функции {name}")
                if len(arguments) != len(signature.parameters):
                    raise node.at.error(f"{name} ожидает {len(signature.parameters)} аргументов")
                for argument, parameter in zip(arguments, signature.parameters):
                    self.expression(argument, assigned).merge(parameter.slot, argument.at)
                if self.function is not None:
                    self.analysis.effects[self.function].add(name)
                if not statement:
                    signature.result.constrain(SCALARS, node.at)
                    self.analysis.calls_in_values.append(node)
                result = signature.result
            case Choice(condition=condition, yes=yes, no=no):
                self.expression(condition, assigned).constrain(self.condition_types(), condition.at)
                result.merge(self.expression(yes, assigned), yes.at)
                result.merge(self.expression(no, assigned), no.at)
            case _:
                raise AssertionError(node)
        self.analysis.expressions[id(node)] = result
        return result

    def condition_types(self) -> set[Type]:
        if self.analysis.program.source in {"python", "javascript", "typescript"}:
            return set(SCALARS)
        if self.analysis.program.source in {"java", "go", "csharp", "rust", "zig", "ruby"}:
            return {"bool"}
        return {"bool", "int"}

    def declare(self, node: Bind) -> Variable:
        existing = self.lookup(node.name) if self.analysis.program.source in {"java", "csharp"} else self.scopes[-1].get(node.name)
        if existing is not None:
            raise node.at.error(f"повторное объявление {node.name}")
        variable = Variable(node.name, TypeSlot(node.at, node.name), node.constant)
        self.scopes[-1][node.name] = variable
        self.signature.variables.append(variable)
        return variable

    def bind(self, node: Bind, assigned: set[Variable]) -> None:
        if node.declaration and not self.python_scope:
            if self.analysis.program.source == "go":
                value = self.expression(node.value, assigned)
                variable = self.declare(node)
            else:
                variable = self.declarations.get(id(node)) or self.declare(node)
                value = self.expression(node.value, assigned)
        else:
            value = self.expression(node.value, assigned)
            variable = self.lookup(node.name)
            if variable is None:
                if not self.python_scope:
                    raise node.at.error(f"присваивание необъявленной переменной {node.name}")
                variable = self.declare(node)
            elif variable.constant or (self.javascript_scope and variable not in assigned):
                raise node.at.error(f"нельзя присвоить значение {node.name} до объявления или изменить константу")
        if node.annotation:
            variable.slot.constrain({node.annotation}, node.at)
        variable.slot.merge(value, node.at)
        self.analysis.references[id(node)] = variable
        assigned.add(variable)

    def block(self, nodes: tuple[Stmt, ...], incoming: set[Variable], *, scoped: bool = True) -> tuple[set[Variable], bool]:
        assigned = set(incoming)
        nested = scoped and not self.python_scope
        if nested:
            self.scopes.append({})
        if self.javascript_scope:
            for node in nodes:
                if isinstance(node, Bind) and node.declaration:
                    self.declarations[id(node)] = self.declare(node)
        falls_through = True
        for node in nodes:
            if not falls_through:
                raise node.at.error("переносимый профиль требует достижимых инструкций после перехода")
            match node:
                case Bind():
                    self.bind(node, assigned)
                case If(condition=test, body=body, otherwise=otherwise):
                    self.expression(test, assigned).constrain(self.condition_types(), test.at)
                    left, left_continues = self.block(body, assigned)
                    right, right_continues = self.block(otherwise, assigned)
                    falls_through = left_continues or right_continues
                    if left_continues and right_continues:
                        assigned = left & right
                    elif falls_through:
                        assigned = left if left_continues else right
                case Loop(condition=test, body=body, initial=initial, update=update):
                    if not self.python_scope:
                        self.scopes.append({})
                    loop_assigned, _ = self.block(initial, assigned, scoped=False)
                    self.expression(test, loop_assigned).constrain(self.condition_types(), test.at)
                    self.loops += 1
                    _, body_continues = self.block(body, loop_assigned)
                    if body_continues:
                        self.analysis.loop_fallthrough.add(id(node))
                    self.loops -= 1
                    self.block(update, loop_assigned, scoped=False)
                    if not self.python_scope:
                        self.scopes.pop()
                case Return(value=value):
                    if self.function is None:
                        raise node.at.error("return вне функции")
                    if value is None:
                        self.signature.result.constrain({"void"}, node.at)
                    else:
                        self.signature.result.constrain(SCALARS, node.at)
                        self.signature.result.merge(self.expression(value, assigned), node.at)
                    falls_through = False
                case Print(values=values, formats=formats):
                    self.analysis.prints.append(node)
                    if self.function is not None:
                        self.analysis.output_functions.add(self.function)
                    for index, expression in enumerate(values):
                        slot = self.expression(expression, assigned)
                        if formats and formats[index] != "*":
                            allowed = {"d": {"int", "bool"}, "D": {"int"}, "s": {"str"}}[formats[index]]
                            slot.constrain(allowed, expression.at)
                case Invoke(call=call):
                    self.expression(call, assigned, statement=True)
                case Jump():
                    if not self.loops:
                        raise node.at.error(f"{node.command} вне цикла")
                    falls_through = False
                case _:
                    raise AssertionError(node)
        if nested:
            assigned.difference_update(self.scopes.pop().values())
        return assigned, falls_through
