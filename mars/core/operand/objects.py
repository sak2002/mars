# Copyright 1999-2020 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from ... import opcodes
from ...serialization.serializables import BoolField
from ..entity import OutputType, register_fetch_class
from .base import Operand, VirtualOperand
from .core import TileableOperandMixin
from .fetch import FetchMixin, Fetch
from .fuse import Fuse, FuseChunkMixin


class ObjectOperand(Operand):
    pass


class ObjectOperandMixin(TileableOperandMixin):
    _output_type_ = OutputType.object

    def get_fuse_op_cls(self, obj):
        return ObjectFuseChunk


class ObjectFuseChunkMixin(FuseChunkMixin, ObjectOperandMixin):
    __slots__ = ()


class ObjectFuseChunk(ObjectFuseChunkMixin, Fuse):
    pass


class ObjectFetch(FetchMixin, ObjectOperandMixin, Fetch):
    _output_type_ = OutputType.object

    def __init__(self, to_fetch_key=None, **kw):
        kw.pop('output_types', None)
        kw.pop('_output_types', None)
        super().__init__(_to_fetch_key=to_fetch_key, **kw)

    def _new_chunks(self, inputs, kws=None, **kw):
        if '_key' in kw and self.to_fetch_key is None:
            self.to_fetch_key = kw['_key']
        return super()._new_chunks(inputs, kws=kws, **kw)

    def _new_tileables(self, inputs, kws=None, **kw):
        if '_key' in kw and self.to_fetch_key is None:
            self.to_fetch_key = kw['_key']
        return super()._new_tileables(inputs, kws=kws, **kw)


register_fetch_class(OutputType.object, ObjectFetch, None)


class MergeDictOperand(ObjectOperand, ObjectOperandMixin):
    _merge = BoolField('merge')

    def __init__(self, merge=None, **kw):
        super().__init__(_merge=merge, **kw)

    @property
    def merge(self):
        return self._merge

    @classmethod
    def concat_tileable_chunks(cls, tileable):
        assert not tileable.is_coarse()

        op = cls(merge=True)
        chunk = cls(merge=True).new_chunk(tileable.chunks)
        return op.new_tileable([tileable], chunks=[chunk], nsplits=((1,),))

    @classmethod
    def execute(cls, ctx, op):
        assert op.merge
        inputs = [ctx[inp.key] for inp in op.inputs]
        ctx[op.outputs[0].key] = next(inp for inp in inputs if inp)


class SuccessorsExclusive(ObjectOperandMixin, VirtualOperand):
    _op_module_ = 'core'
    _op_type_ = opcodes.SUCCESSORS_EXCLUSIVE

    def _new_chunks(self, inputs, kws=None, **kw):
        from ...context import get_context, RunningMode

        ctx = get_context()
        if ctx.running_mode == RunningMode.local:
            # set inputs to None if local
            inputs = None
        return super()._new_chunks(inputs, kws=kws, **kw)

    @classmethod
    def execute(cls, ctx, op):
        from ...context import RunningMode

        # only for local
        if ctx.running_mode == RunningMode.local:
            ctx[op.outputs[0].key] = ctx.create_lock()
        else:  # pragma: no cover
            raise RuntimeError('Cannot execute SuccessorsExclusive '
                               'which is a virtual operand '
                               'for the distributed runtime')