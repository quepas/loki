# (C) Copyright 2018- ECMWF.
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.

from loki.batch import Transformation
from loki.ir import nodes as ir, Transformer, FindNodes
from loki.tools.util import as_tuple, CaseInsensitiveDict

__all__ = ['DuplicateKernel', 'RemoveKernel']


class DuplicateKernel(Transformation):

    creates_items = True

    reverse_traversal = True

    def __init__(self, kernels=None, duplicate_suffix='duplicated',
                 duplicate_module_suffix=None):
        self.suffix = duplicate_suffix
        self.module_suffix = duplicate_module_suffix or duplicate_suffix
        self.kernels = tuple(kernel.lower() for kernel in as_tuple(kernels))

    def _create_duplicate_items(self, successors, item_factory, config):
        new_items = ()
        for item in successors:
            if item.local_name in self.kernels:
                # Determine new item name
                scope_name = item.scope_name
                local_name = f'{item.local_name}{self.suffix}'
                if scope_name:
                    scope_name = f'{scope_name}{self.module_suffix}'

                # Try to get existing item from cache
                new_item_name = f'{scope_name or ""}#{local_name}'
                new_item = item_factory.item_cache.get(new_item_name)

                # Try to get an item for the scope or create that first
                if new_item is None and scope_name:
                    scope_item = item_factory.item_cache.get(scope_name)
                    if scope_item:
                        scope = scope_item.ir
                        if local_name not in scope and item.local_name in scope:
                            # Rename the existing item to the new name
                            scope[item.local_name].name = local_name

                        if local_name in scope:
                            new_item = item_factory.create_from_ir(
                                scope[local_name], scope, config=config
                            )

                # Create new item
                if new_item is None:
                    new_item = item_factory.get_or_create_item_from_item(new_item_name, item, config=config)
                new_items += as_tuple(new_item)
        return tuple(new_items)

    def transform_subroutine(self, routine, **kwargs):
        # Create new dependency items
        new_dependencies = self._create_duplicate_items(
            successors=as_tuple(kwargs.get('successors')),
            item_factory=kwargs.get('item_factory'),
            config=kwargs.get('scheduler_config')
        )
        new_dependencies = CaseInsensitiveDict((new_item.local_name, new_item) for new_item in new_dependencies)

        # Duplicate calls to kernels
        call_map = {}
        new_imports = []
        for call in FindNodes(ir.CallStatement).visit(routine.body):
            call_name = str(call.name).lower()
            if call_name in self.kernels:
                # Duplicate the call
                new_call_name = f'{call_name}{self.suffix}'.lower()
                new_item = new_dependencies[new_call_name]
                proc_symbol = new_item.ir.procedure_symbol.rescope(scope=routine)
                call_map[call] = (call, call.clone(name=proc_symbol))

                # Register the module import
                if new_item.scope_name:
                    new_imports += [ir.Import(module=new_item.scope_name, symbols=(proc_symbol,))]

        if call_map:
            routine.body = Transformer(call_map).visit(routine.body)
            if new_imports:
                routine.spec.prepend(as_tuple(new_imports))

    def plan_subroutine(self, routine, **kwargs):
        item = kwargs.get('item')
        item.plan_data.setdefault('additional_dependencies', ())
        item.plan_data['additional_dependencies'] += self._create_duplicate_items(
            successors=as_tuple(kwargs.get('successors')),
            item_factory=kwargs.get('item_factory'),
            config=kwargs.get('scheduler_config')
        )


class RemoveKernel(Transformation):

    creates_items = True

    def __init__(self, kernels=None):
        self.kernels = tuple(kernel.lower() for kernel in as_tuple(kernels))

    def transform_subroutine(self, routine, **kwargs):
        call_map = {
            call: None for call in FindNodes(ir.CallStatement).visit(routine.body)
            if str(call.name).lower() in self.kernels
        }
        routine.body = Transformer(call_map).visit(routine.body)

    def plan_subroutine(self, routine, **kwargs):
        item = kwargs.get('item')

        successors = as_tuple(kwargs.get('successors'))
        item.plan_data.setdefault('removed_dependencies', ())
        item.plan_data['removed_dependencies'] += tuple(
            child for child in successors if child.local_name in self.kernels
        )
