"""Syntax and type checking shared by the portable translators."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal as Kind

Type = Kind["int", "bool", "str", "int_list", "void"]
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
class IntList(Expr):
    values: tuple[Expr, ...]


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
class Append(Stmt):
    target: Name
    value: Expr


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
VALUES = SCALARS | {"int_list"}


@dataclass(eq=False)
class TypeSlot:
    at: Position
    label: str
    possible: frozenset[Type] = VALUES
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
            raise at.error(f"incompatible types for {self.label}: {have} and {want}")
        root.possible = common

    def merge(self, other: TypeSlot, at: Position) -> None:
        left, right = self.root(), other.root()
        if left is not right:
            left.constrain(right.possible, at)
            right.link = left

    def kind(self) -> Type:
        possible = self.root().possible
        if len(possible) != 1:
            raise self.at.error(f"could not infer the type of {self.label}. Add an annotation or a call with a typed argument")
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
        self.entry = Signature((), TypeSlot(Position(1, 1), "the entry point", frozenset({"void"})))
        self.expressions: dict[int, TypeSlot] = {}
        self.references: dict[int, Variable] = {}
        self.calls_in_values: list[Call] = []
        self.effects: dict[str, set[str]] = {}
        self.output_functions: set[str] = set()
        self.list_writers: set[str] = set()
        self.prints: list[Print] = []
        self.loop_fallthrough: set[int] = set()
        self.implicit_main_return = False

    def run(self) -> Analysis:
        for function in self.program.functions:
            if function.name in self.functions:
                raise function.at.error(f"duplicate declaration of function {function.name}")
            params = tuple(Variable(parameter.name, TypeSlot(parameter.at, parameter.name,
                           frozenset({parameter.annotation}) if parameter.annotation else VALUES))
                           for parameter in function.parameters)
            result_types = frozenset({function.returns}) if function.returns else VALUES | {"void"}
            result = TypeSlot(function.at, f"the return value of {function.name}", result_types)
            self.functions[function.name] = Signature(params, result)
            self.effects[function.name] = set()

        for function in self.program.functions:
            signature = self.functions[function.name]
            if len({parameter.name for parameter in signature.parameters}) != len(signature.parameters):
                raise function.at.error("duplicate parameter name")
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
                raise variable.slot.at.error(f"variable {variable.name} shadows a function name")
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
                        raise first.at.error("the first argument of a multi-argument console.log may contain a % format specifier")

        effectful = self.output_functions | {
            name for name in self.list_writers
            if any(parameter.slot.kind() == "int_list" for parameter in self.functions[name].parameters)
        }
        while additions := {name for name, calls in self.effects.items() if calls & effectful} - effectful:
            effectful.update(additions)
        for call in self.calls_in_values:
            if call.name in effectful:
                raise call.at.error(f"calling {call.name} inside an expression may produce output or modify a list. Call it as a separate statement")
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
        result = TypeSlot(node.at, "the expression")
        match node:
            case Literal(value=value):
                kind = "bool" if type(value) is bool else "int" if type(value) is int else "str"
                if kind == "int" and not INT_MIN <= value <= INT_MAX:
                    raise node.at.error("integer is outside the int32 range")
                if kind == "str" and ("\0" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
                    raise node.at.error("string contains NUL or an unpaired Unicode surrogate")
                result.constrain({kind}, node.at)
            case IntList(values=values):
                for value in values:
                    self.expression(value, assigned).constrain({"int"}, value.at)
                result.constrain({"int_list"}, node.at)
            case Name(name=name):
                variable = self.lookup(name)
                if variable is None or variable not in assigned:
                    raise node.at.error(f"variable {name} is not defined on this path")
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
                    left.constrain(SCALARS, left_node.at)
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
                        raise node.at.error("floating-point division is outside the portable profile. For integer division, use //, Math.trunc(a / b), or Math.floor(a / b)")
                    left.constrain({"int"}, left_node.at)
                    right.constrain({"int"}, right_node.at)
                    result.constrain({"bool"} if op in {"<", "<=", ">", ">="} else {"int"}, node.at)
            case Call(name=name, arguments=arguments):
                if self.lookup(name) is not None:
                    raise node.at.error(f"calling local variable {name} is outside the portable profile")
                signature = self.analysis.functions.get(name)
                if signature is None:
                    raise node.at.error(f"function {name} has no portable implementation")
                if len(arguments) != len(signature.parameters):
                    raise node.at.error(f"{name} expects {len(signature.parameters)} arguments")
                for argument, parameter in zip(arguments, signature.parameters):
                    self.expression(argument, assigned).merge(parameter.slot, argument.at)
                if self.function is not None:
                    self.analysis.effects[self.function].add(name)
                if not statement:
                    signature.result.constrain(VALUES, node.at)
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
            raise node.at.error(f"duplicate declaration of {node.name}")
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
                    raise node.at.error(f"assignment to undeclared variable {node.name}")
                variable = self.declare(node)
            elif variable.constant or (self.javascript_scope and variable not in assigned):
                raise node.at.error(f"cannot assign to {node.name} before declaration or modify a constant")
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
                raise node.at.error("statements after a jump must be reachable in the portable profile")
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
                        raise node.at.error("return outside a function")
                    if value is None:
                        self.signature.result.constrain({"void"}, node.at)
                    else:
                        self.signature.result.constrain(VALUES, node.at)
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
                case Append(target=target, value=value):
                    self.expression(target, assigned).constrain({"int_list"}, target.at)
                    self.expression(value, assigned).constrain({"int"}, value.at)
                    if self.function is not None:
                        self.analysis.list_writers.add(self.function)
                case Jump():
                    if not self.loops:
                        raise node.at.error(f"{node.command} outside a loop")
                    falls_through = False
                case _:
                    raise AssertionError(node)
        if nested:
            assigned.difference_update(self.scopes.pop().values())
        return assigned, falls_through
