"""
Contains the declaration of :any:`Sourcefile` that is used to represent and
manipulate (Fortran) source code files.
"""
from pathlib import Path

from loki.subroutine import Subroutine
from loki.module import Module
from loki.tools import flatten, as_tuple
from loki.logging import info
from loki.frontend import (
    OMNI, OFP, FP, sanitize_input, Source, read_file, preprocess_cpp,
    parse_omni_source, parse_ofp_source, parse_fparser_source,
    parse_omni_ast, parse_ofp_ast, parse_fparser_ast
)
from loki.ir import Section
from loki.backend.fgen import fgen


__all__ = ['Sourcefile']


class Sourcefile:
    """
    Class to handle and manipulate source files, storing :any:`Module` and
    :any:`Subroutine` objects.

    Reading existing source code from file or string can be done via
    :meth:`from_file` or :meth:`from_source`.

    Parameters
    ----------
    path : str
        The name of the source file.
    ir : :any:`Section`, optional
        The IR of the file content (including :any:`Subroutine`, :any:`Module`,
        :any:`Comment` etc.)
    ast : optional
        Parser-AST of the original source file.
    source : :any:`Source`, optional
        Raw source string and line information about the original source file.
    """

    def __init__(self, path, ir=None, ast=None, source=None):
        self.path = Path(path) if path is not None else path
        if ir is not None and not isinstance(ir, Section):
            ir = Section(body=ir)
        self.ir = ir
        self._ast = ast
        self._source = source

    @classmethod
    def from_file(cls, filename, definitions=None, preprocess=False,
                  includes=None, defines=None, omni_includes=None,
                  xmods=None, frontend=FP):
        """
        Constructor from raw source files that can apply a
        C-preprocessor before invoking frontend parsers.

        Parameters
        ----------
        filename : str
            Name of the file to parse into a :any:`Sourcefile` object.
        definitions : list of :any:`Module`, optional
            :any:`Module` object(s) that may supply external type or procedure
            definitions.
        preprocess : bool, optional
            Flag to trigger CPP preprocessing (by default `False`).

            .. attention::
                Please note that, when using the OMNI frontend, C-preprocessing
                will always be applied, so :data:`includes` and :data:`defines`
                may have to be defined even when disabling :data:`preprocess`.

        includes : list of str, optional
            Include paths to pass to the C-preprocessor.
        defines : list of str, optional
            Symbol definitions to pass to the C-preprocessor.
        xmods : str, optional
            Path to directory to find and store ``.xmod`` files when using the
            OMNI frontend.
        omni_includes: list of str, optional
            Additional include paths to pass to the preprocessor run as part of
            the OMNI frontend parse. If set, this **replaces** (!)
            :data:`includes`, otherwise :data:`omni_includes` defaults to the
            value of :data:`includes`.
        frontend : :any:`Frontend`, optional
            Frontend to use for producing the AST (default :any:`FP`).
        """
        filepath = Path(filename)
        raw_source = read_file(filepath)

        if preprocess:
            # Trigger CPP-preprocessing explicitly, as includes and
            # defines can also be used by our OMNI frontend
            source = preprocess_cpp(source=raw_source, filepath=filepath,
                                    includes=includes, defines=defines)
        else:
            source = raw_source

        if frontend == OMNI:
            return cls.from_omni(source, filepath, definitions=definitions,
                                 includes=includes, defines=defines,
                                 xmods=xmods, omni_includes=omni_includes)

        if frontend == OFP:
            return cls.from_ofp(source, filepath, definitions=definitions)

        if frontend == FP:
            return cls.from_fparser(source, filepath, definitions=definitions)

        raise NotImplementedError(f'Unknown frontend: {frontend}')

    @classmethod
    def from_omni(cls, raw_source, filepath, definitions=None, includes=None,
                  defines=None, xmods=None, omni_includes=None):
        """
        Parse a given source file using the OMNI frontend

        Parameters
        ----------
        raw_source : str
            Fortran source string
        filepath : str or :any:`pathlib.Path`
            The filepath of this source file
        definitions : list
            List of external :any:`Module` to provide derived-type and procedure declarations
        includes : list of str, optional
            Include paths to pass to the C-preprocessor.
        defines : list of str, optional
            Symbol definitions to pass to the C-preprocessor.
        xmods : str, optional
            Path to directory to find and store ``.xmod`` files when using the
            OMNI frontend.
        omni_includes: list of str, optional
            Additional include paths to pass to the preprocessor run as part of
            the OMNI frontend parse. If set, this **replaces** (!)
            :data:`includes`, otherwise :data:`omni_includes` defaults to the
            value of :data:`includes`.
        """
        # Always CPP-preprocess source files for OMNI, but optionally
        # use a different set of include paths if specified that way.
        # (It's a hack, I know, but OMNI sucks, so what can I do...?)
        if omni_includes is not None and len(omni_includes) > 0:
            includes = omni_includes
        source = preprocess_cpp(raw_source, filepath=filepath,
                                includes=includes, defines=defines)

        # Parse the file content into an OMNI Fortran AST
        ast = parse_omni_source(source=source, filepath=filepath, xmods=xmods)
        typetable = ast.find('typeTable')
        return cls._from_omni_ast(ast=ast, path=filepath, raw_source=raw_source,
                                  definitions=definitions, typetable=typetable)

    @classmethod
    def _from_omni_ast(cls, ast, path=None, raw_source=None, definitions=None, typetable=None):
        """
        Generate the full set of `Subroutine` and `Module` members of the `Sourcefile`.
        """
        type_map = {t.attrib['type']: t for t in typetable}
        if ast.find('symbols') is not None:
            symbol_map = {s.attrib['type']: s for s in ast.find('symbols')}
        else:
            symbol_map = None

        ir = parse_omni_ast(
            ast=ast, definitions=definitions, raw_source=raw_source,
            type_map=type_map, symbol_map=symbol_map
        )

        lines = (1, raw_source.count('\n') + 1)
        source = Source(lines, string=raw_source, file=path)
        return cls(path=path, ir=ir, ast=ast, source=source)

    @classmethod
    def from_ofp(cls, raw_source, filepath, definitions=None):
        """
        Parse a given source file using the Open Fortran Parser (OFP) frontend

        Parameters
        ----------
        raw_source : str
            Fortran source string
        filepath : str or :any:`pathlib.Path`
            The filepath of this source file
        definitions : list
            List of external :any:`Module` to provide derived-type and procedure declarations
        """
        # Preprocess using internal frontend-specific PP rules
        # to sanitize input and work around known frontend problems.
        source, pp_info = sanitize_input(source=raw_source, frontend=OFP, filepath=filepath)

        # Parse the file content into a Fortran AST
        ast = parse_ofp_source(source, filepath=filepath)

        return cls._from_ofp_ast(path=filepath, ast=ast, definitions=definitions,
                                 pp_info=pp_info, raw_source=raw_source)

    @classmethod
    def _from_ofp_ast(cls, ast, path=None, raw_source=None, definitions=None, pp_info=None):
        """
        Generate the full set of :any:`Subroutine` and :any:`Module` members
        in the :any:`Sourcefile`.
        """
        ir = parse_ofp_ast(ast.find('file'), pp_info=pp_info, definitions=definitions, raw_source=raw_source)

        lines = (1, raw_source.count('\n') + 1)
        source = Source(lines, string=raw_source, file=path)
        return cls(path=path, ir=ir, ast=ast, source=source)

    @classmethod
    def from_fparser(cls, raw_source, filepath, definitions=None):
        """
        Parse a given source file using the fparser frontend

        Parameters
        ----------
        raw_source : str
            Fortran source string
        filepath : str or :any:`pathlib.Path`
            The filepath of this source file
        definitions : list
            List of external :any:`Module` to provide derived-type and procedure declarations
        """
        # Preprocess using internal frontend-specific PP rules
        # to sanitize input and work around known frontend problems.
        source, pp_info = sanitize_input(source=raw_source, frontend=FP, filepath=filepath)

        # Parse the file content into a Fortran AST
        ast = parse_fparser_source(source)

        return cls._from_fparser_ast(path=filepath, ast=ast, definitions=definitions,
                                     pp_info=pp_info, raw_source=raw_source)

    @classmethod
    def _from_fparser_ast(cls, ast, path=None, raw_source=None, definitions=None, pp_info=None):
        """
        Generate the full set of :any:`Subroutine` and :any:`Module` members
        in the :any:`Sourcefile`.
        """
        ir = parse_fparser_ast(ast, pp_info=pp_info, definitions=definitions, raw_source=raw_source)

        lines = (1, raw_source.count('\n') + 1)
        source = Source(lines, string=raw_source, file=path)
        return cls(path=path, ir=ir, ast=ast, source=source)

    @classmethod
    def from_source(cls, source, xmods=None, definitions=None, frontend=FP):
        """
        Constructor from raw source string that invokes specified frontend parser

        Parameters
        ----------
        source : str
            Fortran source string
        xmods : str, optional
            Path to directory to find and store ``.xmod`` files when using the
            OMNI frontend.
        definitions : list of :any:`Module`, optional
            :any:`Module` object(s) that may supply external type or procedure
            definitions.
        frontend : :any:`Frontend`, optional
            Frontend to use for producing the AST (default :any:`FP`).
        """
        if frontend == OMNI:
            ast = parse_omni_source(source, xmods=xmods)
            typetable = ast.find('typeTable')
            return cls._from_omni_ast(path=None, ast=ast, raw_source=source,
                                      definitions=definitions, typetable=typetable)

        if frontend == OFP:
            ast = parse_ofp_source(source)
            return cls._from_ofp_ast(path=None, ast=ast, raw_source=source, definitions=definitions)

        if frontend == FP:
            ast = parse_fparser_source(source)
            return cls._from_fparser_ast(path=None, ast=ast, raw_source=source, definitions=definitions)

        raise NotImplementedError(f'Unknown frontend: {frontend}')

    @property
    def source(self):
        return self._source

    def to_fortran(self, conservative=False):
        return fgen(self, conservative=conservative)

    @property
    def modules(self):
        """
        List of :class:`Module` objects that are members of this :class:`Sourcefile`.
        """
        if self.ir is None:
            return ()
        return as_tuple([
            module for module in self.ir.body if isinstance(module, Module)
        ])

    @property
    def routines(self):
        """
        List of :class:`Subroutine` objects that are members of this :class:`Sourcefile`.
        """
        if self.ir is None:
            return ()
        return as_tuple([
            routine for routine in self.ir.body if isinstance(routine, Subroutine)
        ])

    subroutines = routines

    @property
    def all_subroutines(self):
        routines = self.subroutines
        routines += as_tuple(flatten(m.subroutines for m in self.modules))
        return routines

    def __getitem__(self, name):
        module_map = {m.name.lower(): m for m in self.modules}
        if name.lower() in module_map:
            return module_map[name.lower()]

        subroutine_map = {s.name.lower(): s for s in self.all_subroutines}  # pylint: disable=no-member
        if name.lower() in subroutine_map:
            return subroutine_map[name.lower()]

        return None

    def __iter__(self):
        raise TypeError('Sourcefiles alone cannot be traversed! Try traversing "Sourcefile.ir".')

    def __bool__(self):
        """
        Ensure existing objects register as True in boolean checks, despite
        raising exceptions in `__iter__`.
        """
        return True

    def apply(self, op, **kwargs):
        """
        Apply a given transformation to the source file object.

        Note that the dispatch routine `op.apply(source)` will ensure
        that all entities of this `Sourcefile` are correctly traversed.
        """
        # TODO: Should type-check for an `Operation` object here
        op.apply(self, **kwargs)

    def write(self, path=None, source=None, conservative=False):
        """
        Write content as Fortran source code to file

        Parameters
        ----------
        path : str, optional
            Filepath of target file; if not provided, :any:`Sourcefile.path` is used
        source : str, optional
            Write the provided string instead of generating via :any:`Sourcefile.to_fortran`
        conservative : bool, optional
            Enable conservative output in the backend, aiming to be as much string-identical
            as possible (default: False)
        """
        path = self.path if path is None else Path(path)
        source = self.to_fortran(conservative) if source is None else source
        self.to_file(source=source, path=path)

    @classmethod
    def to_file(cls, source, path):
        """
        Same as :meth:`write` but can be called from a static context.
        """
        info(f'Writing {path}')
        with path.open('w') as f:
            f.write(source)
            if source[-1] != '\n':
                f.write('\n')
