from fparser.two import Fortran2003
from fparser.two.utils import get_child, walk

from loki.frontend import Frontend, Source, extract_source
from loki.frontend.omni import parse_omni_ast, parse_omni_source
from loki.frontend.ofp import parse_ofp_ast, parse_ofp_source
from loki.frontend.fparser import parse_fparser_ast, parse_fparser_source, extract_fparser_source
from loki.backend.fgen import fgen
from loki.ir import (
    Declaration, Allocation, Import, Section, CallStatement,
    CallContext, Intrinsic, Interface, Comment, CommentBlock, Pragma
)
from loki.expression import FindVariables, FindTypedSymbols, Array, SubstituteExpressions
from loki.logging import warning, debug
from loki.pragma_utils import is_loki_pragma, pragmas_attached, process_dimension_pragmas
from loki.visitors import FindNodes, Transformer
from loki.tools import as_tuple, flatten, CaseInsensitiveDict
from loki.types import Scope, BasicType, ProcedureType, SymbolAttributes


__all__ = ['Subroutine']


class Subroutine:
    """
    Class to handle and manipulate a single subroutine.

    Parameters
    ----------
    name : str
        Name of the subroutine.
    args : iterable of str, optional
        The names of the dummy args.
    docstring : tuple of :any:`Node`, optional
        The subroutine docstring in the original source.
    spec : :any:`Section`, optional
        The spec of the subroutine.
    body : :any:`Section`, optional
        The body of the subroutine.
    members : iterable of :any:`Subroutine`
        Member subroutines contained in this subroutine.
    ast : optional
        Frontend node for this subroutine (from parse tree of the frontend).
    bind : optional
        Bind information (e.g., for Fortran ``BIND(C)`` annotation).
    is_function : bool, optional
        Flag to indicate this is a function instead of subroutine
        (in the Fortran sense). Defaults to `False`.
    scope : :any:`Scope`, optional
        The container that manages the symbol table that caches type
        information for all symbols defined within this subroutine's scope.
        If not provided an empty scope is created.
    parent_scope : :any:`Scope`, optional
        Object with type information for the enclosing scope that is given as
        parent to a newly created `scope`. This is also used to define the
        backward link via :py:attr:`Subroutine.parent`.
    rescope_variables : bool, optional
        Ensure that the type information for all :any:`TypedSymbol` in the
        subroutine's IR exist in the subroutine's scope or the scope's parents.
        Defaults to `False`.
    source : :any:`Source`
        Source object representing the raw source string information from the
        read file.
    """

    def __init__(self, name, args=None, docstring=None, spec=None, body=None, members=None,
                 ast=None, bind=None, is_function=False, scope=None, parent_scope=None,
                 rescope_variables=False, source=None):
        self.name = name
        self._ast = ast
        self._dummies = as_tuple(a.lower() for a in as_tuple(args))  # Order of dummy arguments
        self._source = source

        # Ensure we always have a local scope, and register ourselves with it
        self._scope = Scope(parent=parent_scope) if scope is None else scope
        self.scope.defined_by = self

        # The primary IR components
        self.docstring = as_tuple(docstring)
        assert isinstance(spec, Section) or spec is None
        self.spec = spec
        assert isinstance(body, Section) or body is None
        self.body = body
        self._members = as_tuple(members)

        if rescope_variables:
            self.rescope_variables()

        self.bind = bind
        self.is_function = is_function

        # Register this procedure in the parent scope
        if self.scope.parent:
            self.scope.parent.symbols[self.name] = SymbolAttributes(ProcedureType(procedure=self))

        with pragmas_attached(self, Declaration):
            self.spec = process_dimension_pragmas(self.spec)

    @staticmethod
    def _infer_allocatable_shapes(spec, body):
        """
        Infer variable symbol shapes from allocations of ``allocatable`` arrays.
        """
        alloc_map = {}
        for alloc in FindNodes(Allocation).visit(body):
            for v in alloc.variables:
                if isinstance(v, Array):
                    if alloc.data_source:
                        alloc_map[v.name.lower()] = alloc.data_source.type.shape
                    else:
                        alloc_map[v.name.lower()] = v.dimensions.index_tuple
        vmap = {}
        for v in FindVariables().visit(body):
            if v.name.lower() in alloc_map:
                vtype = v.type.clone(shape=alloc_map[v.name.lower()])
                vmap[v] = v.clone(type=vtype)
        smap = {}
        for v in FindVariables().visit(spec):
            if v.name.lower() in alloc_map:
                vtype = v.type.clone(shape=alloc_map[v.name.lower()])
                smap[v] = v.clone(type=vtype)
        return (SubstituteExpressions(smap, invalidate_source=False).visit(spec),
                SubstituteExpressions(vmap, invalidate_source=False).visit(body))

    @classmethod
    def from_source(cls, source, definitions=None, xmods=None, frontend=Frontend.FP):
        """
        Create ``Subroutine`` entry node from raw source string using given frontend.

        :param source: Fortran source string
        :param typdedefs: Derived-type definitions from external modules
        :param xmods: Locations of "xmods" module directory for OMNI frontend
        :param frontend: Choice of frontend to use for parsing source (default FP)
        """
        # TODO: Enable pre-processing on-the-fly
        if frontend == Frontend.OMNI:
            ast = parse_omni_source(source, xmods=xmods)
            typetable = ast.find('typeTable')
            f_ast = ast.find('globalDeclarations/FfunctionDefinition')
            return cls.from_omni(ast=f_ast, raw_source=source, typetable=typetable, definitions=definitions)

        if frontend == Frontend.OFP:
            ast = parse_ofp_source(source)
            f_ast = [r for r in list(ast.find('file')) if r.tag in ('subroutine', 'function')].pop()
            return cls.from_ofp(ast=f_ast, raw_source=source, definitions=definitions)

        if frontend == Frontend.FP:
            ast = parse_fparser_source(source)
            routine_types = (Fortran2003.Subroutine_Subprogram, Fortran2003.Function_Subprogram)
            f_ast = [r for r in ast.content if isinstance(r, routine_types)].pop()
            return cls.from_fparser(ast=f_ast, raw_source=source, definitions=definitions)

        raise NotImplementedError('Unknown frontend: %s' % frontend)

    @classmethod
    def from_ofp(cls, ast, raw_source, name=None, definitions=None, pp_info=None,
                 parent_scope=None):
        name = name or ast.attrib['name']
        is_function = ast.tag == 'function'
        source = extract_source(ast, raw_source, full_lines=True)
        scope = Scope(parent=parent_scope)

        # Store the names of variables in the subroutine signature
        if is_function:
            arg_ast = ast.findall('header/names/name')
            args = [arg.attrib['id'].upper() for arg in arg_ast]
        else:
            arg_ast = ast.findall('header/arguments/argument')
            args = [arg.attrib['name'].upper() for arg in arg_ast]

        # Decompose the body into known sections
        ast_body = list(ast.find('body'))
        ast_spec = ast.find('body/specification')
        idx_spec = ast_body.index(ast_spec)
        ast_docs = ast_body[:idx_spec]
        ast_body = ast_body[idx_spec+1:]

        # Create a IRs for the docstring and the declaration spec
        docs = parse_ofp_ast(ast_docs, pp_info=pp_info, raw_source=raw_source, scope=scope)
        spec = parse_ofp_ast(ast_spec, definitions=definitions, pp_info=pp_info,
                             raw_source=raw_source, scope=scope)

        # Generate the subroutine body with all shape and type info
        body = parse_ofp_ast(ast_body, pp_info=pp_info, raw_source=raw_source, scope=scope)
        body = Section(body=body)

        # Big, but necessary hack:
        # For deferred array dimensions on allocatables, we infer the conceptual
        # dimension by finding any `allocate(var(<dims>))` statements.
        spec, body = cls._infer_allocatable_shapes(spec, body)

        # Parse "member" subroutines and functions recursively
        members = None
        if ast.find('members'):
            members = [Subroutine.from_ofp(ast=member, raw_source=raw_source, definitions=definitions,
                                           parent_scope=scope)
                       for member in list(ast.find('members'))
                       if member.tag in ('subroutine', 'function')]
            members = as_tuple(members)

        return cls(name=name, args=args, docstring=docs, spec=spec, body=body, ast=ast,
                   members=members, scope=scope, is_function=is_function, source=source,
                   rescope_variables=True)

    @classmethod
    def from_omni(cls, ast, raw_source, typetable, definitions=None, name=None, symbol_map=None,
                  parent_scope=None):
        name = name or ast.find('name').text
        # file = ast.attrib['file']
        type_map = {t.attrib['type']: t for t in typetable}
        symbol_map = symbol_map or {}
        symbol_map.update({s.attrib['type']: s for s in ast.find('symbols')})

        # Check if it is a function or a subroutine. There may be a better way to do
        # this but OMNI does not seem to make it obvious, thus checking the return type
        name_id = ast.find('name').attrib['type']
        is_function = name_id in type_map and type_map[name_id].attrib['return_type'] != 'Fvoid'

        source = Source((ast.attrib['lineno'], ast.attrib['lineno']))
        scope = Scope(parent=parent_scope)

        # Get the names of dummy variables from the type_map
        fhash = ast.find('name').attrib['type']
        ftype = [t for t in typetable.findall('FfunctionType')
                 if t.attrib['type'] == fhash][0]
        args = as_tuple(name.text for name in ftype.findall('params/name'))

        # Generate spec
        spec = parse_omni_ast(ast.find('declarations'), definitions=definitions, type_map=type_map,
                              symbol_map=symbol_map, raw_source=raw_source, scope=scope)

        # Filter out the declaration for the subroutine name but keep it for functions (since
        # this declares the return type)
        if not is_function:
            mapper = {d: None for d in FindNodes(Declaration).visit(spec)
                      if d.variables[0].name == name}
            spec = Transformer(mapper, invalidate_source=False).visit(spec)

        # Hack: We remove comments from the beginning of the spec to get the docstring
        comment_map = {}
        docs = []
        for node in spec.body:
            if not isinstance(node, (Comment, CommentBlock)):
                break
            docs.append(node)
            comment_map[node] = None
        spec = Transformer(comment_map, invalidate_source=False).visit(spec)

        # Insert the `implicit none` statement OMNI omits (slightly hacky!)
        f_imports = [im for im in FindNodes(Import).visit(spec) if not im.c_import]
        spec_body = list(spec.body)
        spec_body.insert(len(f_imports), Intrinsic(text='IMPLICIT NONE'))
        spec._update(body=as_tuple(spec_body))

        # Parse member functions properly
        contains = ast.find('body/FcontainsStatement')
        members = None
        if contains is not None:
            members = [Subroutine.from_omni(ast=s, typetable=typetable, definitions=definitions,
                                            symbol_map=symbol_map, raw_source=raw_source,
                                            parent_scope=scope)
                       for s in contains.findall('FfunctionDefinition')]
            # Strip members from the XML before we proceed
            ast.find('body').remove(contains)

        # Convert the core kernel to IR
        body = parse_omni_ast(ast.find('body'), definitions=definitions, type_map=type_map,
                              symbol_map=symbol_map, raw_source=raw_source, scope=scope)
        body = Section(body=body)

        # Big, but necessary hack:
        # For deferred array dimensions on allocatables, we infer the conceptual
        # dimension by finding any `allocate(var(<dims>))` statements.
        spec, body = cls._infer_allocatable_shapes(spec, body)

        return cls(name=name, args=args, docstring=docs, spec=spec, body=body, ast=ast,
                   members=members, scope=scope, is_function=is_function, source=source,
                   rescope_variables=True)

    @classmethod
    def from_fparser(cls, ast, raw_source, name=None, definitions=None, pp_info=None,
                     parent_scope=None):
        is_function = isinstance(ast, Fortran2003.Function_Subprogram)
        if is_function:
            routine_stmt = get_child(ast, Fortran2003.Function_Stmt)
            name = name or routine_stmt.items[1].tostr()
        else:
            routine_stmt = get_child(ast, Fortran2003.Subroutine_Stmt)
            name = name or routine_stmt.get_name().string

        source = extract_fparser_source(ast, raw_source)
        scope = Scope(parent=parent_scope)

        dummy_arg_list = routine_stmt.items[2]
        args = [arg.string for arg in dummy_arg_list.items] if dummy_arg_list else []

        spec_ast = get_child(ast, Fortran2003.Specification_Part)
        if spec_ast:
            spec = parse_fparser_ast(spec_ast, pp_info=pp_info, definitions=definitions,
                                     scope=scope, raw_source=raw_source)
        else:
            spec = Section(body=())

        body_ast = get_child(ast, Fortran2003.Execution_Part)
        if body_ast:
            body = parse_fparser_ast(body_ast, pp_info=pp_info, definitions=definitions,
                                     scope=scope, raw_source=raw_source)
        else:
            body = Section(body=())

        # Big, but necessary hack:
        # For deferred array dimensions on allocatables, we infer the conceptual
        # dimension by finding any `allocate(var(<dims>))` statements.
        spec, body = cls._infer_allocatable_shapes(spec, body)

        # Another big hack: fparser allocates all comments before and after the spec to the spec.
        # We remove them from the beginning to get the docstring and move them from the end to the
        # body as those can potentially be pragmas.
        comment_map = {}
        docs = []
        for node in spec.body:
            if not isinstance(node, (Comment, CommentBlock)):
                break
            docs.append(node)
            comment_map[node] = None
        for node in reversed(spec.body):
            if not isinstance(node, (Pragma, Comment, CommentBlock)):
                break
            body.prepend(node)
            comment_map[node] = None
        spec = Transformer(comment_map, invalidate_source=False).visit(spec)

        # Parse "member" subroutines recursively
        members = None
        contains_ast = get_child(ast, Fortran2003.Internal_Subprogram_Part)
        if contains_ast:
            routine_types = (Fortran2003.Subroutine_Subprogram, Fortran2003.Function_Subprogram)
            members = [Subroutine.from_fparser(ast=s, raw_source=raw_source, definitions=definitions,
                                               pp_info=pp_info, parent_scope=scope)
                       for s in walk(contains_ast, routine_types)]

        return cls(name=name, args=args, docstring=docs, spec=spec, body=body, ast=ast,
                   members=members, scope=scope, is_function=is_function, source=source,
                   rescope_variables=True)

    @property
    def variables(self):
        """
        Return the variables (including arguments) declared in this subroutine
        """
        return as_tuple(flatten(decl.variables for decl in FindNodes(Declaration).visit(self.spec)))

    @variables.setter
    def variables(self, variables):
        """
        Set the variables property and ensure that the internal declarations match.

        Note that arguments also count as variables and therefore any
        removal from this list will also remove arguments from the subroutine signature.
        """
        # First map variables to existing declarations
        declarations = FindNodes(Declaration).visit(self.spec)
        decl_map = dict((v, decl) for decl in declarations for v in decl.variables)

        for v in as_tuple(variables):
            if v not in decl_map:
                # By default, append new variables to the end of the spec
                new_decl = Declaration(variables=[v])
                self.spec.append(new_decl)

        # Run through existing declarations and check that all variables still exist
        dmap = {}
        for decl in FindNodes(Declaration).visit(self.spec):
            new_vars = as_tuple(v for v in decl.variables if v in variables)
            if len(new_vars) > 0:
                decl._update(variables=new_vars)
            else:
                dmap[decl] = None  # Mark for removal
        # Remove all redundant declarations
        self.spec = Transformer(dmap).visit(self.spec)

        # Filter the dummy list in case we removed an argument
        varnames = [str(v.name).lower() for v in variables]
        self._dummies = as_tuple(arg for arg in self._dummies if str(arg).lower() in varnames)

    @property
    def arguments(self):
        """
        Return arguments in order of the defined signature (dummy list).
        """
        # TODO: Can be simplified once we can directly lookup variables objects in scope
        arg_map = {v.name.lower(): v for v in self.variables if v.name.lower() in self._dummies}
        return as_tuple(arg_map[a.lower()] for a in self._dummies)

    @arguments.setter
    def arguments(self, arguments):
        """
        Set the arguments property and ensure that internal declarations and signature match.

        Note that removing arguments from this property does not actually remove declarations.
        """
        # First map variables to existing declarations
        declarations = FindNodes(Declaration).visit(self.spec)
        decl_map = dict((v, decl) for decl in declarations for v in decl.variables)

        arguments = as_tuple(arguments)
        for arg in arguments:
            if arg not in decl_map:
                # By default, append new variables to the end of the spec
                assert arg.type.intent is not None
                new_decl = Declaration(variables=[arg])
                self.spec.append(new_decl)

        # Set new dummy list according to input
        self._dummies = as_tuple(arg.name.lower() for arg in arguments)

    def enrich_calls(self, routines):
        """
        Attach target :class:`Subroutine` object to :class:`CallStatement`
        objects in the IR tree.

        :param call_targets: :class:`Subroutine` objects for corresponding
                             :class:`CallStatement` nodes in the IR tree.
        :param active: Additional flag indicating whether this :call:`CallStatement`
                       represents an active/inactive edge in the
                       interprocedural callgraph.
        """
        routine_map = {r.name.upper(): r for r in as_tuple(routines)}

        with pragmas_attached(self, CallStatement, attach_pragma_post=False):
            for call in FindNodes(CallStatement).visit(self.body):
                name = str(call.name).upper()
                if name in routine_map:
                    # Calls marked as 'reference' are inactive and thus skipped
                    active = not is_loki_pragma(call.pragma, starts_with='reference')
                    context = CallContext(routine=routine_map[name], active=active)
                    call._update(context=context)

        # TODO: Could extend this to module and header imports to
        # facilitate user-directed inlining.

    @property
    def ir(self):
        """
        Intermediate representation (AST) of the body in this subroutine
        """
        return (self.docstring, self.spec, self.body)

    @property
    def source(self):
        return self._source

    @property
    def scope(self):
        return self._scope

    @property
    def symbols(self):
        return self.scope.symbols

    @property
    def parent(self):
        """
        Enclosing object, as defined by the propagation of types via `Scope` objects
        """
        return self.scope.parent.defined_by if self.scope.parent else None

    def to_fortran(self, conservative=False):
        return fgen(self, conservative=conservative)

    @property
    def members(self):
        """
        Tuple of member function defined in this `Subroutine`.
        """
        return as_tuple(self._members)

    @property
    def argnames(self):
        return [a.name for a in self.arguments]

    @property
    def variable_map(self):
        """
        Map of variable names to `Variable` objects
        """
        return CaseInsensitiveDict((v.name, v) for v in self.variables)

    @property
    def interface(self):
        """
        Interface object that defines the `Subroutine` signature in header files.
        """
        arg_names = [arg.name for arg in self.arguments]

        # Remove all local variable declarations from interface routine spec
        # and duplicate all argument symbols within a new subroutine scope
        routine = Subroutine(name=self.name, args=arg_names, spec=None, body=None)
        decl_map = {}
        for decl in FindNodes(Declaration).visit(self.spec):
            if all(v.name in arg_names for v in decl.variables):
                # Replicate declaration with re-scoped variables
                variables = as_tuple(v.clone(scope=routine.scope) for v in decl.variables)
                decl_map[decl] = decl.clone(variables=variables)
            else:
                decl_map[decl] = None  # Remove local variable declarations
        routine.spec = Transformer(decl_map).visit(self.spec)
        return Interface(body=(routine,))

    def apply(self, op, **kwargs):
        """
        Apply a given transformation to the source file object.

        Note that the dispatch routine `op.apply(source)` will ensure
        that all entities of this `Sourcefile` are correctly traversed.
        """
        # TODO: Should type-check for an `Operation` object here
        op.apply(self, **kwargs)

    def __repr__(self):
        """
        String representation.
        """
        return '{}:: {}'.format('Function' if self.is_function else 'Subroutine', self.name)

    def rescope_variables(self):
        """
        Verify that all :any:`TypedSymbol` objects in the IR are in the
        subroutine's scope or in a parent scope.
        """
        # Collect all scopes that we can access/are relevant in this routine
        scope_hierarchy = []
        s = self.scope
        while s is not None:
            scope_hierarchy += [s]
            s = s.parent

        # The local variable map. These really need to be in *this* scope.
        variable_map = self.variable_map
        imports_map = CaseInsensitiveDict(
            (s.name, s) for imprt in FindNodes(Import).visit(self.spec or ()) for s in imprt.symbols
        )

        def check_and_rescope_var(var):
            """
            Helper function that takes care of checking a variable's scope and,
            if necessary, attaching the correct scope
            """
            parent = None if var.parent is None else check_and_rescope_var(var.parent)

            if (var.name in variable_map or var.name in imports_map) and var.scope is not self.scope:
                # This takes care of all local variables or imported symbols
                return var.clone(scope=self.scope, parent=parent)

            if var.scope not in scope_hierarchy:
                # This is the fallback for any symbols from parent scopes.
                # We need to run through the scope hierarchy manually because we
                # not only need to know the stored type (if any) but also in which
                # scope it is stored
                for s in scope_hierarchy:
                    # Here we try to be careful not to accidentally overwrite
                    # existing type information
                    _type = s.symbols.lookup(var.name, recursive=False)
                    if _type:
                        # TODO: comparing derived type member types really should become a bit easier..
                        is_equal = _type.compare(var.type, ignore='parent')
                        if is_equal and (_type.parent or var.type.parent):
                            parent_equal = _type.parent and var.type.parent and \
                                str(_type.parent) == str(var.type.parent) and \
                                _type.parent.type.compare(var.type.parent.type)
                            is_equal &= parent_equal
                        if not is_equal and var.type.dtype is not BasicType.DEFERRED:
                            warning('Subroutine.rescope_variables: type for %s does not match stored type.',
                                    var.name)
                        return var.clone(scope=s, type=_type, parent=parent)

                # Variable does not exist in any scope so we put this in the local
                # scope just to be on the safe side
                debug('Subroutine.rescope_variables: type for %s not found in any scope.', var.name)
                return var.clone(scope=self.scope, parent=parent)

            if parent is not None:
                # Had only to rescope the parent
                return var.clone(parent=parent)

            # Nothing had to be done
            return None

        # Check for all variables that they are associated with one of the
        # scopes in the hierarchy
        rescope_map = {}
        for var in FindTypedSymbols().visit(self.ir):
            if var not in rescope_map:
                rescoped_var = check_and_rescope_var(var)
                if rescoped_var is not None:
                    rescope_map[var] = rescoped_var

        # Now apply the rescoping map
        if rescope_map and self.spec:
            self.spec = SubstituteExpressions(rescope_map, invalidate_source=False).visit(self.spec)
        if rescope_map and self.body:
            self.body = SubstituteExpressions(rescope_map, invalidate_source=False).visit(self.body)

    def clone(self, **kwargs):
        """
        Create a copy of the subroutine with the option to override individual
        parameters.

        Parameters
        ----------
        **kwargs :
            Any parameters from the constructor of :any:`Subroutine`.

        Returns
        -------
        :any:`Subroutine`
            The cloned subroutine object.
        """
        if self.name and 'name' not in kwargs:
            kwargs['name'] = self.name
        if self.argnames and 'args' not in kwargs:
            kwargs['args'] = self.argnames
        if self._ast and 'ast' not in kwargs:
            kwargs['ast'] = self._ast
        if self.bind and 'bind' not in kwargs:
            kwargs['bind'] = self.bind
        if self.is_function and 'is_function' not in kwargs:
            kwargs['is_function'] = self.is_function
        if self.source and 'source' not in kwargs:
            kwargs['source'] = self.source

        if 'scope' not in kwargs:
            kwargs['scope'] = Scope(parent=kwargs.get('parent_scope', self.scope.parent))
        if 'rescope_variables' not in kwargs:
            kwargs['rescope_variables'] = True
        if self.members and 'members' not in kwargs:
            kwargs['members'] = [
                member.clone(parent_scope=kwargs['scope'], rescope_variables=kwargs['rescope_variables'])
                for member in self.members
            ]

        kwargs['docstring'] = Transformer({}).visit(self.docstring)
        kwargs['spec'] = Transformer({}).visit(self.spec)
        kwargs['body'] = Transformer({}).visit(self.body)

        return type(self)(**kwargs)
