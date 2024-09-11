# (C) Copyright 2018- ECMWF.
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.

import pytest

from loki import Module, Subroutine
from loki.build import jit_compile_lib, Builder, Obj
from loki.frontend import available_frontends, OMNI, OFP
from loki.ir import (
    nodes as ir, FindNodes, FindVariables, FindInlineCalls
)

from loki.transformations.inline import (
    inline_elemental_functions, inline_statement_functions
)


@pytest.fixture(name='builder')
def fixture_builder(tmp_path):
    yield Builder(source_dirs=tmp_path, build_dir=tmp_path)
    Obj.clear_cache()


@pytest.mark.parametrize('frontend', available_frontends())
def test_transform_inline_elemental_functions(tmp_path, builder, frontend):
    """
    Test correct inlining of elemental functions.
    """
    fcode_module = """
module multiply_mod
  use iso_fortran_env, only: real64
  implicit none
contains

  elemental function multiply(a, b)
    real(kind=real64) :: multiply
    real(kind=real64), intent(in) :: a, b
    real(kind=real64) :: temp
    !$loki routine seq

    ! simulate multi-line function
    temp = a * b
    multiply = temp
  end function multiply
end module multiply_mod
"""

    fcode = """
subroutine transform_inline_elemental_functions(v1, v2, v3)
  use iso_fortran_env, only: real64
  use multiply_mod, only: multiply
  real(kind=real64), intent(in) :: v1
  real(kind=real64), intent(out) :: v2, v3

  v2 = multiply(v1, 6._real64)
  v3 = 600. + multiply(6._real64, 11._real64)
end subroutine transform_inline_elemental_functions
"""

    # Generate reference code, compile run and verify
    module = Module.from_source(fcode_module, frontend=frontend, xmods=[tmp_path])
    routine = Subroutine.from_source(fcode, frontend=frontend, xmods=[tmp_path])

    refname = f'ref_{routine.name}_{frontend}'
    reference = jit_compile_lib([module, routine], path=tmp_path, name=refname, builder=builder)

    v2, v3 = reference.transform_inline_elemental_functions(11.)
    assert v2 == 66.
    assert v3 == 666.

    (tmp_path/f'{module.name}.f90').unlink()
    (tmp_path/f'{routine.name}.f90').unlink()

    # Now inline elemental functions
    routine = Subroutine.from_source(fcode, definitions=module, frontend=frontend, xmods=[tmp_path])
    inline_elemental_functions(routine)

    # Make sure there are no more inline calls in the routine body
    assert not FindInlineCalls().visit(routine.body)

    # Verify correct scope of inlined elements
    assert all(v.scope is routine for v in FindVariables().visit(routine.body))

    # Ensure the !$loki routine pragma has been removed
    assert not FindNodes(ir.Pragma).visit(routine.body)

    # Hack: rename routine to use a different filename in the build
    routine.name = f'{routine.name}_'
    kernel = jit_compile_lib([routine], path=tmp_path, name=routine.name, builder=builder)

    v2, v3 = kernel.transform_inline_elemental_functions_(11.)
    assert v2 == 66.
    assert v3 == 666.

    builder.clean()
    (tmp_path/f'{routine.name}.f90').unlink()


@pytest.mark.parametrize('frontend', available_frontends())
def test_transform_inline_elemental_functions_extended(tmp_path, builder, frontend):
    """
    Test correct inlining of elemental functions.
    """
    fcode_module = """
module multiply_extended_mod
  use iso_fortran_env, only: real64
  implicit none
contains

  elemental function multiply(a, b) ! result (ret_mult)
    ! real(kind=real64) :: ret_mult
    real(kind=real64) :: multiply
    real(kind=real64), intent(in) :: a, b
    real(kind=real64) :: temp

    ! simulate multi-line function
    temp = a * b
    multiply = temp
    ! ret_mult = temp
  end function multiply

  elemental function multiply_single_line(a, b)
    real(kind=real64) :: multiply_single_line
    real(kind=real64), intent(in) :: a, b
    real(kind=real64) :: temp

    multiply_single_line = a * b
  end function multiply_single_line

  elemental function add(a, b)
    real(kind=real64) :: add
    real(kind=real64), intent(in) :: a, b
    real(kind=real64) :: temp

    ! simulate multi-line function
    temp = a + b
    add = temp
  end function add
end module multiply_extended_mod
"""

    fcode = """
subroutine transform_inline_elemental_functions_extended(v1, v2, v3)
  use iso_fortran_env, only: real64
  use multiply_extended_mod, only: multiply, multiply_single_line, add
  real(kind=real64), intent(in) :: v1
  real(kind=real64), intent(out) :: v2, v3
  real(kind=real64), parameter :: param1 = 100.

  v2 = multiply(v1, 6._real64) + multiply_single_line(v1, 3._real64)
  v3 = add(param1, 200._real64) + add(150._real64, 150._real64) + multiply(6._real64, 11._real64)
end subroutine transform_inline_elemental_functions_extended
"""

    # Generate reference code, compile run and verify
    module = Module.from_source(fcode_module, frontend=frontend, xmods=[tmp_path])
    routine = Subroutine.from_source(fcode, frontend=frontend, xmods=[tmp_path])

    refname = f'ref_{routine.name}_{frontend}'
    reference = jit_compile_lib([module, routine], path=tmp_path, name=refname, builder=builder)

    v2, v3 = reference.transform_inline_elemental_functions_extended(11.)
    assert v2 == 99.
    assert v3 == 666.

    (tmp_path/f'{module.name}.f90').unlink()
    (tmp_path/f'{routine.name}.f90').unlink()

    # Now inline elemental functions
    routine = Subroutine.from_source(fcode, definitions=module, frontend=frontend, xmods=[tmp_path])
    inline_elemental_functions(routine)


    # Make sure there are no more inline calls in the routine body
    assert not FindInlineCalls().visit(routine.body)

    # Verify correct scope of inlined elements
    assert all(v.scope is routine for v in FindVariables().visit(routine.body))

    # Hack: rename routine to use a different filename in the build
    routine.name = f'{routine.name}_'
    kernel = jit_compile_lib([routine], path=tmp_path, name=routine.name, builder=builder)

    v2, v3 = kernel.transform_inline_elemental_functions_extended_(11.)
    assert v2 == 99.
    assert v3 == 666.

    builder.clean()
    (tmp_path/f'{routine.name}.f90').unlink()


@pytest.mark.parametrize('frontend', available_frontends(
    skip={OFP: "OFP apparently has problems dealing with those Statement Functions",
          OMNI: "OMNI automatically inlines Statement Functions"}
))
@pytest.mark.parametrize('stmt_decls', (True, False))
def test_inline_statement_functions(frontend, stmt_decls):
    stmt_decls_code = """
    real :: PTARE
    real :: FOEDELTA
    FOEDELTA ( PTARE ) = PTARE + 1.0
    real :: FOEEW
    FOEEW ( PTARE ) = PTARE + FOEDELTA(PTARE)
    """.strip()

    fcode = f"""
subroutine stmt_func(arr, ret)
    implicit none
    real, intent(in) :: arr(:)
    real, intent(inout) :: ret(:)
    real :: ret2
    real, parameter :: rtt = 1.0
    {stmt_decls_code if stmt_decls else '#include "fcttre.func.h"'}

    ret = foeew(arr) 
    ret2 = foedelta(3.0)
end subroutine stmt_func
     """.strip()

    routine = Subroutine.from_source(fcode, frontend=frontend)
    if stmt_decls:
        assert FindNodes(ir.StatementFunction).visit(routine.spec)
    else:
        assert not FindNodes(ir.StatementFunction).visit(routine.spec)
    assert FindInlineCalls().visit(routine.body)
    inline_statement_functions(routine)

    assert not FindNodes(ir.StatementFunction).visit(routine.spec)
    if stmt_decls:
        assert not FindInlineCalls().visit(routine.body)
        assignments = FindNodes(ir.Assignment).visit(routine.body)
        assert assignments[0].lhs  == 'ret'
        assert assignments[0].rhs  ==  "arr + arr + 1.0"
        assert assignments[1].lhs  == 'ret2'
        assert assignments[1].rhs  ==  "3.0 + 1.0"
    else:
        assert FindInlineCalls().visit(routine.body)
