import itertools

from contextlib import contextmanager
from typing import (
    Any,
    Dict,
    Generator,
    Iterable,
    List,
    Optional,
    Tuple,
    Union,
    cast,
)

from mypy.errors import Errors
from mypy.nodes import (
    AssignmentStmt,
    CallExpr,
    ClassDef,
    Decorator,
    EllipsisExpr,
    FuncDef,
    IfStmt,
    ImportBase,
    MemberExpr,
    MypyFile,
    NameExpr,
    Node,
    Statement,
    SymbolTable,
    TempNode,
    TypeInfo,
    Var,
)

MYPY = False
if MYPY:
    from typing import Type


class MergeFiles:
    """this is a class that allows merge-ing a stub MypyFile into a source MypyFile"""

    def __init__(
        self, src: MypyFile, stub: MypyFile, errors: Errors, src_xpath: str, src_id: str
    ) -> None:
        """ create the class that can perfrom the merge operation

        :param src: the source MypyFile
        :param stub: the stub MypyFile
        :param errors: the place to reports errors during the merge
        :param src_xpath: reports errors for this file (should be the source file)
        :param src_id: reports errors for this module (should be the source module)
        """
        self.src = src  # type: MypyFile
        self.stub = stub  # type: MypyFile
        self.errors = errors  # type: Errors
        self.errors.set_file(src_xpath, src_id)
        self._line = -1  # type: int
        self._column = 0  # type: int

    def run(self) -> None:
        """performs the merge operation"""
        self.merge_symbol_tables(self.src.names, self.stub.names)
        self.enrich_src_defs_with_stub_only()

    def report(
        self, msg: str, line: Optional[int] = None, column: Optional[int] = None
    ) -> None:
        """report an error

        :param msg: the message to report
        :param line: the line number where the message is at (by default use last \
        known position during the merge)
        :param column: the column where the message is at (by default use last known)
        """
        if line is None:
            line = self._line
        if column is None:
            column = self._column
        self.errors.report(line=line, message=msg, column=column)

    def merge_symbol_tables(self, src: SymbolTable, stub: SymbolTable) -> None:
        """merge every element from the stub into the source file"""
        for stub_symbol_name, stub_symbol in stub.items():
            if stub_symbol_name not in src:
                src[stub_symbol_name] = stub_symbol
            else:
                src_symbol = src[stub_symbol_name]
                if src_symbol is not stub_symbol:
                    self.merge_nodes(src_symbol.node, stub_symbol.node)

    @contextmanager
    def report_from_element(self, src: Any) -> Generator[None, None, None]:
        """changes the current report location to the given source location"""
        old_line, old_column = self._line, self._column
        line = getattr(src, "line", old_line)
        column = getattr(src, "line", old_column)
        self._line, self._column = line, column
        try:
            yield
        finally:
            self._line, self._column = old_line, old_column

    def merge_nodes(self, src: Any, stub: Any) -> None:
        """merge a given stub symbol into a source symbol"""
        with self.report_from_element(src):
            node_type = type(src)
            if node_type != type(stub):
                self.report(
                    "conflict of src {} and stub:{} {} definition".format(
                        src.__class__.__qualname__,
                        stub.line,
                        stub.__class__.__qualname__,
                    )
                )
            elif isinstance(src, Var):
                self.merge_variables(src, stub)
            elif isinstance(src, AssignmentStmt):
                self.merge_assignment(src, stub)
            elif isinstance(src, FuncDef):
                self.merge_func_definition(src, stub)
            elif isinstance(src, TypeInfo):
                self.merge_type_info(src, stub)
            elif isinstance(src, Decorator):
                self.merge_decorators(src, stub)
            else:
                raise RuntimeError(
                    "cannot merge {!r} with {!r}".format(src, stub)
                )  # pragma: no cover

    @staticmethod
    def merge_variables(src: Var, stub: Var) -> None:
        """merge a variable definition"""
        src.type = stub.type

    def merge_assignment(self, src: AssignmentStmt, stub: AssignmentStmt) -> None:
        """merge class variables"""
        # note here we don't need to check name matching as the class merge logic
        # already made sure they match
        src.type = stub.type
        src.unanalyzed_type = stub.unanalyzed_type
        stub_name = cast(str, self.simple_assignment_name(stub))
        self.check_no_explicit_default(stub.rvalue, stub, stub_name)

    def merge_func_definition(self, src: FuncDef, stub: FuncDef) -> None:
        """merge a function definition"""
        if src.arg_names == stub.arg_names:
            src.type = stub.type
            src.arg_kinds = stub.arg_kinds
            src.unanalyzed_type = stub.unanalyzed_type
        else:
            self.report(
                "arg conflict of src {} and stub (line {}) {}".format(
                    repr(src.arg_names), stub.line, repr(stub.arg_names)
                )
            )

    def merge_type_info(self, src: TypeInfo, stub: TypeInfo) -> None:
        """merge a class definition"""
        src.type_vars = stub.type_vars
        src.metaclass_type = stub.metaclass_type
        src.runtime_protocol = stub.runtime_protocol
        self.merge_symbol_tables(src.names, stub.names)
        src.defn.type_vars = stub.defn.type_vars

        stub_assigns, stub_funcs = self.collect_assign_and_funcs(stub.defn.defs.body)
        # merge stub entries into source
        for entry in src.defn.defs.body:
            if isinstance(entry, FuncDef):
                self.merge_class_func_def(entry, stub_assigns, stub_funcs)
            elif isinstance(entry, AssignmentStmt):
                self.merge_class_assignment(entry, stub_assigns)
            elif isinstance(entry, Decorator):
                self.merge_class_decorator(entry, stub_funcs)

        # report extra stub entries
        for k, v in stub_assigns.items():
            self.report("no source for assign {} @stub:{}".format(k, v.line))
        for f_k, f_v in stub_funcs.items():
            self.report("no source for func {} @stub:{}".format(f_k, f_v.line))

    def merge_class_decorator(
        self, src: Decorator, stub_funcs: Dict[str, Union[Decorator, FuncDef]]
    ) -> None:
        """merge class decorator, if stub not found"""
        name = src.func.name()
        if name in stub_funcs:
            self.merge_nodes(src, stub_funcs[name])
            del stub_funcs[name]

    def merge_class_assignment(
        self, src: AssignmentStmt, stubs: Dict[str, AssignmentStmt]
    ) -> None:
        """merge class variables"""
        src_name = self.simple_assignment_name(src)
        if src_name is not None:
            if src_name in stubs:
                self.merge_nodes(src, stubs[src_name])
            del stubs[src_name]

    def simple_assignment_name(
        self,
        node: AssignmentStmt,
        l_value_type: 'Type[Union[NameExpr, MemberExpr]]' = NameExpr,
        report: bool = True,
    ) -> Optional[str]:
        if len(node.lvalues) == 1:
            l_value = node.lvalues[0]
            if isinstance(l_value, l_value_type):
                return cast(Union[NameExpr, MemberExpr], l_value).name
            else:
                if report:
                    self.report(
                        "l-values must be simple name expressions, is {}".format(
                            type(l_value).__qualname__
                        )
                    )
        elif report:  # pragma: no cover
            # TODO: how can we have more than one l-value in an assignment
            self.report("assignment has more than one l-values")  # pragma: no cover
        return None

    def merge_class_func_def(
        self,
        src: FuncDef,
        stub_assigns: Dict[str, AssignmentStmt],
        stub_funcs: Dict[str, Union[Decorator, FuncDef]],
    ) -> None:
        """merge the function nodes and if it's a class constructor try to enrich
        self assignments from the class variable type hints
        """
        name = src.name()
        if name in stub_funcs:
            self.merge_nodes(src, stub_funcs[name])
            del stub_funcs[name]
        if name == "__init__":
            for init_part in src.body.body:
                # we support either direct assignment, or assignment within
                # if statements
                if isinstance(init_part, AssignmentStmt):
                    self.enrich_src_assign_from_stub(init_part, stub_assigns)
                elif isinstance(init_part, IfStmt):
                    for b in init_part.body:
                        for part in b.body:
                            if isinstance(part, AssignmentStmt):
                                self.enrich_src_assign_from_stub(part, stub_assigns)

    def collect_assign_and_funcs(
        self, body: Iterable[Node]
    ) -> Tuple[Dict[str, AssignmentStmt], Dict[str, Union[Decorator, FuncDef]]]:
        """collect assignments and stubs from body"""
        funcs = {}  # type: Dict[str, Union[Decorator, FuncDef]]
        assigns = {}  # type: Dict[str, AssignmentStmt]
        for entry in body:
            if isinstance(entry, AssignmentStmt):
                name = self.simple_assignment_name(entry)
                if name is not None:
                    assigns[name] = entry
            elif isinstance(entry, Decorator):
                funcs[entry.func.name()] = entry
            elif isinstance(entry, FuncDef):
                funcs[entry.name()] = entry
        return assigns, funcs

    def merge_decorators(self, src: Decorator, stub: Decorator) -> None:
        """merge decorators above functions"""
        for l, r in itertools.zip_longest(src.decorators, stub.decorators):
            if type(l) != type(r):
                self.report(
                    "conflict of src {} and stub {} decorator".format(
                        l.__class__.__qualname__, r.__class__.__qualname__
                    )
                )
                break
            if isinstance(l, NameExpr):
                if not self.decorator_name_check(l, r):
                    break
            elif isinstance(l, CallExpr):
                if isinstance(l.callee, NameExpr) and isinstance(r.callee, NameExpr):
                    if not self.decorator_name_check(l.callee, r.callee):
                        break
                    self.decorator_argument_checks(l, r)
        else:
            self.merge_nodes(src.func, stub.func)

    def decorator_argument_checks(self, src: CallExpr, stub: CallExpr) -> None:
        """check decorator arguments"""
        for l_arg, r_arg in itertools.zip_longest(src.arg_names, stub.arg_names):
            if l_arg != r_arg:
                self.report(
                    "conflict of src {} and stub {} decorator argument name".format(
                        l_arg, r_arg
                    ),
                    line=src.line,
                )
                break
        for name, default_node in zip(stub.arg_names, stub.args):
            self.check_no_explicit_default(default_node, src, cast(str, name))

    def check_no_explicit_default(
        self, default_node: Node, node: Node, name: str
    ) -> None:
        """check that no default value is set for this node"""
        if not isinstance(node, TempNode) and not isinstance(
            default_node, EllipsisExpr
        ):
            self.report(
                (
                    "stub should not contain default value, {} has {}".format(
                        name, type(default_node).__name__
                    )
                )
            )

    def decorator_name_check(self, src: NameExpr, stub: NameExpr) -> bool:
        """check if the decorator name from source and stub match

        :return: True if the names match
        """
        if src.name != stub.name:
            self.report(
                "conflict of src {} and stub {} decorator name".format(
                    src.name, stub.name
                )
            )
            return False
        return True

    def enrich_src_assign_from_stub(
        self, src_assign: AssignmentStmt, stub_assign: Dict[str, AssignmentStmt]
    ) -> bool:
        """try to match a source assignment against existing stub assignment

        :return: True, if we found a matching stub assignment
        """
        name = self.simple_assignment_name(
            src_assign, l_value_type=MemberExpr, report=False
        )
        if name is not None:
            if name in stub_assign:
                src_assign.type = stub_assign[name].type
                src_assign.unanalyzed_type = stub_assign[name].unanalyzed_type
                del stub_assign[name]
                return True
            else:
                self.report("no stub definition for class member {}".format(name))
        return False

    def enrich_src_defs_with_stub_only(self) -> None:
        """
        There are definitions that are needed to evaluate the source, which are not
        present in the source file, only the stub:
        - imports from the stub file (this help resolve those types)
        - type aliases (take form of assignment)
        - protocol definitions (in form of a class definition)

        Here we copy them over into the source definitions.
        """
        src_definitions = self.source_definitions

        stub_definitions = []  # type: List[Statement]
        for i in self.stub.defs:
            if isinstance(i, ImportBase):
                stub_definitions.append(i)
            elif isinstance(i, ClassDef):
                if i.name not in src_definitions:
                    stub_definitions.append(i)
            elif isinstance(i, AssignmentStmt) and len(i.lvalues) == 1:
                entry = i.lvalues[0]
                if isinstance(entry, NameExpr):
                    name = entry.name
                    if name not in src_definitions:
                        # could be a type alias we add this
                        stub_definitions.append(i)
                    else:
                        # merge it into the source data
                        src_assignment = src_definitions[name]
                        if isinstance(src_assignment, AssignmentStmt):
                            src_assignment.type = i.type
                            src_assignment.unanalyzed_type = i.unanalyzed_type
                            del src_definitions[name]

        # stub imports are available for source
        self.src.imports.extend(self.stub.imports)

        # we insert at start the stub definitions, this is important so source
        # definition evaluation have all stub definitions available
        self.src.defs = stub_definitions + self.src.defs

    @property
    def source_definitions(self) -> Dict[str, Union[ClassDef, AssignmentStmt]]:
        """collect source definitions that influence the symbol table (without imports)
        """
        src_definitions = {}  # type: Dict[str, Union[ClassDef, AssignmentStmt]]
        for d in self.src.defs:
            if isinstance(d, ClassDef):
                src_definitions[d.name] = d
            elif isinstance(d, AssignmentStmt):
                for l in d.lvalues:
                    if isinstance(l, NameExpr):
                        src_definitions[l.name] = d
        return src_definitions
