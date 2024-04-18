# (C) Copyright 2018- ECMWF.
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.

from loki import (
    Transformation, ProcedureItem, ir, Module, as_tuple, SymbolAttributes, BasicType, Variable,
    RangeIndex, Array, FindVariables, resolve_associates, SubstituteExpressions, FindNodes,
    resolve_typebound_var, recursive_expression_map_update
)

from transformations.single_column_coalesced import SCCBaseTransformation

__all__ = ['UnprivatiseStructsTransformation', 'BlockIndexInjectTransformation']

class UnprivatiseStructsTransformation(Transformation):


    _key = 'UnprivatiseStructsTransformation'

    # This trafo only operates on procedures
    item_filter = (ProcedureItem,)

    def __init__(self, horizontal, exclude=[], key=None):
        self.horizontal = horizontal
        self.exclude = exclude
        if key:
             self._key = key

    @staticmethod
    def get_parent_typedef(var, routine):

        if not var.parent.type.dtype.typedef == BasicType.DEFERRED:
            return var.parent.type.dtype.typedef
        elif not routine.symbol_map[var.parent.type.dtype.name].type.dtype.typedef == BasicType.DEFERRED:
            return routine.symbol_map[var.parent.type.dtype.name].type.dtype.typedef
        else:
            raise RuntimeError(f'Container data-type {var.parent.type.dtype.name} not enriched')

    def transform_subroutine(self, routine, **kwargs):

        if not (item := kwargs['item']):
            raise RuntimeError('Cannot apply DeprivatiseStructsTransformation without item to store definitions')
        successors = kwargs.get('successors', ())

        role = kwargs['role']
        targets = tuple(str(t).lower() for t in as_tuple(kwargs.get('targets', None)))

        if role == 'kernel':
            self.process_kernel(routine, item, successors, targets)
        if role == 'driver':
           self.process_driver(routine, successors)

    @staticmethod
    def _get_parkind_suffix(type):
        return type.rsplit('_')[1][1:3]

    def _build_parkind_import(self, field_array_module, wrapper_types):

        deferred_type = SymbolAttributes(BasicType.DEFERRED, imported=True)
        vars = {Variable(name='JP' + self._get_parkind_suffix(type), type=deferred_type, scope=field_array_module)
                for type in wrapper_types}

        return ir.Import(module='PARKIND1', symbols=as_tuple(vars))

    def _build_field_array_types(self, field_array_module, wrapper_types):

        typedefs = ()
        for type in wrapper_types:
            suff = self._get_parkind_suffix(type)
            kind = field_array_module.symbol_map['JP' + suff]
            rank = int(type.rsplit('_')[1][0])

            view_shape = (RangeIndex(children=(None, None)),) * (rank - 1)
            array_shape = (RangeIndex(children=(None, None)),) * rank

            if suff == 'IM':
                basetype = BasicType.INTEGER
            elif suff == 'LM':
                basetype = BasicType.LOGICAL
            else:
                basetype = BasicType.REAL

            pointer_type = SymbolAttributes(basetype, pointer=True, kind=kind, shape=view_shape)
            contig_pointer_type = pointer_type.clone(contiguous=True, shape=array_shape)

            pointer_var = Variable(name='P', type=pointer_type, dimensions=view_shape)
            contig_pointer_var = pointer_var.clone(name='P_FIELD', type=contig_pointer_type, dimensions=array_shape)

            decls = (ir.VariableDeclaration(symbols=(pointer_var,)),)
            decls += (ir.VariableDeclaration(symbols=(contig_pointer_var,)),)

            typedefs += (ir.TypeDef(name=type, body=decls, parent=field_array_module),)

        return typedefs

    def _create_dummy_field_api_defs(self, field_array_mod_imports):

        wrapper_types = {sym.name for imp in field_array_mod_imports for sym in imp.symbols}

        # create dummy module with empty spec
        field_array_module = Module(name='FIELD_ARRAY_MODULE', spec=ir.Section(body=()))

        # build parkind1 import
        parkind_import = self._build_parkind_import(field_array_module, wrapper_types)
        field_array_module.spec.append(parkind_import)

        # build dummy type definitions
        typedefs = self._build_field_array_types(field_array_module, wrapper_types)
        field_array_module.spec.append(typedefs)

        return [field_array_module,]

    @staticmethod
    def propagate_defs_to_children(key, definitions, successors):
        for child in successors:
            child.ir.enrich(definitions)
            child.trafo_data.update({key: {'definitions': definitions}})

    def process_driver(self, routine, successors):

        # create dummy definitions for field_api wrapper types
        field_array_mod_imports = [imp for imp in routine.imports if imp.module.lower() == 'field_array_module']
        definitions = []
        if field_array_mod_imports:
            definitions += self._create_dummy_field_api_defs(field_array_mod_imports)

        # propagate dummy field_api wrapper definitions to children
        self.propagate_defs_to_children(self._key, definitions, successors)

    def build_ydvars_global_gfl_ptr(self, var):
        if (parent := var.parent):
            parent = self.build_ydvars_global_gfl_ptr(parent)

        _type = var.type
        if 'gfl_ptr' in var.name.lower().split('%')[-1]:
            _type = parent.type.dtype.typedef.variable_map['gfl_ptr_g'].type

        return var.clone(name=var.name.upper().replace('GFL_PTR', 'GFL_PTR_G'),
                         parent=parent, type=_type)

    def process_kernel(self, routine, item, successors, targets):

        # Sanitize the subroutine
        resolve_associates(routine)
        v_index = SCCBaseTransformation.get_integer_variable(routine, name=self.horizontal.index)
        SCCBaseTransformation.resolve_masked_stmts(routine, loop_variable=v_index)

        if self.horizontal.bounds[0] in routine.variables and self.horizontal.bounds[1] in routine.variables:
            _bounds = self.horizontal.bounds
        else:
            _bounds = self.horizontal._bounds_aliases
        SCCBaseTransformation.resolve_vector_dimension(routine, loop_variable=v_index, bounds=_bounds)

        # build list of type-bound array access using the horizontal index
        vars = [var for var in FindVariables().visit(routine.body)
                if isinstance(var, Array) and var.parents and self.horizontal.index in getattr(var, 'dimensions', ())]

        # build list of type-bound view pointers passed as subroutine arguments
        for call in [call for call in FindNodes(ir.CallStatement).visit(routine.body) if call.name in targets]:
            _args = {a: d for d, a in call.arg_map.items() if isinstance(d, Array)}
            _args = {a: d for a, d in _args.items()
                     if any([v in d.shape for v in self.horizontal.size_expressions]) and a.parents}
            vars += list(_args)

        # replace per-block view pointers with full field pointers
        vmap = {var: var.clone(name=var.name_parts[-1] + '_FIELD',
                               type=self.get_parent_typedef(var, routine).variable_map[var.name_parts[-1] + '_FIELD'].type)
                for var in vars}

        # replace thread-private GFL_PTR with global
        vmap.update({v: self.build_ydvars_global_gfl_ptr(vmap.get(v, v))
                     for v in FindVariables().visit(routine.body) if 'ydvars%gfl_ptr' in v.name.lower()})
        vmap = recursive_expression_map_update(vmap)

        # filter out arrays marked for exclusion
        vmap = {k: v for k, v in vmap.items() if not any(e in k for e in self.exclude)}

        # finally perform the substitution
        routine.body = SubstituteExpressions(vmap).visit(routine.body)

        # propagate dummy field_api wrapper definitions to children
        definitions = item.trafo_data[self._key]['definitions']
        self.propagate_defs_to_children(self._key, definitions, successors)


class BlockIndexInjectTransformation(Transformation):

    _key = 'BlockIndexInjectTransformation'

    # This trafo only operates on procedures
    item_filter = (ProcedureItem,)

    def __init__(self, block_dim, exclude=[], key=None):
        self.block_dim = block_dim
        self.exclude = exclude
        if key:
             self._key = key

    def transform_subroutine(self, routine, **kwargs):

        role = kwargs['role']
        targets = tuple(str(t).lower() for t in as_tuple(kwargs.get('targets', None)))

        if role == 'kernel':
            self.process_kernel(routine, targets)

    @staticmethod
    def _update_expr_map(var, rank, index):
        if getattr(var, 'dimensions', None):
            return {var: var.clone(dimensions=var.dimensions + as_tuple(index))}
        else:
            return {var:
                    var.clone(dimensions=((RangeIndex(children=(None, None)),) * (rank - 1)) + as_tuple(index))}

    @staticmethod
    def get_call_arg_rank(arg):
        rank = len(arg.shape) if getattr(arg, 'shape', None) else 0
        if getattr(arg, 'dimensions', None):
            # We assume here that the callstatement is free of sequence association
            rank = rank - len([d for d in arg.dimensions if not isinstance(d, RangeIndex)])

        return rank

    def get_block_index(self, routine):
        variable_map = routine.variable_map
        if (block_index := variable_map.get(self.block_dim.index, None)):
            return block_index
        elif any(i.rsplit('%')[0] in variable_map for i in self.block_dim._index_aliases):
            index_name = [alias for alias in self.block_dim._index_aliases
                          if alias.rsplit('%')[0] in variable_map][0]

            block_index = resolve_typebound_var(index_name, variable_map)

        return block_index

    def process_kernel(self, routine, targets):

        # we skip routines that do not contain the block index or any known alias
        if not (block_index := self.get_block_index(routine)):
            return

        # The logic for callstatement args differs from other variables in the body,
        # so we build a list to filter
        call_args = [a for call in FindNodes(ir.CallStatement).visit(routine.body) for a in call.arguments]

        # First get rank mismatched call statement args
        vmap = {}
        for call in [call for call in FindNodes(ir.CallStatement).visit(routine.body) if call.name in targets]:
            for dummy, arg in call.arg_map.items():
                arg_rank = self.get_call_arg_rank(arg)
                dummy_rank = len(dummy.shape) if getattr(dummy, 'shape', None) else 0
                if arg_rank - 1 == dummy_rank:
                    vmap.update(self._update_expr_map(arg, arg_rank, block_index))

        # Now get the rest of the variables
        for var in [var for var in FindVariables().visit(routine.body)
                    if getattr(var, 'dimensions', None) and not var in call_args]:

            local_rank = len(var.dimensions)
            decl_rank = local_rank
            # we assume here that all derived-type components we wish to transform
            # have been parsed
            if getattr(var, 'shape', None):
                decl_rank = len(var.shape)

            if local_rank == decl_rank - 1:
                vmap.update(self._update_expr_map(var, decl_rank, block_index))

        # filter out arrays marked for exclusion
        vmap = {k: v for k, v in vmap.items() if not any(e in k for e in self.exclude)}

        routine.body = SubstituteExpressions(vmap).visit(routine.body)
